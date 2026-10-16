import copy
import json
import unittest
from unittest.mock import patch
import httpx
from core import DEFAULTS, ROOT, put
import credentials
import providers
from server import validate_params, keys_check
from worker import translation, speech_signature


class FunctionalTests(unittest.TestCase):
    def setUp(self):
        self.passwords={}
        self.patches=[patch.object(credentials.keyring,'get_password',side_effect=lambda s,k:self.passwords.get(k)),
                      patch.object(credentials.keyring,'set_password',side_effect=lambda s,k,v:self.passwords.__setitem__(k,v)),
                      patch.object(credentials.keyring,'delete_password',side_effect=lambda s,k:self.passwords.pop(k,None))]
        for p in self.patches:p.start()
        self.addCleanup(lambda:[p.stop() for p in reversed(self.patches)])
        for provider in credentials.PROVIDERS:put('credential_slots',provider,{})

    def client(self, handler):
        client=httpx.Client(transport=httpx.MockTransport(handler))
        self.addCleanup(client.close)
        return client

    def test_multiple_keys_dedupe_manual_selection_and_removal(self):
        rows=credentials.add_credentials('gemini','fake-secret-AAAA,fake-secret-BBBB,fake-secret-AAAA')
        self.assertEqual(len(rows),2)
        self.assertTrue(rows[0]['active'])
        credentials.select_credential('gemini',rows[1]['id'])
        self.assertEqual(credentials.active_secret('gemini'),'fake-secret-BBBB')
        credentials.delete_credential('gemini',rows[1]['id'])
        self.assertIsNone(credentials.active_secret('gemini'))
        self.assertFalse(credentials.credential_status('gemini')['present'])
        self.assertNotIn('fake-secret',json.dumps(credentials.credential_list('gemini')))

    def test_key_check_bound_to_exact_secret(self):
        rows=credentials.add_credentials('gemini','fake-secret-AAAA')
        key=rows[0]['id']
        credentials.record_check('gemini',key,'fake-secret-AAAA',{'ok':True,'scope':'generation'})
        self.assertTrue(credentials.credential_status('gemini')['provider_verified'])
        self.passwords['gemini:'+key]='replaced-key-ZZZZ'
        self.assertFalse(credentials.credential_status('gemini')['provider_verified'])

    def test_catalog_is_not_generation_success_and_paginates(self):
        calls=[]
        def handle(request):
            self.assertNotIn('SECRET',str(request.url))
            self.assertEqual(request.headers['x-goog-api-key'],'SECRET')
            calls.append(request)
            if len(calls)==1:return httpx.Response(200,json={'models':[{'name':'models/gemini-test','supportedGenerationMethods':['generateContent']},{'name':'models/embedding','supportedGenerationMethods':['embedContent']}],'nextPageToken':'next'})
            return httpx.Response(200,json={'models':[{'name':'models/gemini-other','supportedGenerationMethods':['generateContent']}]})
        result=providers.check_connection('gemini','SECRET',client=self.client(handle))
        self.assertEqual(result['status'],'catalog_ok')
        self.assertEqual(len(result['models']),2)
        self.assertEqual(calls[1].url.params['pageToken'],'next')

    def test_provider_failures_do_not_leak_raw_response(self):
        for code,status in [(400,'request'),(401,'auth'),(402,'credit'),(403,'permission'),(404,'model'),(429,'quota'),(500,'service')]:
            result=providers.check_connection('deepseek','SECRET',client=self.client(lambda r:httpx.Response(code,text='SECRET raw body')))
            self.assertFalse(result['ok']);self.assertEqual(result['status'],status);self.assertNotIn('SECRET',json.dumps(result))

    def test_timeout_is_not_wrong_key(self):
        def handle(request):raise httpx.ReadTimeout('SECRET',request=request)
        result=providers.check_connection('gemini','SECRET',client=self.client(handle))
        self.assertEqual(result['status'],'timeout');self.assertNotIn('SECRET',json.dumps(result))

    def test_generation_needs_explicit_cost_consent(self):
        credentials.add_credentials('gemini','fake-secret-AAAA')
        with patch('providers.check_connection') as call:
            with self.assertRaises(ValueError):keys_check('gemini',{'mode':'generation','model':'gemini-test'})
            call.assert_not_called()

    def test_generation_probe_contract(self):
        def handle(request):
            payload=json.loads(request.content)
            self.assertEqual(payload['generationConfig']['maxOutputTokens'],256)
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':'OK'}]}}]})
        result=providers.check_connection('gemini','SECRET','gemini-test','generation',self.client(handle))
        self.assertTrue(result['ok']);self.assertEqual(result['status'],'ready')

    def test_settings_validate_real_voice_and_ranges(self):
        from voices import entries
        self.assertGreaterEqual(len(entries()),25)
        self.assertEqual(validate_params({'voice_id':entries()[1]['id']})['voice_id'],entries()[1]['id'])
        for p in ({'voice_id':'../not-a-voice'},{'ocr_confidence':2},{'cover_color':'black;evil'}, {'translation_model':'../bad'},{'video_encoder':'fantasy'}):
            with self.assertRaises(ValueError):validate_params(p)

    def test_voice_selection_invalidates_old_audio(self):
        p={'params':DEFAULTS.copy(),'segments':[],'media':{'duration':8}};other=copy.deepcopy(p);other['params']['voice_id']='piper:another'
        self.assertNotEqual(speech_signature(p),speech_signature(other))

    def test_preflight_blocks_undubbed_original_and_tracked_light_cover(self):
        from preflight import inspect
        p={'params':DEFAULTS|{'pipeline_translate':False,'pipeline_tts':True},'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'audio':True,'duration':8},'regions':[],
           'segments':[{'start':0,'end':2,'text':'中文','translated':''}]}
        self.assertFalse(inspect(p,'pipeline')['ok'])
        p['params'].update(method='box',mask_source='tracked');p['regions']=[{'x':0}]
        self.assertFalse(inspect(p,'clean')['ok'])

    def test_missing_translation_cannot_silently_read_chinese(self):
        from worker import tts
        with patch('voices.load_voice') as load:
            with self.assertRaises(ValueError):tts({'params':DEFAULTS,'segments':[{'text':'中文'}]}, {},None)
            load.assert_not_called()

    def test_preflight_requires_clean_artifact_before_spending_api_quota(self):
        from preflight import inspect
        p={'params':DEFAULTS|{'pipeline_clean':False,'pipeline_translate':False,'pipeline_tts':False},
           'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'audio':True,'duration':8},'segments':[{'start':0,'end':2,'text':'x','translated':'y'}], 'regions':[],'artifacts':{}}
        result=inspect(p,'pipeline')
        self.assertFalse(result['ok'])
        self.assertTrue(any('Dùng bản đã xóa chữ' in s for s in result['errors']))

    def test_translation_budget_batching_and_missing_ids(self):
        import tempfile
        from pathlib import Path
        from core import DATA
        credentials.store_credential('gemini','fake-secret-AAAA')
        p={'params':DEFAULTS|{'translation_batch':1}, 'segments':[{'start':0,'end':2,'text':'one','translated':''},{'start':3,'end':5,'text':'two','translated':''}]}
        calls=[]
        def generate(provider,secret,model,prompt,**kwargs):
            inputs=json.loads(prompt.split('Input:\n')[1]);calls.append(inputs)
            return json.dumps({'lines':[{'id':i['id'],'text':'Xin chào'} for i in inputs]})
        with tempfile.TemporaryDirectory(dir=DATA) as folder,patch('providers.generate',side_effect=generate):
            result=translation(p,{},Path(folder));self.assertEqual(len(calls),2);self.assertEqual(calls[0][0]['max_words'],10)
            self.assertTrue(all(r['translated'] for r in result['segments']))
        with tempfile.TemporaryDirectory(dir=DATA) as folder,patch('providers.generate',return_value='{"lines":[]}'),patch('worker.time.sleep'):
            with self.assertRaises(ValueError):translation(p,{},Path(folder))

    def test_quota_is_not_retried_or_rotated(self):
        credentials.store_credential('gemini','fake-secret-AAAA')
        with patch('providers.generate',side_effect=providers.ProviderError('quota','HTTP 429')) as generate:
            with self.assertRaises(providers.ProviderError):translation({'params':DEFAULTS,'segments':[{'start':0,'end':1,'text':'one'}]}, {},None)
            self.assertEqual(generate.call_count,1)

    def test_nvenc_probe_and_cpu_fallback(self):
        from encoding import video_encoding
        with patch('encoding.nvenc_ready',return_value=False):
            self.assertIn('libx264',video_encoding(DEFAULTS|{'video_encoder':'auto'})[0])
            with self.assertRaises(ValueError):video_encoding(DEFAULTS|{'video_encoder':'nvenc'})
        with patch('encoding.nvenc_ready',return_value=True):self.assertIn('h264_nvenc',video_encoding(DEFAULTS|{'video_encoder':'auto'})[0])

    def test_light_preview_never_changes_outside_mask(self):
        from cleaner import Cleaner
        from pathlib import Path
        import numpy as np
        frame=np.random.default_rng(7).integers(0,256,(100,160,3),dtype='uint8')
        region={'x':50,'y':50,'w':30,'h':20,'start':1,'end':2}
        for method in ('box','blur','stretch'):
            cleaner=Cleaner(Path('no-model'),DEFAULTS|{'method':method})
            result,mask=cleaner(frame,[region],1.5)
            self.assertTrue(np.array_equal(frame[mask==0],result[mask==0]))
            result,mask=cleaner(frame,[region],2)
            self.assertFalse(mask.any());self.assertTrue(np.array_equal(frame,result))
