"""Style-only rendering reuses verified media; no inference or provider calls.

The project is assembled in memory. All files and artifact paths used below
live in a temporary directory; tests never read or write the user's database.
"""
import copy
from contextlib import ExitStack
from pathlib import Path
import shutil
import tempfile
import unittest
import wave
import uuid
from unittest.mock import patch

import core
import preflight
import resource_budget
import server
import worker
from subtitle_presets import PRESETS
from subtitle_quality import coverage_record


class StyleRerenderTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory(prefix='npk-style-rerender-')
        self.addCleanup(self.files.cleanup)
        self.data = Path(self.files.name)
        (self.data / 'jobs').mkdir()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        for module in (core, resource_budget, worker):
            self.patches.enter_context(patch.object(module, 'DATA', self.data))
        # A read-only fixture is copied before being used as the clean artifact.
        source = self.data / 'source.mp4'
        shutil.copyfile(core.ROOT / 'tests/fixtures/demo.mp4', source)
        self.project = {
            'id': 'style-fixture', 'name': 'Style fixture',
            'media': core.probe(source), 'regions': [], 'artifacts': {},
            'params': core.DEFAULTS | {
                'method': 'blur', 'use_clean': True, 'burn_subtitles': True,
                'video_encoder': 'cpu', 'original_gain': 0, 'voice_gain': 1,
                'bgm_gain': 0, 'voice_path': '', 'bgm_path': '',
                'pipeline_translate': False, 'pipeline_tts': False,
                'pipeline_clean': False,
            },
            'segments': [{'start': .3, 'end': 7.7, 'text': 'Source caption',
                          'translated': 'Xin chào bạn.'}],
        }
        self.project['coverage'] = coverage_record(self.project, 'asr', 0, 8)
        clean_folder = self.data / 'jobs' / uuid.uuid4().hex
        clean_folder.mkdir()
        shutil.copyfile(source, clean_folder / 'clean.mp4')
        speech_folder = self.data / 'jobs' / uuid.uuid4().hex
        speech_folder.mkdir()
        with wave.open(str(speech_folder / 'voice.wav'), 'wb') as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(48000)
            # Timing fixture only, not synthesized speech.
            output.writeframes(b'\x40\x00\xc0\xff' * (48000 * 4))
        self.project['artifacts'] = {
            'clean': {'job_id': clean_folder.name, 'signature': core.signature(self.project), 'files': ['clean.mp4']},
            'tts': {'job_id': speech_folder.name, 'speech_signature': worker.speech_signature(self.project), 'files': ['voice.wav']},
        }
        from job_progress import verify_outputs
        for kind, folder in (('clean', clean_folder), ('tts', speech_folder)):
            artifact = self.project['artifacts'][kind]
            artifact['verification'] = verify_outputs(artifact, self.project, {'kind': kind}, folder)

    def select_preset(self, preset):
        self.project['params'].update(server.validate_params({'subtitle_preset': preset}))

    def test_all_presets_keep_clean_and_speech_signatures_and_pass_render_preflight(self):
        clean = core.signature(self.project)
        speech = worker.speech_signature(self.project)
        for preset in PRESETS:
            with self.subTest(preset=preset):
                self.select_preset(preset)
                self.assertEqual(core.signature(self.project), clean)
                self.assertEqual(worker.speech_signature(self.project), speech)
                # Render must not query credentials or resolve an inference engine.
                with (patch('credentials.active_secret', side_effect=AssertionError('Provider accessed')),
                      patch('voices.resolve_voice', side_effect=AssertionError('Voice engine accessed'))):
                    result = preflight.inspect(self.project, 'render')
                self.assertTrue(result['ok'], result['errors'])

    def test_style_only_patch_preserves_legacy_missing_text_source(self):
        params = {key: value for key, value in self.project['params'].items()
                  if key not in ('tts_text_source', 'subtitle_preset')}
        self.project['params'] = core.restored_params(params)
        speech = worker.speech_signature(self.project)
        self.select_preset('highlight')
        self.assertNotIn('tts_text_source', self.project['params'])
        self.assertEqual(worker.speech_signature(self.project), speech)
        # A new explicit source is a real speech change even with identical words.
        self.project['params']['tts_text_source'] = 'translated'
        self.assertNotEqual(worker.speech_signature(self.project), speech)

    def test_real_cpu_render_reuses_clean_and_voice_without_recognition_translation_or_tts(self):
        self.select_preset('highlight')
        output = self.data / 'jobs' / 'style-render'
        output.mkdir()
        artifacts = copy.deepcopy(self.project['artifacts'])
        with ExitStack() as guarded:
            for name in ('ocr', 'asr', 'translation', 'tts', 'clean', 'pipeline'):
                guarded.enter_context(patch.object(worker, name,
                    side_effect=AssertionError('Unexpected expensive stage: ' + name)))
            guarded.enter_context(patch.object(worker, 'emit'))
            result = worker.render(self.project, {'kind': 'render'}, output)
        self.assertEqual(self.project['artifacts'], artifacts)
        self.assertEqual(result['type'], 'render')
        self.assertTrue(result['subtitles_burned'])
        self.assertEqual(result['encoder'], 'H.264 CPU')
        final = output / 'final.mp4'
        info = core.probe(final)
        self.assertAlmostEqual(info['duration'], 8, delta=.1)
        self.assertTrue(info['audio'])
        self.assertIn(r'\1c&H4DD8FF&', (output / 'subtitles.ass').read_text(encoding='utf-8'))
        core.run([core.binary('ffmpeg'), '-v', 'error', '-xerror', '-i', final,
                  '-f', 'null', '-'], timeout=40)
        from exports import publish_verified
        destination = self.data / 'exports'
        self.project['params'].update(export_directory=str(destination), export_name='Retained-video')
        first = publish_verified(self.project, output)
        old_bytes = Path(first['path']).read_bytes()
        second = publish_verified(self.project, output)
        self.assertEqual(Path(second['path']).name, 'Retained-video-2.mp4')
        self.assertEqual(Path(first['path']).read_bytes(), old_bytes)
        self.assertEqual(Path(second['path']).read_bytes(), old_bytes)

    def test_changed_voice_or_clean_geometry_is_rejected_before_rendering(self):
        self.select_preset('pop')
        self.project['segments'][0]['translated'] = 'Bản dịch đã thay đổi.'
        self.assertTrue(any('Giọng đã tạo không khớp' in message
                            for message in preflight.inspect(self.project, 'render')['errors']))
        self.project['segments'][0]['translated'] = 'Xin chào bạn.'
        self.project['regions'] = [{'x': 10, 'y': 10, 'w': 100, 'h': 30, 'start': 0, 'end': 8}]
        self.assertTrue(any('chưa có bản còn khớp' in message
                            for message in preflight.inspect(self.project, 'render')['errors']))

    def test_matching_signature_cannot_hide_a_missing_voice_file(self):
        self.select_preset('highlight')
        self.assertTrue(preflight.inspect(self.project, 'render')['ok'])
        voice = self.data / 'jobs' / self.project['artifacts']['tts']['job_id'] / 'voice.wav'
        voice.unlink()
        result = preflight.inspect(self.project, 'render')
        self.assertFalse(result['ok'])
        self.assertIn('Không còn tệp giọng đã tạo. Tạo lại lồng tiếng trước khi xuất.', result['errors'])
        # A pipeline which explicitly creates replacement speech is permitted.
        self.project['params']['pipeline_tts'] = True
        with patch('voices.resolve_voice', return_value={'id':'piper:fixture', 'provider':'piper', 'available': True}), \
             patch('voices.audio_identity', return_value=['fixture-admission-identity']):
            self.assertTrue(preflight.inspect(self.project, 'pipeline')['ok'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
