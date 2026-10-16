"""Worker speech-role persistence and cache parity with synthetic PCM only."""
import array
import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

from core import DATA, DEFAULTS, prepare_project_text
from speech_delivery import effective_speech_rows
from speech_preparation import cache_items
import worker


class SyntheticVoice:
    def __init__(self):
        self.calls = []

    def synthesize_wav(self, text, output, syn_config):
        self.calls.append(text)
        count = round(.3 * syn_config.length_scale * 22050)
        samples = array.array('h', (int(8000 * math.sin(index * 2 * math.pi * 220 / 22050))
                                   for index in range(count)))
        output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
        output.writeframes(samples.tobytes())


class SpeechCleanupWorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=DATA, prefix='cleanup-worker-')
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        model = self.folder / 'fixture.onnx'
        config = self.folder / 'fixture.onnx.json'
        model.write_bytes(b'identity-only-no-runtime-model')
        config.write_text('{}', encoding='utf-8')
        self.entry = {'id': 'piper:fixture-' + uuid.uuid4().hex, 'name': 'Synthetic fixture',
                      'provider': 'piper', 'model': model, 'config': config, 'speaker_id': None}
        self.voice = SyntheticVoice()

    def project(self):
        rows = [dict(id='caption-first', start=0, end=1.5, text='但底下却是另一个世界',
                     translated='Nhưng bên dưới là một thế giới khác.', voice_id=self.entry['id']),
                dict(id='caption-second', start=1.6, end=3, text='但底下却是另一个世界 @Account',
                     translated='Nhưng bên dưới là một thế giới khác. @Account', voice_id=self.entry['id']),
                dict(id='caption-third', start=3.1, end=4.5, text='但底下却是另一个世界',
                     translated='Nhưng bên dưới là một thế giới khác.', voice_id=self.entry['id']),
                dict(id='caption-credit', start=5, end=6, text='视频来源: @Publisher',
                     translated='Nguồn video: @Publisher', voice_id='piper:unavailable-credit')]
        return {'id': uuid.uuid4().hex, 'media': {'duration': 8}, 'artifacts': {},
                'segments': rows, 'params': DEFAULTS | {'subtitle_text_cleanup': True,
                    'tts_text_source': 'translated', 'tts_timing_mode': 'natural',
                    'voice_id': 'piper:unavailable-global-default'}}

    def run_tts(self, project, name):
        folder = self.folder / name
        folder.mkdir()
        with patch('voices.resolve_voice', return_value=self.entry) as resolve, \
                patch('voices.load_voice', return_value=(self.voice, self.entry)) as load, \
                patch('worker.emit'):
            result = worker.tts(project, {'id': name}, folder)
        return result, folder, resolve, load

    def test_tts_keeps_caption_identity_and_only_synthesizes_retained_dialogue(self):
        project = self.project()
        before = copy.deepcopy(project['segments'])
        result, folder, resolve, load = self.run_tts(project, 'first')
        self.assertEqual(self.voice.calls, ['Nhưng bên dưới là một thế giới khác.'])
        resolve.assert_called_once_with(self.entry['id'], require_ready=True)
        load.assert_called_once()
        self.assertEqual(result['generated'], 1)
        self.assertEqual([cue.get('speech_enabled', True) for cue in project['segments']],
                         [True, False, False, False])
        self.assertEqual(result['segments'], project['segments'])
        for original, prepared in zip(before, result['segments']):
            for field in ('id', 'start', 'end', 'text', 'translated', 'voice_id'):
                self.assertEqual(original[field], prepared[field])
        self.assertIn('text-preparation-speech.json', result['files'])
        report = json.loads((folder / 'text-preparation-speech.json').read_text(encoding='utf-8'))
        self.assertEqual(report['text_source'], 'translated')
        self.assertEqual(report['input_count'], report['output_count'])
        self.assertEqual([cue['id'] for cue in report['segments']], [cue['id'] for cue in before])
        self.assertEqual(sum(change['kind'] == 'duplicate_speech' for change in report['changes']), 2)
        self.assertTrue(any(change['kind'] == 'credit_only' for change in report['changes']))
        self.assertTrue((folder / 'voice.wav').exists())

    def test_second_run_reuses_only_cleaned_retained_raw_clip(self):
        project = self.project()
        first, _, _, _ = self.run_tts(project, 'first')
        second, _, resolve, load = self.run_tts(project, 'second')
        self.assertEqual(first['speech_signature'], second['speech_signature'])
        self.assertEqual(len(self.voice.calls), 1)
        load.assert_not_called()
        resolve.assert_called_once_with(self.entry['id'], require_ready=True)
        self.assertEqual(second['generated'], 0)
        self.assertEqual(second['cache_hits'], 1)
        with patch('voices.resolve_voice', return_value=self.entry):
            items = cache_items(project)
        self.assertEqual([bool(item['key']) for item in items], [True, False, False, False])
        self.assertEqual(sum(item['cache_hit'] for item in items), 1)

    def test_signature_ignores_a_credit_only_unavailable_voice_identity(self):
        project = self.project()
        project['params']['pronunciation_dictionary'] = 'NPK=Nờ pê ca'
        with patch('voices.resolve_voice', return_value=self.entry) as resolve, \
                patch('voices.audio_identity', return_value=['fixture']) as identity:
            before = worker.speech_signature(project)
            project['segments'] = effective_speech_rows(project['segments'], project['params'])
            after = worker.speech_signature(project)
        self.assertEqual(before, after)
        self.assertEqual(resolve.call_count, 2)
        self.assertTrue(all(call.args == (self.entry['id'],) for call in resolve.call_args_list))
        self.assertEqual(identity.call_count, 2)

    def test_missing_translation_stops_before_resolving_or_loading_voice(self):
        project = self.project()
        project['segments'] = [project['segments'][0] | {'translated': ''}]
        folder = self.folder / 'missing'
        folder.mkdir()
        with patch('voices.resolve_voice') as resolve, patch('voices.load_voice') as load:
            with self.assertRaisesRegex(ValueError, 'thiếu nội dung'):
                worker.tts(project, {'id': 'missing'}, folder)
        resolve.assert_not_called()
        load.assert_not_called()
        self.assertTrue((folder / 'text-preparation-speech.json').exists())
        self.assertFalse((folder / 'voice.wav').exists())

    def test_core_speech_report_respects_selected_source(self):
        project = self.project()
        project['params']['tts_text_source'] = 'source'
        project['segments'] = [
            dict(id='first', start=0, end=2, text='Bầu trời hôm nay trong xanh.', translated='Same translation.'),
            dict(id='second', start=2.1, end=4, text='Người thợ xây bước vào nhà.', translated='Same translation.')]
        before = copy.deepcopy(project)
        prepared, report = prepare_project_text(project, stage='speech')
        self.assertEqual(report['text_source'], 'source')
        self.assertEqual(report['changes'], [])
        self.assertTrue(all(cue.get('speech_enabled', True) for cue in prepared['segments']))
        self.assertEqual(project, before)

    def inspect_preflight(self, project, kind='tts'):
        from preflight import inspect
        source = self.folder / 'source-identity.mp4'
        source.write_bytes(b'synthetic identity only; no media decoder is needed for admission')
        project['media'].update(path=str(source), width=1280, height=720, fps=30, audio=True)
        project['params'].update(pipeline_extract='existing', pipeline_translate=False,
                                 pipeline_tts=True, pipeline_clean=False, use_clean=False,
                                 subtitle_automation='manual')
        before = copy.deepcopy(project)

        def resolve(identifier, **kwargs):
            if identifier != self.entry['id']:
                raise ValueError('Unavailable fixture voice must not be resolved: ' + identifier)
            return self.entry

        with patch('voices.resolve_voice', side_effect=resolve) as resolver, \
                patch('speech_preparation.measured_duration', return_value=None), \
                patch('credentials.active_secret', side_effect=AssertionError('No provider required')), \
                patch('subtitle_quality.inspect_subtitles', return_value={
                    'errors': [], 'issues': [], 'requires_review': False}) as captions:
            result = inspect(project, kind)
        self.assertEqual(project, before)
        return result, resolver, captions

    def test_preflight_skips_voice_of_credit_only_caption(self):
        project = self.project()
        result, resolver, _ = self.inspect_preflight(project)
        self.assertTrue(result['ok'], result['errors'])
        self.assertTrue(resolver.call_args_list)
        self.assertTrue(all(call.args == (self.entry['id'],) for call in resolver.call_args_list))
        self.assertEqual(result['delivery']['cue_count'], 1)

    def test_preflight_ignores_exhausted_pause_of_suppressed_duplicate(self):
        project = self.project()
        # Matching reservations remain duplicate-compatible, but the short
        # duplicate cannot accommodate them. Only the kept cue needs a window.
        project['segments'][0].update(end=2, pause_before_ms=800, pause_after_ms=800)
        project['segments'][1].update(start=2.1, end=2.6, pause_before_ms=800, pause_after_ms=800)
        project['segments'] = project['segments'][:2]
        result, _, _ = self.inspect_preflight(project)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['delivery']['cue_count'], 1)

    def test_pipeline_caption_qa_retains_the_complete_authored_table(self):
        project = self.project()
        result, _, captions = self.inspect_preflight(project, 'pipeline')
        self.assertTrue(result['ok'], result['errors'])
        self.assertTrue(captions.called)
        self.assertEqual(captions.call_args.args[0]['segments'], project['segments'])
        self.assertEqual(len(captions.call_args.args[0]['segments']), 4)


if __name__ == '__main__':
    unittest.main()
