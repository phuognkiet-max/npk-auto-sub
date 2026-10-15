"""Profile, multi-cue batching and pronunciation tests without inference or user data."""
import io
import queue
from pathlib import Path
import tempfile
import threading
import types
import unittest
from unittest.mock import patch
import wave

import pronunciation as lexicon
import vieneu_voice as adapter
from test_vieneu_voice import CatalogFixture, engine, wav


class PronunciationTests(unittest.TestCase):
    def test_boundaries_case_longest_phrase_and_no_cascade(self):
        rules = 'NPK => en pi kê\nNPK Auto => ứng dụng riêng\nRAM => ram mới\nen pi kê => thay tiếp'
        self.assertEqual(lexicon.apply_pronunciation('npk AUTO, NPK; NPKX RAM.', rules),
                         'ứng dụng riêng, en pi kê; NPKX ram mới.')

    def test_inline_tags_are_never_rewritten(self):
        rules = 'cười => tiếng cười\nNPK => en pi kê\nnghỉ => dừng'
        self.assertEqual(lexicon.apply_pronunciation('NPK [cười] nghỉ [nghỉ 1s] <|emotion_1|>', rules),
                         'en pi kê [cười] dừng [nghỉ 1s] <|emotion_1|>')

    def test_canonical_identity_ignores_comments_and_spacing(self):
        self.assertEqual(lexicon.validate_dictionary('# ghi chú\n NPK  =>  en pi kê \n'), 'NPK => en pi kê')
        self.assertEqual(lexicon.dictionary_identity('# note\nNPK => en pi kê'),
                         lexicon.dictionary_identity('NPK=>en pi kê'))
        self.assertEqual(lexicon.dictionary_identity(''), '')

    def test_invalid_and_duplicate_rules_are_rejected(self):
        for document in (None, 'a', 'a =>', '=> b', 'a=>b=>c', 'NPK=>một\nnpk=>hai',
                         'a => [cười]', 'a => <|emotion_1|>', 'x' * 20001):
            with self.subTest(document=str(document)[:50]), self.assertRaises(ValueError):
                lexicon.validate_dictionary(document)

    def test_limit_and_no_partial_replacement_inside_words(self):
        with self.assertRaises(ValueError):
            lexicon.validate_dictionary('\n'.join(f'a{i} => b' for i in range(257)))
        self.assertEqual(lexicon.apply_pronunciation('RAM ramjet VRAM', 'RAM => bộ nhớ'), 'bộ nhớ ramjet VRAM')


