"""Owned offline voice dispatch and standalone narration; synthetic audio, no AI loading."""
import array
import copy
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import wave

from core import DATA, DEFAULTS
import worker


def write_signal(path, rate=48000, seconds=.2, silent=False):
    samples = array.array('h', (0 if silent else int(9000 * math.sin(i * 2 * math.pi * 230 / rate))
                                for i in range(round(rate * seconds))))
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


class SyntheticPiper:
    def __init__(self):
        self.calls = []

    def synthesize_wav(self, text, output, syn_config):
        self.calls.append((text, syn_config.length_scale))
        rate = 22050
        samples = array.array('h', (int(9000 * math.sin(i * 2 * math.pi * 180 / rate))
                                    for i in range(round(rate * .2 * syn_config.length_scale))))
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(samples.tobytes())


class UnifiedVoiceWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=DATA, prefix='unified-voice-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.params = DEFAULTS | {'voice_id': 'vieneu:Hải Đăng', 'voice_speed': 1,
                                  'tts_timing_mode': 'natural'}

    def synthesize(self, identifier, text, path, params, progress=None):
        self.assertEqual(identifier, 'vieneu:Hải Đăng')
        write_signal(path)
        return {'provider': 'vieneu', 'voice_id': identifier, 'voice_name': 'Hải Đăng'}

    def test_standalone_accepts_more_than_preview_limit_without_any_video_project(self):
        text = '[cười] ' + 'Xin chào bạn. ' * 50
        project = {'params': self.params, 'speech_text': text}
        with patch('voices.synthesize_text', side_effect=self.synthesize) as generate, patch('worker.emit'):
            result = worker.TASKS['text_to_speech'](project, {'sample_text': text}, self.folder)
        self.assertEqual(generate.call_args.args[1], text.strip())
        self.assertEqual(result['files'], ['speech.wav', 'speech.mp3'])
        self.assertEqual(result['primary_file'], 'speech.wav')
        self.assertEqual(result['type'], 'text_to_speech')
        self.assertEqual(result['provider'], 'vieneu')
        self.assertEqual(result['sample_rate'], 48000)
        self.assertAlmostEqual(result['duration'], .2, places=3)
        with wave.open(str(self.folder / 'speech.wav'), 'rb') as audio:
            self.assertEqual(audio.getframerate(), 48000)
        self.assertGreater((self.folder / 'speech.mp3').stat().st_size, 100)
        self.assertFalse(list(self.folder.glob('*.tmp.*')))

    def test_standalone_rejects_empty_and_oversized_text_before_loading_engine(self):
        with patch('voices.synthesize_text') as generate:
            for text in ('  ', 'a' * 10001):
                with self.subTest(length=len(text)), self.assertRaises(ValueError):
                    worker.text_to_speech({'params': self.params}, {'sample_text': text}, self.folder)
            generate.assert_not_called()
        self.assertFalse(list(self.folder.iterdir()))

    def test_standalone_encoder_failure_does_not_publish_partial_audio(self):
        with patch('voices.synthesize_text', side_effect=self.synthesize), patch('worker.emit'), \
             patch('worker.run', side_effect=RuntimeError('encoder failed')):
            with self.assertRaisesRegex(RuntimeError, 'encoder failed'):
                worker.text_to_speech({'params': self.params}, {'sample_text': 'Thử'}, self.folder)
        self.assertFalse(list(self.folder.iterdir()))

    def test_standalone_silence_fails_before_mp3_encoding(self):
        def silent(identifier, text, path, params, progress=None):
            write_signal(path, silent=True)
            return {}
        with patch('voices.synthesize_text', side_effect=silent), patch('worker.emit'), patch('worker.run') as encode:
            with self.assertRaisesRegex(ValueError, 'im lặng'):
                worker.text_to_speech({'params': self.params}, {'sample_text': 'Thử'}, self.folder)
            encode.assert_not_called()
        self.assertFalse(list(self.folder.iterdir()))

    def test_mixed_local_vieneu_and_piper_use_native_timeline_and_reuse_cache(self):
        suffix = uuid.uuid4().hex
        model, config = self.folder / 'piper.onnx', self.folder / 'piper.json'
        model.write_bytes(b'synthetic identity')
        config.write_text('{}', encoding='utf-8')
        local = {'id': 'vieneu:synthetic-' + suffix, 'name': 'VieNeu test', 'provider': 'vieneu',
                 'sample_rate': 48000, 'cache_identity': {'model': 'local-test', 'instance': suffix}}
        piper = {'id': 'piper:synthetic-' + suffix, 'name': 'Piper test', 'provider': 'piper',
                 'model': model, 'config': config, 'speaker_id': None}
        entries = {local['id']: local, piper['id']: piper}
        project = {'media': {'duration': 3}, 'params': self.params | {'voice_id': local['id']},
                   'segments': [{'start': .25, 'end': 1.25, 'text': 'One', 'translated': 'Lời một'},
                                {'start': 1.5, 'end': 2.5, 'text': 'Two', 'translated': 'Lời hai',
                                 'voice_id': piper['id']}]}
        piper_voice = SyntheticPiper()

        def resolve(identifier, require_ready=False):
            return entries[identifier]

        def local_synthesis(requests, params=None, progress=None):
            self.assertEqual(len(requests), 1)
            entry, text, path, rate = [requests[0][k] for k in ('entry','text','path','rate')]
            self.assertEqual(entry['provider'], 'vieneu')
            self.assertEqual(text, 'Lời một')
            self.assertEqual(rate, 1)
            write_signal(path)
            return [{'sample_rate': 48000}]

        folders = [self.folder / 'initial', self.folder / 'reuse']
        for folder in folders:
            folder.mkdir()
        with patch('voices.resolve_voice', side_effect=resolve), \
             patch('voices.synthesize_vieneu_batch', side_effect=local_synthesis) as generate, \
             patch('vieneu_voice.synthesis_identity', side_effect=lambda entry, params=None: entry['cache_identity']), \
             patch('voices.load_voice', return_value=(piper_voice, piper)), patch('worker.emit'):
            first = worker.tts(copy.deepcopy(project), {}, folders[0])
            second = worker.tts(copy.deepcopy(project), {}, folders[1])
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(len(piper_voice.calls), 1)
        self.assertEqual(first['cache_hits'], 0)
        self.assertEqual(second['cache_hits'], 2)
        self.assertEqual(first['sample_rate'], 48000)
        with wave.open(str(folders[0] / 'voice.wav'), 'rb') as audio:
            self.assertEqual(audio.getframerate(), 48000)
            self.assertEqual(audio.getnframes(), 3 * 48000)
            samples = array.array('h', audio.readframes(audio.getnframes()))
        self.assertTrue(any(abs(value) > 100 for value in samples[12000:24000]))
        self.assertTrue(any(abs(value) > 100 for value in samples[72000:84000]))

    def test_standalone_preflight_requires_text_and_local_voice_but_no_media(self):
        from preflight import inspect
        project = {'params': self.params, 'media': {'duration': 0}, 'segments': [], 'speech_text': 'Xin chào'}
        with patch('voices.resolve_voice', return_value={'id': 'vieneu:Hải Đăng', 'provider': 'vieneu'}) as resolve:
            check = inspect(project, 'text_to_speech')
            self.assertTrue(check['ok'], check)
            resolve.assert_called_with('vieneu:Hải Đăng', require_ready=True)
            project['speech_text'] = ''
            invalid = inspect(project, 'text_to_speech')
            self.assertFalse(invalid['ok'])
        self.assertFalse(any('CIT' in message or '127.0.0.1' in message for message in check['warnings']))

    def test_standalone_disk_forecast_includes_text_duration_at_native_rate(self):
        from resource_budget import estimate_space
        project = {'params': self.params, 'media': {'duration': 0}, 'speech_text': 'a' * 8000}
        space = estimate_space(project, 'text_to_speech')
        self.assertGreaterEqual(space['required_bytes'], 1000 * 48000 * 8 * 2)
