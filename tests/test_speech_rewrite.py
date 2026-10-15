import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import DEFAULTS
from speech_rewrite import optimize_spoken
from providers import ProviderError


class SpeechRewriteTests(unittest.TestCase):
    def project(self):
        return {'params':DEFAULTS | {'translation_batch':1},
                'segments':[{'start':0,'end':3,'text':'Giá chỉ 199.000đ',
                             'translated':'Sản phẩm có giá 199.000đ','spoken_text':'Lời tôi đang dùng 199.000đ',
                             'speech_rate':.95,'pause_before_ms':100,'voice_id':'vieneu:Hải Đăng'},
                            {'start':3,'end':6,'text':'Giảm 20%','translated':'Ưu đãi giảm 20%'}]}

    def test_only_spoken_text_changes_and_requires_review(self):
        project = self.project()
        original = copy.deepcopy(project)
        def generate(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            return json.dumps({'lines':[{'id':r['id'],'spoken_text':'Chỉ 199.000đ' if r['id']==0 else 'Giảm 20%'} for r in lines]})
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',side_effect=generate):
            result = optimize_spoken(project,{},Path(directory))
        self.assertEqual(project,original)
        self.assertTrue(result['review_required'])
        for before, after in zip(original['segments'],result['segments']):
            self.assertEqual({k:v for k,v in before.items() if k!='spoken_text'},
                             {k:v for k,v in after.items() if k!='spoken_text'})
        self.assertEqual(result['segments'][0]['spoken_text'],'Chỉ 199.000đ')
        self.assertEqual(result['suggestions'][0]['original_spoken_text'],original['segments'][0]['spoken_text'])
        self.assertEqual(result['suggestions'][1]['original_spoken_text'],'')

    def test_numeric_change_is_rejected_per_cue_not_applied(self):
        project = self.project()
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',return_value=json.dumps({'lines':[{'id':0,'spoken_text':'Chỉ 299.000đ'}]})):
            result = optimize_spoken(project,{'segment_indices':[0]},Path(directory))
        self.assertFalse(result['suggestions'][0]['accepted'])
        self.assertEqual(result['segments'][0]['spoken_text'],project['segments'][0]['spoken_text'])
        self.assertEqual(result['segments'][1],project['segments'][1])

    def test_overbudget_is_rejected_without_truncating(self):
        project = self.project()
        project['segments'][0]['end'] = .5
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',return_value=json.dumps({'lines':[{'id':0,'spoken_text':'Sản phẩm chỉ có giá 199.000đ'}]})):
            result = optimize_spoken(project,{'segment_indices':[0]},Path(directory))
        self.assertFalse(result['suggestions'][0]['accepted'])
        self.assertIn('ngân sách',result['warnings'][0])

    def test_checkpoint_resumes_only_remaining_without_key_rotation(self):
        project = self.project()
        ids = []
        def generated(provider,secret,model,prompt,**kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            ids.extend(r['id'] for r in lines)
            if lines[0]['id']==1:
                raise ProviderError('quota','simulated')
            return json.dumps({'lines':[{'id':0,'spoken_text':'Chỉ 199.000đ'}]})
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'):
            folder=Path(directory)
            with patch('providers.generate',side_effect=generated):
                with self.assertRaises(ProviderError):
                    optimize_spoken(project,{},folder)
            self.assertEqual(ids,[0,1])
            self.assertTrue((folder/'speech-rewrite-checkpoint.json').is_file())
            with patch('providers.generate',return_value=json.dumps({'lines':[{'id':1,'spoken_text':'Giảm 20%'}]})) as provider:
                result=optimize_spoken(project,{},folder)
            self.assertEqual(provider.call_count,1)
            self.assertEqual(result['resumed_count'],1)
            self.assertEqual([s['index'] for s in result['suggestions']],[0,1])

    def test_manual_edit_invalidates_checkpoint_before_provider(self):
        project = self.project()
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'):
            folder=Path(directory)
            with patch('providers.generate',return_value=json.dumps({'lines':[{'id':0,'spoken_text':'Chỉ 199.000đ'}]})):
                optimize_spoken(project,{'segment_indices':[0]},folder)
            project['segments'][0]['spoken_text']='Tôi vừa sửa tay'
            with patch('providers.generate') as provider:
                with self.assertRaises(ValueError):
                    optimize_spoken(project,{'segment_indices':[0]},folder)
                provider.assert_not_called()

    def test_model_malformed_output_is_retried_but_never_applied(self):
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',return_value='{"lines":[{"id":0,"spoken_text":null}]}') as provider, \
                patch('speech_rewrite.time.sleep'):
            with self.assertRaises(ValueError):
                optimize_spoken(self.project(),{'segment_indices':[0]},Path(directory))
        self.assertEqual(provider.call_count,3)

    def test_no_key_fails_without_api_call(self):
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value=None), \
                patch('providers.generate') as provider:
            with self.assertRaises(ValueError):
                optimize_spoken(self.project(),{},Path(directory))
            provider.assert_not_called()

    def checkpoint_fixture(self, directory):
        project=self.project()
        with patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',return_value='{"lines":[{"id":0,"spoken_text":"Chỉ 199.000đ"}]}'):
            optimize_spoken(project,{'segment_indices':[0]},Path(directory))
        path=Path(directory)/'speech-rewrite-checkpoint.json'
        return project,path,json.loads(path.read_text(encoding='utf-8'))

    def assert_checkpoint_rejected(self, project, path):
        before=copy.deepcopy(project)
        payload=path.read_bytes()
        with patch('credentials.active_secret',return_value='fake'),patch('providers.generate') as provider:
            with self.assertRaises(ValueError):
                optimize_spoken(project,{'segment_indices':[0]},path.parent)
            provider.assert_not_called()
        self.assertEqual(project,before)
        self.assertEqual(path.read_bytes(),payload)

    def test_corrupt_checkpoint_types_scope_and_rows_fail_before_provider(self):
        mutations=[
            lambda d:d['suggestions'][0].update(accepted='false'),
            lambda d:d['suggestions'][0].update(accepted=1),
            lambda d:d.update(completed_indices=[0,0]),
            lambda d:d.update(completed_indices=[False]),
            lambda d:d['suggestions'].append(copy.deepcopy(d['suggestions'][0])),
            lambda d:d['suggestions'][0].update(index=False),
            lambda d:d['suggestions'][0].update(index=1),
            lambda d:d['suggestions'][0].update(spoken_text=None),
            lambda d:d['suggestions'][0].update(spoken_text=['bad']),
            lambda d:d['suggestions'][0].update(spoken_text='Chỉ 299.000đ'),
            lambda d:d['suggestions'][0].update(spoken_text='bad\x00text'),
            lambda d:d['suggestions'][0].update(max_words=999),
            lambda d:d['suggestions'][0].update(original_spoken_text='Changed'),
            lambda d:d['rows'][1].update(translated='Outside selected scope'),
            lambda d:d.update(warnings={'bad':'shape'}),
            lambda d:d.update(suggestions=[]),
            lambda d:d.update(schema_version=True),
        ]
        with tempfile.TemporaryDirectory() as directory:
            project,path,original=self.checkpoint_fixture(directory)
            for index,mutate in enumerate(mutations):
                with self.subTest(case=index):
                    document=copy.deepcopy(original);mutate(document)
                    path.write_text(json.dumps(document,ensure_ascii=False),encoding='utf-8')
                    self.assert_checkpoint_rejected(project,path)

    def test_checkpoint_duplicate_nonfinite_deep_unicode_and_size_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            project,path,original=self.checkpoint_fixture(directory)
            text=json.dumps(original,ensure_ascii=False)
            corruptions=[text.replace('"accepted": true','"accepted": false,"accepted": true'),
                         text[:-1]+',"bad":NaN}',text[:-1]+',"bad":1e9999}',
                         '['*2000+'0'+']'*2000]
            for content in corruptions:
                path.write_text(content,encoding='utf-8')
                self.assert_checkpoint_rejected(project,path)
            path.write_bytes(b'\xff invalid UTF-8')
            self.assert_checkpoint_rejected(project,path)
            path.write_text(text,encoding='utf-8')
            with patch('speech_rewrite.MAX_REWRITE_CHECKPOINT_BYTES',100):
                self.assert_checkpoint_rejected(project,path)

    def test_duplicate_generated_ids_never_create_a_suggestion_file(self):
        project=self.project();before=copy.deepcopy(project)
        text='{"lines":[{"id":1,"id":0,"spoken_text":"Chỉ 199.000đ"}]}'
        with tempfile.TemporaryDirectory() as directory,patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',return_value=text) as provider,patch('speech_rewrite.time.sleep'):
            with self.assertRaises(ValueError):
                optimize_spoken(project,{'segment_indices':[0]},Path(directory))
            self.assertEqual(provider.call_count,3)
            self.assertFalse((Path(directory)/'speech-suggestions.json').exists())
        self.assertEqual(project,before)

    def test_valid_rejected_proposal_checkpoint_remains_resumable(self):
        project=self.project()
        with tempfile.TemporaryDirectory() as directory,patch('credentials.active_secret',return_value='fake'):
            folder=Path(directory)
            with patch('providers.generate',return_value='{"lines":[{"id":0,"spoken_text":"Chỉ 299.000đ"}]}'):
                first=optimize_spoken(project,{'segment_indices':[0]},folder)
            with patch('providers.generate') as provider:
                resumed=optimize_spoken(project,{'segment_indices':[0]},folder)
                provider.assert_not_called()
        self.assertFalse(first['suggestions'][0]['accepted'])
        self.assertEqual(resumed['resumed_count'],1)
        self.assertEqual(resumed['segments'],project['segments'])


if __name__ == '__main__':
    unittest.main()