class RuntimeAndBatchTests(CatalogFixture):
    def setUp(self):
        super().setUp()
        folder = self.runtime / 'audio-cpp/cuda'
        folder.mkdir(parents=True)
        (folder / 'audiocpp_cli.exe').write_bytes(b'cuda fixture')
        self.core = types.SimpleNamespace(binary=lambda name: 'owned-ffmpeg.exe')

    def test_profile_controls_native_threads_and_pacing(self):
        for mode, threads, duty, batch_size in [('quiet', 1, .6, 24), ('balanced', 2, .85, 64), ('fast', 4, 1, 128)]:
            config = adapter.runtime_configuration({'tts_device': 'cuda', 'workload_mode': mode})
            self.assertEqual(config, dict(backend='cuda', threads=threads, duty=duty, batch_size=batch_size))
        self.assertEqual(adapter.runtime_configuration(None)['threads'], 4)

    def test_missing_explicit_device_is_error_auto_can_use_cpu(self):
        (self.runtime / 'audio-cpp/cuda/audiocpp_cli.exe').unlink()
        with self.assertRaises(ValueError):
            adapter.runtime_configuration({'tts_device': 'cuda'})
        self.assertEqual(adapter.runtime_configuration({'tts_device': 'auto'})['backend'], 'cpu')
        with self.assertRaises(ValueError):
            adapter.runtime_configuration({'tts_device': 'best'})

    def test_identity_includes_effective_device_profile_and_dictionary(self):
        row = adapter.resolve_voice('vieneu:Giọng 0', True)
        quiet = adapter.synthesis_identity(row, {'tts_device': 'cpu', 'workload_mode': 'quiet'})
        for params in ({'tts_device': 'cuda'}, {'workload_mode': 'fast'}, {'pronunciation_dictionary': 'NPK => en pi kê'}):
            self.assertNotEqual(quiet, adapter.synthesis_identity(row, params))

    def test_same_voice_batch_uses_one_request_and_keeps_order_and_delivery(self):
        rows = [adapter.resolve_voice('vieneu:Giọng 0', True)] * 2
        calls, events = [], []
        def generate(payload, progress=None):
            calls.append(payload)
            for index, item in enumerate(payload['items']):
                wav(item['output'], frames=(index + 1) * 24000)
            if progress:
                progress(dict(progress=.5, message='native progress'))
            return {'items': [{}, {}], 'native_batches': 1}
        requests = [dict(entry=row, text='NPK [nghỉ 1s] xin chào.', path=self.root / f'cue{index}.wav', rate=1)
                    for index, row in enumerate(rows)]
        options = {'tts_device': 'cuda', 'workload_mode': 'quiet', 'pronunciation_dictionary': 'NPK => en pi kê'}
        with patch.object(adapter, '_engine', return_value=types.SimpleNamespace(request=generate)), \
                patch.dict('sys.modules', {'core': self.core}):
            result = adapter.synthesize_vieneu_batch(requests, options, events.append)
        self.assertEqual(len(calls), 1)
        self.assertEqual([row['duration'] for row in result], [.5, 1])
        self.assertEqual((calls[0]['backend'], calls[0]['threads']), ('cuda', 1))
        self.assertEqual(calls[0]['items'][0]['text'], 'en pi kê [nghỉ 1s] xin chào.')
        self.assertTrue(events)
        self.assertEqual(events[-1]['progress'], 1)

    def test_mixed_voice_groups_return_original_request_order(self):
        calls = []
        def generate(payload):
            calls.append(payload)
            for item in payload['items']:
                wav(item['output'], frames=24000 if 'voice_0' in payload['voice_codes'] else 48000)
            return {'items': [{} for _ in payload['items']]}
        identifiers = ['vieneu:Giọng 1', 'vieneu:Giọng 0', 'vieneu:Giọng 1']
        requests = [dict(entry=adapter.resolve_voice(identifier, True), text='Xin chào.',
                         path=self.root / f'mixed{index}.wav', rate=1) for index, identifier in enumerate(identifiers)]
        with patch.object(adapter, '_engine', return_value=types.SimpleNamespace(request=generate)), \
                patch.dict('sys.modules', {'core': self.core}):
            result = adapter.synthesize_vieneu_batch(requests, {'tts_device': 'cpu'})
        self.assertEqual(len(calls), 2)
        self.assertEqual([item['voice_id'] for item in result], identifiers)
        self.assertEqual([item['duration'] for item in result], [1, .5, 1])

    def test_one_bad_wave_keeps_verified_earlier_target_and_preserves_bad_target(self):
        def generate(payload):
            for index, item in enumerate(payload['items']):
                wav(item['output'], rate=24000 if index else 48000)
            return {'items': [{}, {}]}
        requests = []
        for index in range(2):
            target = self.root / f'atomic{index}.wav'
            target.write_bytes(b'prior audio')
            requests.append(dict(entry=adapter.resolve_voice('vieneu:Giọng 0', True), text='Xin chào.', path=target))
        with patch.object(adapter, '_engine', return_value=types.SimpleNamespace(request=generate)), \
                patch.dict('sys.modules', {'core': self.core}), self.assertRaises(ValueError) as caught:
            adapter.synthesize_vieneu_batch(requests, {})
        with wave.open(str(requests[0]['path'])) as stream:
            self.assertEqual(stream.getframerate(), 48000)
        self.assertEqual(requests[1]['path'].read_bytes(), b'prior audio')
        self.assertEqual(set(caught.exception.partial_results), {0})
        self.assertEqual(caught.exception.partial_results[0]['lexical_verification'], 'unverified')
        self.assertFalse(list(self.root.glob('*.tmp.wav')))

    def test_broker_failure_recovers_complete_item_with_unknown_engine_metadata(self):
        def generate(payload):
            wav(payload['items'][0]['output'])
            Path(payload['items'][1]['output']).write_bytes(b'incomplete')
            raise ValueError('late native failure')
        requests = [dict(entry=adapter.resolve_voice('vieneu:Giọng 0', True), text='Xin chào.',
                         path=self.root / f'recover{index}.wav') for index in range(2)]
        with patch.object(adapter, '_engine', return_value=types.SimpleNamespace(request=generate)), \
                patch.dict('sys.modules', {'core': self.core}), self.assertRaisesRegex(ValueError, 'late native') as caught:
            adapter.synthesize_vieneu_batch(requests, {})
        self.assertTrue(requests[0]['path'].is_file())
        self.assertFalse(requests[1]['path'].exists())
        proof = caught.exception.partial_results[0]
        self.assertFalse(proof['engine_metadata_complete'])
        self.assertEqual(proof['audio_verification'], 'pcm-complete')
        self.assertEqual(proof['lexical_verification'], 'unverified')


