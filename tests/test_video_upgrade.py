import copy
import json
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

from core import DEFAULTS, ROOT
from translation_quality import ocr_same_content, review_translation, batch_context
from translation_checkpoint import (translation_signature, checkpoint_rows, read_checkpoint,
                                    validate_resume, write_checkpoint, make_pipeline_state,
                                    validate_pipeline_resume)


class VideoUpgradeTests(unittest.TestCase):
    def project(self):
        return {'params': DEFAULTS | {'translation_batch': 1, 'use_clean': False,
                                     'workload_mode': 'fast', 'asr_device': 'cpu'},
                'media': {'path': str(ROOT/'tests/fixtures/demo.mp4'), 'duration': 8,
                          'fps': 12, 'width': 320, 'height': 180, 'audio': True},
                'regions': [], 'artifacts': {},
                'segments': [{'start': 0, 'end': 2, 'text': 'Giá 199.000đ', 'translated': ''},
                             {'start': 3, 'end': 5, 'text': 'Giảm 20%', 'translated': ''}]}

    def test_ocr_changed_facts_never_merge(self):
        for a, b in [('Giá chỉ 199.000đ', 'Giá chỉ 299.000đ'),
                     ('Giảm ngay 20%', 'Giảm ngay 30%'),
                     ('Khối lượng 20kg', 'Khối lượng 20g'),
                     ('Dung tích 10l', 'Dung tích 10ml'),
                     ('Thời lượng 10s', 'Thời lượng 10h'),
                     ('Giá 10USD', 'Giá 10VND'),
                     ('Đời iPhone14 chính hãng', 'Đời iPhone15 chính hãng')]:
            with self.subTest(a=a, b=b):
                self.assertFalse(ocr_same_content(a, b))

    def test_ocr_still_accepts_nonfact_noise_and_grouping(self):
        self.assertTrue(ocr_same_content('Sản phẩm tuyệt vời!', 'Sản phẩm tuyệt vời'))
        self.assertTrue(ocr_same_content('Giá chỉ 199.000đ', 'Giá chỉ 199,000đ'))
        # A letter/diacritic change can alter a name or meaning. Keep both raw
        # versions; similarity alone is not permission to discard one.
        self.assertFalse(ocr_same_content('Xin chào các bạn hôm nay', 'Xin chào cac bạn hôm nay'))

    def test_worker_ocr_preserves_price_change(self):
        import numpy as np
        import worker
        project = self.project()
        project['media'].update(duration=1, fps=12)
        box = [[5,5], [150,5], [150,25], [5,25]]
        engine = unittest.mock.Mock(side_effect=[([(box, 'Giá chỉ 199.000đ', .99)], None),
                                                ([(box, 'Giá chỉ 299.000đ', .99)], None)])
        frame = np.zeros((180, 320, 3), dtype='uint8')
        with tempfile.TemporaryDirectory() as directory, \
                patch('worker.create_ocr', return_value=engine), \
                patch('worker.sampled_frames', return_value=iter([(0, frame), (.5, frame)])), \
                patch('worker.emit'):
            result = worker.ocr(project, {}, Path(directory))
        self.assertEqual([r['text'] for r in result['segments']],
                         ['Giá chỉ 199.000đ', 'Giá chỉ 299.000đ'])

    def test_checkpoint_atomic_failure_preserves_previous(self):
        project = self.project()
        rows = copy.deepcopy(project['segments'])
        rows[0]['translated'] = 'Giá 199.000đ'
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)/'translation-checkpoint.json'
            write_checkpoint(target, project, rows, [0])
            before = target.read_bytes()
            with patch('translation_checkpoint.os.fsync', side_effect=OSError('disk failure')):
                with self.assertRaises(OSError):
                    write_checkpoint(target, project, rows, [0])
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(Path(directory).glob('*.tmp')), [])
            self.assertEqual(validate_resume(read_checkpoint(target), project), [0])

    def test_checkpoint_exact_snapshot_scope_and_model(self):
        project = self.project()
        rows = copy.deepcopy(project['segments'])
        rows[0]['translated'] = 'Bản dịch'
        with tempfile.TemporaryDirectory() as directory:
            saved = write_checkpoint(Path(directory)/'checkpoint.json', project, rows, [0], [0])
            self.assertEqual(validate_resume(saved, project, [0]), [0])
            variants = []
            for key, value in [('translation_model', 'different-model'), ('glossary', 'A=B'),
                               ('target_language', 'English')]:
                changed = copy.deepcopy(project)
                changed['params'][key] = value
                variants.append(changed)
            changed = copy.deepcopy(project)
            changed['segments'][0]['translated'] = 'Sửa tay'
            variants.append(changed)
            for changed in variants:
                with self.assertRaises(ValueError):
                    validate_resume(saved, changed, [0])
            with self.assertRaises(ValueError):
                validate_resume(saved, project, [1])

    def test_old_checkpoint_remains_readable_but_not_auto_resumed(self):
        rows = self.project()['segments']
        self.assertEqual(checkpoint_rows(rows), rows)
        with self.assertRaises(ValueError):
            validate_resume(rows, self.project())

    def test_checkpoint_rejects_tampered_completed_rows(self):
        project = self.project()
        rows = copy.deepcopy(project['segments'])
        rows[0]['translated'] = 'Bản dịch'
        with tempfile.TemporaryDirectory() as directory:
            saved = write_checkpoint(Path(directory)/'checkpoint.json', project, rows, [0])
            saved['rows'][0]['text'] = 'Khác nội dung'
            with self.assertRaises(ValueError):
                validate_resume(saved, project)

    def test_translation_resume_calls_only_remaining_ids_and_uses_context(self):
        import worker
        from providers import ProviderError
        initial = self.project()
        attempts = []
        def interrupted(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            attempts.extend(r['id'] for r in lines)
            if lines[0]['id'] == 1:
                raise ProviderError('quota', 'simulated quota')
            return json.dumps({'lines':[{'id':0, 'text':'Giá 199.000đ'}]})
        prompts = []
        def remaining(provider, secret, model, prompt, **kwargs):
            prompts.append(prompt)
            lines = json.loads(prompt.split('Input:\n')[1])
            self.assertEqual([r['id'] for r in lines], [1])
            return json.dumps({'lines':[{'id':1, 'text':'Giảm 20%'}]})
        with tempfile.TemporaryDirectory() as directory, \
                patch('credentials.active_secret', return_value='fake'), patch('worker.emit'):
            folder = Path(directory)
            with patch('providers.generate', side_effect=interrupted):
                with self.assertRaises(ProviderError):
                    worker.translation(copy.deepcopy(initial), {}, folder)
            self.assertEqual(attempts, [0,1])
            with patch('providers.generate', side_effect=remaining):
                result = worker.translation(copy.deepcopy(initial), {}, folder)
            self.assertEqual(result['resumed_count'], 1)
            self.assertIn('Giá 199.000đ', prompts[0])
            self.assertEqual([r['translated'] for r in result['segments']], ['Giá 199.000đ', 'Giảm 20%'])
            self.assertEqual(read_checkpoint(folder/'translation-checkpoint.json')['completed_indices'], [0,1])

    def test_translation_scope_missing_preserves_manual_translation(self):
        import worker
        project = self.project()
        project['params']['translation_scope'] = 'missing'
        project['segments'][0]['translated'] = 'Bản tôi đã sửa tay'
        def generated(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            self.assertEqual([r['id'] for r in lines], [1])
            return json.dumps({'lines':[{'id':1, 'text':'Giảm 30%'}]})
        with tempfile.TemporaryDirectory() as directory, \
                patch('credentials.active_secret', return_value='fake'), \
                patch('providers.generate', side_effect=generated), patch('worker.emit'):
            result = worker.translation(project, {}, Path(directory))
        self.assertEqual(result['segments'][0]['translated'], 'Bản tôi đã sửa tay')
        self.assertTrue(any('Số/giá' in w for w in result['warnings']))
        self.assertEqual(result['segments'][1]['translated'], 'Giảm 30%')

    def test_translation_mismatch_fails_before_provider_call(self):
        import worker
        project = self.project()
        rows = copy.deepcopy(project['segments'])
        rows[0]['translated'] = 'Giá 199.000đ'
        with tempfile.TemporaryDirectory() as directory, \
                patch('credentials.active_secret', return_value='fake'), patch('providers.generate') as provider:
            folder = Path(directory)
            write_checkpoint(folder/'translation-checkpoint.json', project, rows, [0])
            project['segments'][0]['translated'] = 'Sửa tay mới'
            with self.assertRaises(ValueError):
                worker.translation(project, {}, folder)
            provider.assert_not_called()

    def test_pipeline_checkpoint_restores_recognition_without_redoing_translation(self):
        import worker
        initial = self.project()
        initial['params']['pipeline_extract'] = 'asr'
        initial['segments'] = []
        recognized = copy.deepcopy(initial)
        recognized['segments'] = self.project()['segments']
        recognized['coverage'] = {'method':'asr', 'analyzed_start':0, 'analyzed_end':8}
        state = make_pipeline_state(initial, recognized)
        rows = copy.deepcopy(recognized['segments'])
        rows[0]['translated'] = 'Giá 199.000đ'
        def generate(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            self.assertEqual([r['id'] for r in lines], [1])
            return json.dumps({'lines':[{'id':1,'text':'Giảm 20%'}]})
        with tempfile.TemporaryDirectory() as directory, \
                patch('credentials.active_secret', return_value='fake'), \
                patch('providers.generate', side_effect=generate), patch('worker.emit'):
            folder = Path(directory)
            saved = write_checkpoint(folder/'translation-checkpoint.json', recognized, rows, [0],
                                     pipeline_state=state)
            recovered = validate_pipeline_resume(saved, initial)
            self.assertEqual(recovered['coverage'], recognized['coverage'])
            self.assertEqual(recovered['segments'][0]['translated'], '')
            result = worker.translation(recovered, {'translation_pipeline_state':state}, folder)
            self.assertEqual(result['resumed_count'], 1)
            self.assertEqual(read_checkpoint(folder/'translation-checkpoint.json')['pipeline_state'], state)

    def test_pipeline_checkpoint_rejects_changed_recognition_inputs(self):
        initial = self.project()
        initial['params']['pipeline_extract'] = 'ocr'
        recognized = copy.deepcopy(initial)
        rows = copy.deepcopy(recognized['segments'])
        rows[0]['translated'] = 'Giá 199.000đ'
        with tempfile.TemporaryDirectory() as directory:
            saved = write_checkpoint(Path(directory)/'checkpoint.json', recognized, rows, [0],
                                     pipeline_state=make_pipeline_state(initial, recognized))
            for params in ({'ocr_fps':8}, {'extraction_start':1}, {'target_language':'English'}):
                changed = copy.deepcopy(initial)
                changed['params'].update(params)
                with self.assertRaises(ValueError):
                    validate_pipeline_resume(saved, changed)
            saved['pipeline_state']['recognition_snapshot']['segments'][0]['text'] = 'Tampered'
            with self.assertRaises(ValueError):
                validate_pipeline_resume(saved, initial)

    def test_pipeline_integration_resumes_extraction_and_translation_with_real_outputs(self):
        import worker
        import shutil
        from core import probe, save_srt
        from subtitle_quality import coverage_record
        from providers import ProviderError
        initial = self.project()
        initial['media'] = probe(ROOT/'tests/fixtures/demo.mp4')
        initial['params'].update(pipeline_extract='asr',pipeline_translate=True,pipeline_tts=False,
                                 pipeline_clean=False,burn_subtitles=False,voice_gain=0,use_clean=False)
        initial['segments'] = []
        recognized = self.project()['segments']
        def fake_asr(project,job,folder):
            save_srt(recognized,folder/'source.srt',translated=False)
            return {'type':'asr','files':['source.srt'],'segments':copy.deepcopy(recognized),
                    'coverage':coverage_record(project,'asr',0,project['media']['duration'])}
        def fake_render(project,job,folder):
            shutil.copyfile(project['media']['path'],folder/'final.mp4')
            return {'type':'render','files':['final.mp4'],'primary_file':'final.mp4',
                    'subtitles_burned':False,'subtitle_count':0}
        def interrupted(provider,secret,model,prompt,**kwargs):
            lines=json.loads(prompt.split('Input:\n')[1])
            if lines[0]['id']==1:
                raise ProviderError('quota','simulated')
            return json.dumps({'lines':[{'id':0,'text':'Giá 199.000đ'}]})
        def remaining(provider,secret,model,prompt,**kwargs):
            self.assertEqual([r['id'] for r in json.loads(prompt.split('Input:\n')[1])],[1])
            return json.dumps({'lines':[{'id':1,'text':'Giảm 20%'}]})
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'), \
                patch('worker.emit'), patch.dict(worker.TASKS,{'asr':fake_asr,'render':fake_render}):
            folder=Path(directory)
            with patch('providers.generate',side_effect=interrupted):
                with self.assertRaises(ProviderError):
                    worker.pipeline(copy.deepcopy(initial),{'id':'f'*32,'kind':'pipeline'},folder)
            with patch('providers.generate',side_effect=remaining), \
                    patch.dict(worker.TASKS,{'asr':unittest.mock.Mock(side_effect=AssertionError('ASR repeated'))}):
                result=worker.pipeline(copy.deepcopy(initial),{'id':'e'*32,'kind':'pipeline'},folder)
            self.assertIn('asr',result['reused'])
            self.assertEqual(result['steps'],['translate','render'])
            self.assertTrue((folder/'final.mp4').is_file())

    def test_translation_review_is_warning_only_and_checks_glossary(self):
        warnings = review_translation('Acme chỉ 199.000đ, giảm 20%', 'Acme giá 299 USD', 'Acme=Nhãn Acme')
        self.assertEqual(len(warnings), 3)
        self.assertFalse(review_translation('Giá 199.000đ', 'Giá 199,000 VND'))

    def test_context_is_valid_bounded_json_and_no_input_replacement(self):
        rows = [{'text':'\\\n'*300, 'translated':'Lời dịch'} for _ in range(20)]
        context = batch_context(rows, [8,9], list(range(8)))
        self.assertLessEqual(len(context), 2800)
        self.assertIsInstance(json.loads(context), dict)

    def test_asr_retains_absolute_word_times(self):
        import worker
        project = self.project()
        project['params'].update(extraction_start=5, extraction_end=7)
        project['media'].update(audio_duration=8.,audio_start_time=0.)
        model = unittest.mock.Mock()
        model.transcribe.return_value = (iter([types.SimpleNamespace(start=0, end=1, text=' Xin chào',
            words=[types.SimpleNamespace(start=.1, end=.4, word=' Xin', probability=.95),
                   types.SimpleNamespace(start=.4, end=.8, word=' chào', probability=.9)])]),
            types.SimpleNamespace(duration=2, language='vi'))
        def decode(command, **kwargs):
            with wave.open(str(command[-1]), 'wb') as wav:
                wav.setparams((1,2,16000,0,'NONE','not compressed'))
                wav.writeframes(b'\x00\x00'*32000)
        fake = types.ModuleType('faster_whisper')
        fake.WhisperModel = unittest.mock.Mock(return_value=model)
        with tempfile.TemporaryDirectory() as directory, patch.dict('sys.modules', {'faster_whisper':fake}), \
                patch('worker.run', side_effect=decode), patch('worker.emit'):
            result = worker.asr(project, {}, Path(directory))
        self.assertTrue(model.transcribe.call_args.kwargs['word_timestamps'])
        self.assertEqual(result['segments'][0]['words'][0]['start'], 5.1)
        self.assertEqual(result['segments'][0]['words'][1]['end'], 5.8)

    def test_scan_uses_sequential_decoder_no_opencv_seeks(self):
        import worker
        import cv2
        import numpy as np
        project = self.project()
        project['media'].update(duration=1)
        project['regions'] = [{'x':0,'y':0,'w':320,'h':180,'start':0,'end':1}]
        frame = np.zeros((180,320,3), dtype='uint8')
        engine = unittest.mock.Mock(return_value=([], None))
        with tempfile.TemporaryDirectory() as directory, \
                patch('worker.create_ocr', return_value=engine), patch('worker.emit'), \
                patch('worker.sampled_frames', return_value=iter([(0,frame),(.5,frame)])) as samples, \
                patch.object(cv2, 'VideoCapture', side_effect=AssertionError('random seek path')):
            result = worker.scan(project, {}, Path(directory))
        self.assertEqual(result['samples'], 2)
        self.assertEqual(result['empty_samples'], 2)
        samples.assert_called_once()

    def test_scan_decode_failure_is_not_published_as_complete(self):
        import worker
        import numpy as np
        project = self.project()
        project['media'].update(duration=1)
        project['regions'] = [{'x':0,'y':0,'w':320,'h':180,'start':0,'end':1}]
        def broken():
            yield 0, np.zeros((180,320,3), dtype='uint8')
            raise RuntimeError('damaged source')
        with tempfile.TemporaryDirectory() as directory, \
                patch('worker.create_ocr', return_value=lambda frame:([],None)), \
                patch('worker.sampled_frames', return_value=broken()), patch('worker.emit'):
            folder = Path(directory)
            with self.assertRaises(RuntimeError):
                worker.scan(project, {}, folder)
            self.assertFalse((folder/'masks.json').exists())

    def test_clean_respects_encoder_selection_and_auto_cpu_fallback(self):
        import worker
        from core import probe, run, binary
        project = self.project()
        project['media'] = probe(ROOT/'tests/fixtures/demo.mp4')
        project['params'].update(method='telea', video_quality=24)
        project['regions'] = [{'x':10,'y':10,'w':30,'h':20,'start':0,'end':8}]
        with tempfile.TemporaryDirectory() as directory, \
                patch('encoding.nvenc_ready', return_value=False), patch('worker.emit'):
            for mode in ('cpu', 'auto'):
                folder = Path(directory)/mode
                folder.mkdir()
                project['params']['video_encoder'] = mode
                result = worker.clean(project, {'kind':'clean'}, folder)
                self.assertEqual(result['encoder'], 'H.264 CPU')
                self.assertGreater(result['modified_frames'], 0)
                output = probe(folder/'clean.mp4')
                self.assertAlmostEqual(output['duration'], project['media']['duration'], delta=.3)
                run([binary('ffmpeg'), '-v','error','-xerror','-i',folder/'clean.mp4','-f','null','-'], timeout=30)


if __name__ == '__main__':
    unittest.main()
