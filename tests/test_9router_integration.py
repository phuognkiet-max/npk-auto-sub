"""Router wiring preserves project content, endpoint identity, and API admission."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx
import core
from core import DATA, DEFAULTS
import server
import worker
from execution_admission import AdmissionError, PROTECTED_PROJECT_ID, admission_status, provider_required
from provider_usage import TranslationUsage, prepare_lineage, validate_lineage, read_summary
from translation_checkpoint import translation_signature
from stage_journal import stage_signature
from speech_rewrite import rewrite_signature


class RouterIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))
        self.enterContext(patch.object(core, 'DATA', self.base))
        source = self.base/'source.mp4'
        source.write_bytes(b'isolated source identity')
        self.project = {'id':uuid.uuid4().hex, 'source_revision':uuid.uuid4().hex, 'updated':1,
            'media':{'path':str(source),'duration':20.,'width':1280,'height':720,'audio':True},
            'regions':[], 'artifacts':{}, 'params':DEFAULTS | {'provider':'router9',
                'translation_model':'ag/gemini-3.8-test', 'router9_base_url':'http://127.0.0.1:20129/v1',
                'translation_scope':'missing','subtitle_automation':'router9'},
            'segments':[{'start':float(i*2),'end':float(i*2+1),'text':'Source sentence',
                'translated':'Existing translation' if i<5 else ''} for i in range(8)]}

    def test_project_validation_accepts_route_ids_and_incomplete_setup(self):
        params=server.validate_params({'provider':'router9','translation_model':'ag/gemini-3.8-test',
                                      'router9_base_url':'http://127.0.0.1:20129/'})
        self.assertEqual(params['router9_base_url'],'http://127.0.0.1:20129/v1')
        self.assertEqual(server.validate_params({'translation_model':'cx/example'},provider='router9')['translation_model'],'cx/example')
        self.assertEqual(server.validate_params({'provider':'router9','translation_model':''})['translation_model'],'')
        with self.assertRaises(ValueError):
            server.validate_params({'provider':'gemini','translation_model':'ag/gemini-test'})

    def test_router_fingerprints_bind_endpoint_without_invalidating_direct_providers(self):
        changed=copy.deepcopy(self.project)
        changed['params']['router9_base_url']='http://127.0.0.1:20130/v1'
        for fingerprint in (translation_signature, rewrite_signature, lambda p:stage_signature('translate',p),
                            lambda p:stage_signature('auto_subtitles',p)):
            self.assertNotEqual(fingerprint(self.project),fingerprint(changed))
        for p in (self.project,changed):
            p['params'].update(provider='gemini',translation_model='gemini-test',subtitle_automation='gemini')
        for fingerprint in (translation_signature,rewrite_signature,lambda p:stage_signature('translate',p),
                            lambda p:stage_signature('auto_subtitles',p)):
            self.assertEqual(fingerprint(self.project),fingerprint(changed))

    def test_router_director_stays_subject_to_project_hold(self):
        self.project['id']=PROTECTED_PROJECT_ID
        self.project['params'].update(pipeline_translate=False,pipeline_extract='ocr')
        monitor=self.base/'monitor.json'
        monitor.write_text(json.dumps({'project_id':PROTECTED_PROJECT_ID,'audit_hold':True,'quota_scope':'unknown'}))
        for kind in ('ocr','auto_subtitles','pipeline'):
            self.assertTrue(provider_required(self.project,kind))
            self.assertEqual(admission_status(self.project,[],kind=kind,monitor_path=monitor)['blocked_code'],'audit_hold')

    def test_bad_endpoint_and_held_probe_stop_before_credentials(self):
        with patch('server.keyring.get_password') as vault:
            with self.assertRaises(ValueError):
                server.keys_check('router9',{'base_url':'https://secret@example.com/v1','mode':'catalog'})
            vault.assert_not_called()
        with patch('server.keyring.get_password') as vault:
            with self.assertRaises(ValueError):
                server.keys_check('router9',{'mode':'generation','model':'ag/test','confirm_cost':True})
            vault.assert_not_called()
        self.project['id']=PROTECTED_PROJECT_ID
        monitor=self.base/'monitor.json'
        monitor.write_text(json.dumps({'project_id':PROTECTED_PROJECT_ID,'audit_hold':True,'quota_scope':'unknown'}))
        with patch('server.project_by_id',return_value=self.project), patch('server.all_items',return_value=[]), \
             patch('execution_admission.default_monitor_path',return_value=monitor), \
             patch('server.keyring.get_password') as vault:
            with self.assertRaises(AdmissionError):
                server.keys_check('router9',{'base_url':self.project['params']['router9_base_url'],
                    'mode':'generation','model':'ag/test','confirm_cost':True,'project_id':PROTECTED_PROJECT_ID})
            vault.assert_not_called()

    def test_preflight_router_director_needs_only_router_credential(self):
        from preflight import inspect
        with patch('auto_subtitle_director.automation_status',return_value={'available':True,'can_process':False}), \
             patch('credentials.active_secret',return_value='TEST-ONLY') as vault:
            result=inspect(self.project,'auto_subtitles')
        self.assertEqual(vault.call_args_list[0].args,('router9',))
        self.assertFalse(any('Gemini' in error for error in result['errors']))
        self.project['params']['translation_model']=''
        with patch('auto_subtitle_director.automation_status',return_value={'available':True,'can_process':False}):
            result=inspect(self.project,'auto_subtitles',check_credentials=False)
        self.assertTrue(any('chọn model' in error for error in result['errors']))

    def test_budget_and_ledger_cannot_be_reused_at_another_endpoint(self):
        jid=uuid.uuid4().hex; folder=self.base/'jobs'/jid; folder.mkdir(parents=True)
        descriptor=prepare_lineage(self.project,'translate',jid,jobs_root=self.base/'jobs')
        usage=TranslationUsage(folder,3,20,'router9',self.project['params']['translation_model'],
            lineage=descriptor,job_id=jid,jobs_root=self.base/'jobs',endpoint=self.project['params']['router9_base_url'])
        usage.set_batch([5,6,7],'fixture prompt')
        usage.observe({'event':'before_send','attempt':1})
        changed=copy.deepcopy(self.project); changed['params']['router9_base_url']='http://127.0.0.1:20130/v1'
        with self.assertRaises(ValueError): validate_lineage(descriptor,changed)
        with self.assertRaises(ValueError):
            TranslationUsage(folder,3,20,'router9',self.project['params']['translation_model'],endpoint=changed['params']['router9_base_url'])
        self.assertEqual(read_summary(folder)['request_attempts'],1)

    def test_translation_uses_router_endpoint_and_preserves_five_existing_lines(self):
        project=copy.deepcopy(self.project); project['params']['subtitle_automation']='local'
        jid=uuid.uuid4().hex; folder=self.base/'jobs'/jid; folder.mkdir(parents=True)
        job={'id':jid,'kind':'translate','project_id':project['id'],'source_revision':project['source_revision']}
        job['api_usage_lineage']=prepare_lineage(project,'translate',jid,jobs_root=self.base/'jobs')
        calls=[]
        def send(request):
            calls.append(request)
            self.assertEqual(str(request.url),'http://127.0.0.1:20129/v1/chat/completions')
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(
                {'lines':[{'id':i,'text':'Bản dịch thử'} for i in (5,6,7)]})}}],
                'usage':{'prompt_tokens':100,'completion_tokens':20,'total_tokens':120}})
        client=httpx.Client(transport=httpx.MockTransport(send))
        self.addCleanup(client.close)
        with patch('providers.httpx.Client',return_value=client), \
             patch('credentials.active_secret',return_value='TEST-ONLY'), patch('worker.emit'):
            result=worker.translation(project,job,folder)
        self.assertEqual(len(calls),1)
        self.assertEqual([r['translated'] for r in result['segments'][:5]],['Existing translation']*5)
        saved=json.loads((folder/'translation-checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(bool(r['translated']) for r in saved['rows']),8)
        usage=read_summary(folder)
        self.assertEqual((usage['request_attempts'],usage['reported_tokens']['total_tokens']),(1,120))


if __name__=='__main__':
    unittest.main()