class NativeBatchTests(unittest.TestCase):
    def test_late_native_block_failure_retains_finished_utterance(self):
        native_count = 0
        def run(command, env, timeout=850):
            nonlocal native_count
            if '--batch-text-dir' in command:
                native_count += 1
                source = Path(command[command.index('--batch-text-dir') + 1])
                target = Path(command[command.index('--out-dir') + 1])
                if native_count == 2:
                    raise ValueError('later batch failed')
                for entry in sorted(source.glob('*.json')):
                    wav(target / (entry.stem + '.wav'))
            else:
                wav(command[-1])
        with tempfile.TemporaryDirectory(prefix='npk-partial-native-') as temporary:
            folder = Path(temporary)
            request = dict(cli='native.exe', model='own.gguf', voice_codes='codes.txt', speaker_embedding='emb.txt',
                           ffmpeg='ffmpeg.exe', batch_size=1,
                           items=[dict(text='Một.', output=str(folder/'one.wav')),
                                  dict(text='Hai.', output=str(folder/'two.wav'))])
            with patch.object(engine, '_run', side_effect=run), patch.object(engine, 'phonemize', side_effect=lambda text: text), \
                    self.assertRaisesRegex(ValueError, 'later batch failed'):
                engine.synthesize_batch(request)
            with wave.open(str(folder/'one.wav')) as stream:
                self.assertEqual(stream.getnframes(), 48000)
            self.assertFalse((folder/'two.wav').exists())
            self.assertFalse(list(folder.glob('*.writing')))

    def test_partial_native_block_salvages_only_exact_named_complete_item(self):
        def run(command, env, timeout=850):
            if '--batch-text-dir' in command:
                target = Path(command[command.index('--out-dir') + 1])
                wav(target/'part0001.wav')  # request 0 is missing; do not shift request 1 onto it.
                raise ValueError('partial native block')
            wav(command[-1])
        with tempfile.TemporaryDirectory(prefix='npk-partial-native-') as temporary:
            folder = Path(temporary)
            request = dict(cli='native.exe', model='own.gguf', voice_codes='codes.txt', speaker_embedding='emb.txt',
                           ffmpeg='ffmpeg.exe', batch_size=2,
                           items=[dict(text='Một.', output=str(folder/'one.wav')),
                                  dict(text='Hai.', output=str(folder/'two.wav'))])
            with patch.object(engine, '_run', side_effect=run), patch.object(engine, 'phonemize', side_effect=lambda text: text), \
                    self.assertRaisesRegex(ValueError, 'partial native block'):
                engine.synthesize_batch(request)
            self.assertFalse((folder/'one.wav').exists())
            self.assertTrue((folder/'two.wav').exists())

    def test_many_cues_share_session_and_keep_individual_speed_pause_metadata(self):
        calls, events = [], []
        def run(command, env, timeout=850):
            calls.append(command)
            if '--batch-text-dir' in command:
                source = Path(command[command.index('--batch-text-dir') + 1])
                target = Path(command[command.index('--out-dir') + 1])
                for entry in sorted(source.glob('*.json')):
                    wav(target / (entry.stem + '.wav'))
            else:
                speed = float(command[command.index('-af') + 1].split('=')[1]) if '-af' in command else 1
                wav(command[-1], frames=round(48000 / speed))
        with tempfile.TemporaryDirectory(prefix='npk-batch-test-') as temporary:
            folder = Path(temporary)
            request = dict(cli='native.exe', model='own.gguf', voice_codes='codes.txt', speaker_embedding='emb.txt',
                           ffmpeg='ffmpeg.exe', backend='cpu', threads=1, batch_size=24, duty=.6,
                           items=[dict(text='Một [nghỉ 500ms] Hai.', rate=2, output=str(folder / 'one.wav')),
                                  dict(text='Ba.', rate=.5, output=str(folder / 'two.wav'))])
            with patch.object(engine, '_run', side_effect=run), patch.object(engine, 'phonemize', side_effect=lambda text: text):
                result = engine.synthesize_batch(request, events.append)
            self.assertEqual(sum('--batch-text-dir' in call for call in calls), 1)
            self.assertEqual([item['duration'] for item in result['items']], [1.5, 2])
            self.assertEqual(result['items'][0]['chunks'][1], {'kind': 'pause', 'start': .5, 'end': 1.0})
            self.assertTrue(all(int(call[call.index('--threads') + 1]) == 1 for call in calls if '--threads' in call))
            self.assertEqual({event['stage'] for event in events}, {'phonemize', 'synthesize', 'align'})
            self.assertFalse(list(folder.glob('npk-vieneu-*')))

    def test_quiet_native_blocks_rest_without_dropping_text(self):
        native_calls, text_inputs = [], []
        def run(command, env, timeout=850):
            if '--batch-text-dir' in command:
                native_calls.append(command)
                source = Path(command[command.index('--batch-text-dir') + 1])
                target = Path(command[command.index('--out-dir') + 1])
                for entry in sorted(source.glob('*.json')):
                    text_inputs.append(entry.read_text(encoding='utf-8'))
                    wav(target / (entry.stem + '.wav'), frames=2400)
            else:
                wav(command[-1], frames=2400)
        with tempfile.TemporaryDirectory(prefix='npk-batch-test-') as temporary:
            folder = Path(temporary)
            request = dict(cli='native.exe', model='own.gguf', voice_codes='codes.txt', speaker_embedding='emb.txt',
                ffmpeg='ffmpeg.exe', duty=.6, batch_size=2,
                items=[dict(text=f'Câu {index}.', output=str(folder / f'{index}.wav')) for index in range(3)])
            with patch.object(engine, '_run', side_effect=run), patch.object(engine, 'phonemize', side_effect=lambda text: text), \
                    patch.object(engine.time, 'sleep') as rest:
                result = engine.synthesize_batch(request)
            self.assertEqual((len(native_calls), len(text_inputs), result['native_batches']), (2, 3, 2))
            self.assertEqual(rest.call_count, 1)
            self.assertGreater(rest.call_args.args[0], 0)


class BrokerProgressTests(unittest.TestCase):
    def client(self):
        client = adapter._EngineClient.__new__(adapter._EngineClient)
        client.lock = threading.Lock()
        client.results = queue.Queue()
        client.process = types.SimpleNamespace(poll=lambda: None, stdin=io.StringIO())
        return client

    def test_progress_events_do_not_become_final_result(self):
        client, events = self.client(), []
        client.results.put({'event': 'progress', 'progress': .4, 'message': 'đang chạy'})
        client.results.put({'items': [{'duration': 1}]})
        result = client.request({'items': []}, events.append)
        self.assertEqual(result, {'items': [{'duration': 1}]})
        self.assertEqual(events, [{'progress': .4, 'message': 'đang chạy'}])

    def test_cancel_callback_closes_native_children(self):
        client = self.client()
        client.results.put({'event': 'progress', 'progress': .4})
        def cancelled(event):
            raise InterruptedError('cancelled')
        with patch.object(client, 'close') as close, self.assertRaises(InterruptedError):
            client.request({}, cancelled)
        close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
