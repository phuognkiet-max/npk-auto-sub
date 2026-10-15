"""Independent VieNeu discovery, cache safety, chunking and WAV assembly.

No model downloads, CUDA inference or application startup in these tests.
"""
from __future__ import annotations
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

import vieneu_voice as voices

_spec = importlib.util.spec_from_file_location('npk_vieneu_engine_test',
    Path(__file__).resolve().parents[1] / 'tools/vieneu_engine.py')
engine = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(engine)


def wav(path, frames=48000, rate=48000, channels=1, width=2):
    with wave.open(str(path), 'wb') as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        stream.writeframes(struct.pack('<h', 1234) * frames * channels if width == 2 else b'\x7f' * frames * channels)


def alter(path, text):
    old = path.stat().st_mtime_ns
    path.write_text(text, encoding='utf-8')
    os.utime(path, ns=(old + 10000000, old + 10000000))


class DeliveryTests(unittest.TestCase):
    def test_pause_units_and_defaults(self):
        self.assertEqual(engine.parse_delivery('[nghỉ 250ms] Một [pause] hai [nghi 0,5 giây] ba.'),
            [('pause', .25), ('speech', 'Một'), ('pause', 1), ('speech', 'hai'), ('pause', .5), ('speech', 'ba.')])
        self.assertEqual(engine.parse_delivery('Một [nghỉ 0s] hai'),
            [('speech', 'Một'), ('pause', 0), ('speech', 'hai')])

    def test_bad_and_empty_delivery_rejected(self):
        for text in ('[nghỉ 11s] Xin chào', 'Xin [nghỉ -1s] chào', '[pause]', '[nghỉ 1s]', '[hét]', '[cười] [hét]'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                engine.parse_delivery(text)

    def test_supported_nonverbal_only_cues_and_silence_are_real_inputs(self):
        for text in ('[cười]', '[thở dài]', '[hắng giọng]', '[cười] [cười]!'):
            with self.subTest(text=text):
                self.assertEqual(engine.parse_delivery(text), [('speech', text)])
        self.assertEqual(engine.parse_delivery('[nghỉ 250ms] [cười]'), [('pause', .25), ('speech', '[cười]')])

    def test_long_speech_keeps_every_word_in_bounded_chunks(self):
        text = ' '.join('từ' + str(index) for index in range(1000))
        chunks = engine.chunk_speech(text)
        self.assertGreater(len(chunks), 25)
        self.assertEqual(' '.join(chunks), text)
        self.assertTrue(all(len(chunk.split()) <= 36 and len(chunk) <= 240 for chunk in chunks))

    def test_chunking_preserves_emotion_and_manual_pause(self):
        first = ' '.join(['Xin'] * 40) + ' [thở dài] chào.'
        parts = engine.expanded_delivery(first + ' [nghỉ 2s] Hẹn gặp lại.')
        self.assertEqual([value for kind, value in parts if kind == 'pause'], [.15, 2])
        self.assertIn('[thở dài]', ' '.join(value for kind, value in parts if kind == 'speech'))

    def test_only_supported_emotions_become_model_tokens(self):
        fake = types.SimpleNamespace(punc_norm=lambda text: text)
        pipeline = types.SimpleNamespace(run=lambda text, **kwargs: text.strip())
        with patch.dict(sys.modules, {'sea_g2p': fake}), patch.object(engine, '_pipeline', return_value=pipeline):
            actual = engine.phonemize('Xin [cười] chào [thở dài] bạn [hắng giọng].')
        self.assertEqual(actual, 'Xin <|emotion_1|> chào <|emotion_2|> bạn <|emotion_3|>.')


class CatalogFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='npk-vieneu-test-')
        self.root = Path(self.temporary.name)
        self.models = self.root / 'models/vieneu-v3-turbo'
        self.runtime = self.root / 'runtime'
        self.models.mkdir(parents=True)
        (self.runtime / 'vieneu/Scripts').mkdir(parents=True)
        (self.runtime / 'vieneu/Scripts/python.exe').write_bytes(b'owned python fixture')
        (self.runtime / 'audio-cpp/cpu/bin').mkdir(parents=True)
        (self.runtime / 'audio-cpp/cpu/bin/audiocpp_cli.exe').write_bytes(b'owned native fixture')
        (self.models / 'model.gguf').write_bytes(b'GGUF fixture')
        sdk, manifest = {'presets': {}}, {'voices': []}
        for index in range(25):
            name, slug = f'Giọng {index}', f'voice_{index}'
            sdk['presets'][name] = {'description': 'Nam · Bắc · Trẻ', 'gender': 'male', 'aliases': [f'Cũ {index}']}
            manifest['voices'].append({'name': f'Cũ {index}', 'id': slug})
            folder = self.models / 'voices' / slug
            folder.mkdir(parents=True)
            (folder / 'ref_codes.txt').write_text('1,2,3', encoding='utf-8')
            (folder / 'speaker.emb.txt').write_text('0.1,0.2', encoding='utf-8')
        (self.models / 'voices_v3_turbo.json').write_text(json.dumps(sdk, ensure_ascii=False), encoding='utf-8')
        (self.models / 'voices/manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        self.installation = self.models / 'installation.json'
        self.installation.write_text(json.dumps({'verified': True, 'backend': 'cpu', 'threads': 4,
            'model_revision': 'pinned revision', 'model_sha256': 'pinned sha', 'audio_cpp_version': '0.9.1'}), encoding='utf-8')
        self.paths = patch.multiple(voices, ROOT=self.root, MODEL_ROOT=self.models, RUNTIME=self.runtime)
        self.paths.start()
        voices._read_cached.cache_clear()
        voices._small_hash.cache_clear()

    def tearDown(self):
        self.paths.stop()
        self.temporary.cleanup()


class CatalogTests(CatalogFixture):
    def test_catalog_canonical_alias_region_and_no_inference(self):
        with patch.object(voices.subprocess, 'Popen', side_effect=AssertionError('catalog must be cheap')):
            rows = voices.entries()
            state = voices.provider_status()
        self.assertEqual(len(rows), 25)
        self.assertEqual(rows[0]['id'], 'vieneu:Giọng 0')
        self.assertEqual(rows[0]['voice_slug'], 'voice_0')
        self.assertEqual(rows[0]['region'], 'North')
        self.assertEqual(rows[0]['sample_rate'], 48000)
        self.assertTrue(state['available'])
        self.assertEqual(state['message'], 'Engine và giọng đã cài trong NPK.')

    def test_offline_project_voice_can_be_saved_but_not_generated(self):
        (self.runtime / 'vieneu/Scripts/python.exe').unlink()
        entry = voices.resolve_voice('vieneu:Giọng 0')
        self.assertFalse(entry['available'])
        with self.assertRaisesRegex(ValueError, 'cài engine/model NPK'):
            voices.resolve_voice(entry['id'], require_ready=True)

    def test_missing_assets_or_unverified_install_not_ready(self):
        (self.models / 'voices/voice_2/ref_codes.txt').unlink()
        self.assertFalse(voices.provider_status()['available'])
        (self.models / 'voices/voice_2/ref_codes.txt').write_text('1', encoding='utf-8')
        alter(self.installation, '{"verified": false, "backend": "cpu"}')
        self.assertFalse(voices.provider_status()['available'])

    def test_invalid_slug_is_never_resolved_outside_owned_assets(self):
        path = self.models / 'voices/manifest.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['voices'][0]['id'] = '../../elsewhere'
        alter(path, json.dumps(data))
        self.assertEqual(voices.entries(), [])
        self.assertFalse(voices.provider_status()['available'])

    def test_identity_invalidates_codes_embedding_and_model_changes(self):
        original = voices.resolve_voice('vieneu:Giọng 0')['cache_identity']
        for path in (self.models / 'voices/voice_0/ref_codes.txt',
                     self.models / 'voices/voice_0/speaker.emb.txt', self.models / 'model.gguf'):
            alter(path, path.read_text(encoding='utf-8') + '1')
            current = voices.resolve_voice('vieneu:Giọng 0')['cache_identity']
            self.assertNotEqual(original, current)
            original = current

    def test_metadata_parse_is_cached_until_stat_changes(self):
        path = self.models / 'voices_v3_turbo.json'
        with patch.object(voices.json, 'loads', wraps=json.loads) as decoder:
            voices._read(path)
            voices._read(path)
            self.assertEqual(decoder.call_count, 1)
            alter(path, path.read_text(encoding='utf-8') + ' ')
            voices._read(path)
            self.assertEqual(decoder.call_count, 2)


class AdapterTests(CatalogFixture):
    def test_expressive_flag_changes_identity_only_when_enabled(self):
        entry = voices.resolve_voice('vieneu:Giọng 0', require_ready=True)
        baseline = voices.synthesis_identity(entry, {})
        self.assertEqual(baseline, voices.synthesis_identity(entry, {'tts_expressive': False}))
        self.assertNotEqual(baseline, voices.synthesis_identity(entry, {'tts_expressive': True}))

    def test_opt_in_delivery_prepares_laughter_but_leaves_input_text_untouched(self):
        entry = voices.resolve_voice('vieneu:Giọng 0', require_ready=True)
        text = 'Hì hì! Mua 2 chai 500ml.'
        core = types.SimpleNamespace(binary=lambda name: 'owned-ffmpeg.exe')
        with patch.dict(sys.modules, {'core': core}):
            original = voices._prepare(entry, text, self.root / 'speech.wav', 1, {})
            expressive = voices._prepare(entry, text, self.root / 'speech.wav', 1, {'tts_expressive': True})
        self.assertEqual(original['text'], text)
        self.assertEqual(expressive['text'], '[cười]! Mua 2 chai 500ml.')
        self.assertEqual(text, 'Hì hì! Mua 2 chai 500ml.')

    def generate(self, callback, rate=1):
        entry = voices.resolve_voice('vieneu:Giọng 0', require_ready=True)
        target = self.root / 'speech.wav'
        target.write_bytes(b'previous file')
        client = types.SimpleNamespace(request=callback)
        core = types.SimpleNamespace(binary=lambda name: 'owned-ffmpeg.exe')
        with patch.object(voices, '_engine', return_value=client), patch.dict(sys.modules, {'core': core}):
            result = voices.synthesize_vieneu(entry, 'Xin chào.', target, rate)
        return result, target

    def test_atomic_pcm16_output_and_own_paths(self):
        requests = []
        def render(payload):
            requests.append(payload)
            wav(payload['output'], frames=24000)
            return {'elapsed_seconds': .1}
        result, target = self.generate(render, .8)
        self.assertEqual(result['duration'], .5)
        self.assertEqual(result['sample_rate'], 48000)
        self.assertEqual(requests[0]['rate'], .8)
        self.assertEqual(requests[0]['backend'], 'cpu')
        self.assertTrue(str(requests[0]['model']).startswith(str(self.models)))
        with wave.open(str(target)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getsampwidth()), (1, 2))
        self.assertEqual(list(self.root.glob('speech.wav.*.tmp.wav')), [])

    def test_bad_rate_rejected_before_launch(self):
        for rate in (None, 'bad', float('nan'), .49, 2.01):
            with self.subTest(rate=rate), self.assertRaises(ValueError):
                self.generate(lambda payload: self.fail('engine must not launch'), rate)

    def test_invalid_audio_preserves_old_output_and_cleans_temporary(self):
        def wrong_rate(payload):
            wav(payload['output'], rate=24000)
            return {}
        def truncated(payload):
            path = Path(payload['output'])
            wav(path, frames=1000)
            path.write_bytes(path.read_bytes()[:-1000])
            return {}
        def corrupt(payload):
            Path(payload['output']).write_bytes(b'not a wave')
            return {}
        for callback in (wrong_rate, truncated, corrupt):
            with self.subTest(callback=callback.__name__), self.assertRaises(ValueError):
                self.generate(callback)
            self.assertEqual((self.root / 'speech.wav').read_bytes(), b'previous file')
            self.assertEqual(list(self.root.glob('speech.wav.*.tmp.wav')), [])

    def test_model_changed_during_synthesis_rejected(self):
        def render(payload):
            wav(payload['output'], frames=1000)
            alter(self.models / 'voices/voice_0/ref_codes.txt', 'new codes')
            return {}
        with self.assertRaisesRegex(ValueError, 'thay đổi trong lúc tạo'):
            self.generate(render)
        self.assertEqual((self.root / 'speech.wav').read_bytes(), b'previous file')


