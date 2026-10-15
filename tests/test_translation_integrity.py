"""Translation failures preserve completed batches without malformed publication."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import DEFAULTS, ROOT
from providers import ProviderError
from translation_checkpoint import (read_checkpoint, write_checkpoint, validate_resume,
                                    validated_partial_rows)
from translation_quality import facts, review_translation
import worker


def project():
    return {'params': DEFAULTS | {'translation_batch':1,'translation_scope':'all'},
            'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':8},
            'artifacts':{},'regions':[],
            'segments':[{'start':0.,'end':1.,'text':'你好','translated':''},
                        {'start':2.,'end':3.,'text':'谢谢','translated':''}]}


class TranslationIntegrityTests(unittest.TestCase):
    def test_short_cue_budget_is_advisory_and_complete_subtitle_is_preserved(self):
        p = project()
        p['segments'] = [{'start': 0., 'end': .5, 'text': 'Chị Lan, mua 2 chai 500ml, giảm 20%, đặt ngay nhé!',
                          'translated': '', 'spoken_text': 'Lời đọc riêng đã duyệt.'}]
        translated = 'Chị Lan ơi, mua 2 chai dung tích 500ml, được giảm 20%; đặt hàng ngay nhé!'
        prompts = []

        def service(provider, secret, model, prompt, **kwargs):
            prompts.append(prompt)
            lines = json.loads(prompt.split('Input:\n')[1])
            self.assertEqual(lines[0]['max_words'], 2)
            return json.dumps({'lines': [{'id': lines[0]['id'], 'text': translated}]}, ensure_ascii=False)

        with tempfile.TemporaryDirectory() as tmp, patch('credentials.active_secret', return_value='test-only'), \
                patch('worker.emit'), patch('providers.generate', side_effect=service):
            result = worker.translation(p, {}, Path(tmp))
            saved = read_checkpoint(Path(tmp) / 'translation-checkpoint.json')
            self.assertEqual(saved['rows'][0]['translated'], translated)
            self.assertIn(translated, (Path(tmp) / 'translated.srt').read_text(encoding='utf-8-sig'))
        self.assertEqual(len(prompts), 1)
        self.assertIn('advisory reading-time estimates, not output limits', prompts[0])
        self.assertIn('Never drop words or shorten content to satisfy them', prompts[0])
        self.assertIn('forms of address, repetitions, emphasis, slang', prompts[0])
        self.assertEqual(result['segments'][0]['translated'], translated)
        self.assertEqual(result['segments'][0]['spoken_text'], 'Lời đọc riêng đã duyệt.')
        self.assertTrue(any('vượt ngân sách từ' in warning for warning in result['warnings']))

    def test_duplicate_json_keys_nonfinite_and_deep_output_never_publish_wrong_cue(self):
        outputs = ['{"lines":[{"id":1,"id":0,"text":"wrong cue"}]}',
                   '{"lines":[{"id":0,"text":"first","text":"second"}]}',
                   '{"lines":[],"lines":[{"id":0,"text":"wrong duplicate"}]}',
                   '{"lines":[{"id":0,"text":"bad"}],"meta":NaN}',
                   '{"lines":[{"id":0,"text":"bad"}],"meta":1e10000}',
                   '['*1500+'0'+']'*1500]
        for output in outputs:
            p=project(); original=copy.deepcopy(p)
            with self.subTest(output=output[:70]),tempfile.TemporaryDirectory() as tmp, \
                    patch('credentials.active_secret',return_value='test-only'), \
                    patch('worker.emit'),patch('providers.generate',return_value=output) as generate:
                with self.assertRaisesRegex(ValueError,'sai cấu trúc sau 3 lần'):
                    worker.translation(p,{'segment_indices':[0]},Path(tmp))
                self.assertEqual(generate.call_count,3)
                self.assertEqual(p,original)
                self.assertFalse((Path(tmp)/'translated.srt').exists())
                self.assertEqual(read_checkpoint(Path(tmp)/'translation-checkpoint.json')['completed_indices'],[])

    def test_malformed_translation_never_coerces_null_boolean_or_list_into_cues(self):
        for entry in ({'id':True,'text':'wrong'}, {'id':'0','text':'wrong'},
                      {'id':0,'text':None}, {'id':0,'text':['wrong']},
                      {'id':0,'text':'x'*10001}, {'id':0,'text':'bad\x00text'}):
            p=project()
            with self.subTest(entry=str(entry)[:40]), tempfile.TemporaryDirectory() as tmp, \
                    patch('credentials.active_secret',return_value='test-only'), \
                    patch('worker.emit'),patch('providers.generate',return_value=json.dumps({'lines':[entry]})) as generate:
                with self.assertRaisesRegex(ValueError,'sai cấu trúc sau 3 lần'):
                    worker.translation(p,{},Path(tmp))
                self.assertEqual(generate.call_count,3)
                self.assertFalse((Path(tmp)/'translated.srt').exists())
                saved=read_checkpoint(Path(tmp)/'translation-checkpoint.json')
                self.assertEqual(saved['completed_indices'],[])
                self.assertFalse(any(r['translated'] for r in p['segments']))

    def test_exhausted_provider_retry_is_not_multiplied_and_first_batch_stays_saved(self):
        p=project();initial=copy.deepcopy(p);calls=[]
        def service(provider,secret,model,prompt,**kwargs):
            line=json.loads(prompt.split('Input:\n')[1])[0]
            calls.append(line['id'])
            if line['id']==1:
                kwargs['on_retry']({'message':'HTTP 503; lần 2/3'})
                raise ProviderError('service','HTTP 503 after provider retry budget')
            return json.dumps({'lines':[{'id':0,'text':'Xin chào'}]})
        with tempfile.TemporaryDirectory() as tmp,patch('credentials.active_secret',return_value='test-only'), \
                patch('worker.emit') as emit,patch('providers.generate',side_effect=service):
            with self.assertRaises(ProviderError):worker.translation(p,{},Path(tmp))
            doc=read_checkpoint(Path(tmp)/'translation-checkpoint.json')
            self.assertEqual(validate_resume(doc,initial),[0])
            self.assertEqual(doc['rows'][0]['translated'],'Xin chào')
            self.assertEqual(calls,[0,1])
            self.assertFalse((Path(tmp)/'translated.srt').exists())
            self.assertTrue(any(call.kwargs.get('stage')=='provider_retry' for call in emit.call_args_list))

    def test_first_batch_outage_preserves_selected_scope_before_any_translation(self):
        p=project();p['segments'][0]['translated']='Đã sửa tay'
        with tempfile.TemporaryDirectory() as tmp,patch('credentials.active_secret',return_value='test-only'), \
                patch('worker.emit'),patch('providers.generate',side_effect=ProviderError('service','503')) as generate:
            with self.assertRaises(ProviderError):
                worker.translation(copy.deepcopy(p),{'segment_indices':[1]},Path(tmp))
            doc=read_checkpoint(Path(tmp)/'translation-checkpoint.json')
            self.assertEqual(validate_resume(doc,p,[1]),[])
            self.assertEqual(doc['rows'][0]['translated'],'Đã sửa tay')
            self.assertEqual(generate.call_count,1)
            with self.assertRaises(ValueError):validate_resume(doc,p,None)

    def test_first_batch_outage_does_not_repeat_pipeline_recognition(self):
        from core import probe,save_srt
        from subtitle_quality import coverage_record
        from translation_checkpoint import validate_pipeline_resume
        import shutil
        initial=project();initial['media']=probe(ROOT/'tests/fixtures/demo.mp4')
        initial['params'].update(pipeline_extract='asr',pipeline_translate=True,pipeline_tts=False,
                                 pipeline_clean=False,burn_subtitles=False,voice_gain=0,use_clean=False)
        recognized=copy.deepcopy(initial['segments']);initial['segments']=[]
        def asr(p,j,folder):
            save_srt(recognized,folder/'source.srt',translated=False)
            return {'type':'asr','files':['source.srt'],'segments':copy.deepcopy(recognized),
                    'coverage':coverage_record(p,'asr',0,p['media']['duration'])}
        def render(p,j,folder):
            shutil.copyfile(p['media']['path'],folder/'final.mp4')
            return {'type':'render','files':['final.mp4'],'primary_file':'final.mp4'}
        def recovered(provider,secret,model,prompt,**kwargs):
            lines=json.loads(prompt.split('Input:\n')[1])
            return json.dumps({'lines':[{'id':r['id'],'text':'Xin chào'} for r in lines]})
        with tempfile.TemporaryDirectory() as tmp,patch('credentials.active_secret',return_value='test-only'), \
                patch('worker.emit'),patch.dict(worker.TASKS,{'asr':asr,'render':render}):
            folder=Path(tmp)
            with patch('providers.generate',side_effect=ProviderError('service','503')):
                with self.assertRaises(ProviderError):worker.pipeline(copy.deepcopy(initial),{'id':'a'*32,'kind':'pipeline'},folder)
            doc=read_checkpoint(folder/'translation-checkpoint.json')
            self.assertEqual(doc['completed_indices'],[])
            self.assertEqual(validate_pipeline_resume(doc,initial)['segments'],recognized)
            with patch('providers.generate',side_effect=recovered), \
                    patch.dict(worker.TASKS,{'asr':unittest.mock.Mock(side_effect=AssertionError('recognition repeated'))}):
                result=worker.pipeline(copy.deepcopy(initial),{'id':'a'*32,'kind':'pipeline'},folder)
            self.assertEqual(result['reused'],['asr'])
            self.assertEqual(result['steps'],['translate','render'])

    def test_unconfirmed_cues_or_out_of_scope_completed_cues_cannot_resume(self):
        p=project();rows=copy.deepcopy(p['segments']);rows[0]['translated']='Xin chào'
        with tempfile.TemporaryDirectory() as tmp:
            doc=write_checkpoint(Path(tmp)/'saved.json',p,rows,[0])
            corrupt=copy.deepcopy(doc);corrupt['rows'][1]['text']='changed'
            with self.assertRaises(ValueError):validate_resume(corrupt,p)
            corrupt=copy.deepcopy(doc);corrupt['rows'][1]['translated']='Unconfirmed'
            with self.assertRaises(ValueError):validate_resume(corrupt,p)
            p['params']['translation_scope']='missing';p['segments'][0]['translated']='Manual'
            with self.assertRaises(ValueError):write_checkpoint(Path(tmp)/'saved.json',p,rows,[0])

    def test_partial_v2_and_legacy_require_exact_original_rows(self):
        p=project();rows=copy.deepcopy(p['segments']);rows[0]['translated']='Xin chào'
        with tempfile.TemporaryDirectory() as tmp:
            doc=write_checkpoint(Path(tmp)/'saved.json',p,rows,[0])
            self.assertEqual(validated_partial_rows(doc,p),rows)
            self.assertEqual(validated_partial_rows(rows,p),rows)
            malformed=copy.deepcopy(rows);malformed[1]['end']=4.
            with self.assertRaises(ValueError):validated_partial_rows(malformed,p)
            self.assertFalse(p['segments'][0]['translated'])

    def test_bounded_checkpoint_rejects_nonfinite_duplicate_and_bad_timing(self):
        p=project();rows=copy.deepcopy(p['segments']);rows[0]['translated']='Xin chào'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'saved.json';doc=write_checkpoint(path,p,rows,[0]);original=path.read_bytes()
            for text in ('{"rows":[],"rows":[]}', '{"rows":[],"updated_at":NaN}',
                         '{"rows":[],"updated_at":1e9999}', '['*2000+'0'+']'*2000,
                         json.dumps(doc | {'rows':[rows[0]|{'start':False},rows[1]]})):
                path.write_text(text,encoding='utf-8')
                with self.assertRaises(ValueError):read_checkpoint(path)
            path.write_bytes(original)
            with patch('translation_checkpoint.MAX_CHECKPOINT_BYTES',32),self.assertRaises(ValueError):read_checkpoint(path)
            self.assertEqual(path.read_bytes(),original)

    def test_vietnamese_words_and_chinese_names_are_not_currency_units(self):
        for text in ('Bạn đi đi, đừng đợi!','đây là lời thoại','đồng ý','元气少女'):
            with self.subTest(text=text):self.assertFalse(facts(text)['units'])
        self.assertFalse(review_translation('你别摇了','Đừng lắc nữa'))
        self.assertFalse(review_translation('Giá 199.000đ','Giá 199,000 VND'))
        self.assertFalse(review_translation('20公斤重量','Khối lượng 20kg'))
        self.assertTrue(review_translation('10元包邮','Giá 10 USD'))
        self.assertTrue(review_translation('Giảm 20%','Giảm 30%'))
        self.assertFalse(review_translation('100 đồng','100 VND'))


if __name__=='__main__':unittest.main()