class EngineAssemblyTests(unittest.TestCase):
    def test_nonverbal_only_packet_generates_verified_complete_wave(self):
        inputs = []
        def run(command, env, timeout=850):
            if '--batch-text-dir' in command:
                folder = Path(command[command.index('--batch-text-dir') + 1])
                output = Path(command[command.index('--out-dir') + 1])
                for path in sorted(folder.glob('*.json')):
                    inputs.append(json.loads(path.read_text(encoding='utf-8'))['text'])
                    wav(output / (path.stem + '.wav'), frames=12000)
            else:
                wav(command[-1], frames=12000)
        with tempfile.TemporaryDirectory(prefix='npk-nonverbal-test-') as temporary:
            output = Path(temporary) / 'laugh.wav'
            request = {'text': '[cười]', 'output': str(output), 'cli': 'owned-native.exe', 'model': 'owned.gguf',
                'voice_codes': 'own-codes.txt', 'speaker_embedding': 'own-embedding.txt', 'ffmpeg': 'ffmpeg.exe'}
            with patch.object(engine, '_run', side_effect=run), patch.object(engine, 'phonemize', return_value='<|emotion_1|>.'):
                result = engine.synthesize(request)
            self.assertEqual(inputs, ['<|emotion_1|>.'])
            self.assertEqual(result['speech_parts'], 1)
            self.assertEqual(result['chunks'][0]['text'], '[cười]')
            with wave.open(str(output)) as stream:
                self.assertEqual((stream.getnchannels(), stream.getsampwidth(), stream.getframerate(), stream.getnframes()), (1, 2, 48000, 12000))
                self.assertTrue(any(stream.readframes(12000)))

    def test_batch_once_speed_changes_only_speech_and_output_stays_48k(self):
        calls = []
        def run(command, env, timeout=850):
            calls.append((command, env))
            if '--batch-text-dir' in command:
                inputs = Path(command[command.index('--batch-text-dir') + 1])
                outputs = Path(command[command.index('--out-dir') + 1])
                for source in sorted(inputs.glob('*.json')):
                    wav(outputs / (source.stem + '.wav'), frames=48000)
            else:
                speed = float(command[command.index('-af') + 1].split('=')[1]) if '-af' in command else 1
                wav(command[-1], frames=round(48000 / speed))
        with tempfile.TemporaryDirectory(prefix='npk-engine-test-') as temporary:
            output = Path(temporary) / 'speech.wav'
            request = {'text': '[nghỉ 250ms] Một [nghỉ 500ms] Hai [nghỉ 250ms]', 'rate': 2,
                'output': str(output), 'cli': 'owned-native.exe', 'model': 'owned.gguf',
                'voice_codes': 'own-codes.txt', 'speaker_embedding': 'own-embedding.txt', 'ffmpeg': 'ffmpeg.exe'}
            with patch.object(engine, '_run', side_effect=run), patch.object(engine, 'phonemize', side_effect=lambda text: text):
                result = engine.synthesize(request)
            with wave.open(str(output)) as stream:
                self.assertEqual(stream.getnframes(), 96000)  # 2 x .5 speech + 1 second explicit silence.
                samples = struct.unpack('<' + 'h' * stream.getnframes(), stream.readframes(stream.getnframes()))
                self.assertTrue(all(value == 0 for value in samples[:12000]))
                self.assertTrue(all(value != 0 for value in samples[12000:36000]))
                self.assertTrue(all(value == 0 for value in samples[36000:60000]))
                self.assertTrue(all(value != 0 for value in samples[60000:84000]))
                self.assertTrue(all(value == 0 for value in samples[84000:]))
            self.assertEqual(sum('--batch-text-dir' in command for command, _ in calls), 1)
            self.assertEqual(result['speech_parts'], 2)
            self.assertTrue(all(env['HF_HUB_OFFLINE'] == '1' for _, env in calls))
            self.assertFalse(list(Path(temporary).glob('npk-vieneu-*')))

    def test_failed_native_batch_preserves_existing_output(self):
        with tempfile.TemporaryDirectory(prefix='npk-engine-test-') as temporary:
            output = Path(temporary) / 'speech.wav'
            output.write_bytes(b'old audio')
            request = {'text': 'Xin chào.', 'output': str(output), 'cli': 'native.exe', 'model': 'model.gguf',
                'voice_codes': 'codes.txt', 'speaker_embedding': 'emb.txt', 'ffmpeg': 'ffmpeg.exe'}
            with patch.object(engine, '_run', side_effect=ValueError('native error')), patch.object(engine, 'phonemize', return_value='xin'):
                with self.assertRaisesRegex(ValueError, 'native error'):
                    engine.synthesize(request)
            self.assertEqual(output.read_bytes(), b'old audio')
            self.assertFalse(output.with_name(output.name + '.writing').exists())
            self.assertFalse(list(Path(temporary).glob('npk-vieneu-*')))


if __name__ == '__main__':
    unittest.main()
