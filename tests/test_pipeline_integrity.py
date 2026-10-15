"""Real tiny media plus synthetic speech: payload, clock and audio-mix regressions.

No model loads, provider calls or production projects are used.
"""
import array
import math
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
import wave

from audio_verification import verify_pcm_speech
from audio_inputs import source_audio_span
from core import DEFAULTS, binary, probe, run
from job_progress import verify_outputs
import worker


def signal(path, *, seconds=1., rate=16000, channels=1, width=2, silent=False):
    count = round(seconds * rate)
    values = array.array('h', (0 if silent else int(7000 * math.sin(i * 2 * math.pi * 400 / rate))
                               for i in range(count) for _ in range(channels)))
    with wave.open(str(path), 'wb') as output:
        output.setparams((channels, width, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(values.tobytes() if width == 2 else b'\x90' * count * channels)


class SpeechPayloadIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='npk-pcm-integrity-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.path = self.folder / 'voice.wav'
        self.project = {'media': {'duration': 1., 'audio': False, 'fps': 30}, 'params': {}}

    def test_header_without_all_declared_pcm_frames_is_rejected(self):
        signal(self.path)
        self.path.write_bytes(self.path.read_bytes()[:-24000])
        with self.assertRaisesRegex(ValueError, 'thiếu dữ liệu'):
            verify_outputs({'files': ['voice.wav']}, self.project, {'kind': 'tts'}, self.folder)

    def test_partial_last_pcm_sample_is_rejected(self):
        signal(self.path)
        self.path.write_bytes(self.path.read_bytes()[:-1])
        with self.assertRaisesRegex(ValueError, 'thiếu dữ liệu'):
            verify_pcm_speech(self.path)

    def test_complete_but_short_timeline_is_rejected(self):
        signal(self.path, seconds=.5)
        with self.assertRaisesRegex(ValueError, 'Thời lượng WAV'):
            verify_outputs({'files': ['voice.wav']}, self.project, {'kind': 'tts'}, self.folder)

    def test_complete_mono_and_stereo_are_checked_and_counted(self):
        for channels in (1, 2):
            signal(self.path, channels=channels)
            with self.subTest(channels=channels):
                result = verify_outputs({'files': ['voice.wav'], 'sample_rate': 16000},
                                        self.project, {'kind': 'tts'}, self.folder)
                self.assertEqual(result['audio_frames_verified'], {'voice.wav': 16000})

    def test_silent_or_wrong_format_or_reported_rate_are_rejected(self):
        signal(self.path, silent=True)
        with self.assertRaisesRegex(ValueError, 'im lặng'):
            verify_pcm_speech(self.path)
        signal(self.path, width=1)
        with self.assertRaisesRegex(ValueError, 'PCM16'):
            verify_pcm_speech(self.path)
        signal(self.path)
        with self.assertRaisesRegex(ValueError, 'Tần số WAV'):
            verify_pcm_speech(self.path, expected_rate=48000)

    def test_first_sample_is_included_and_declared_preview_duration_checked(self):
        with wave.open(str(self.folder / 'sample.wav'), 'wb') as output:
            output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            output.writeframes(array.array('h', [1000]).tobytes())
        result = verify_outputs({'files': ['sample.wav'], 'duration': 1/16000},
                                self.project, {'kind': 'voice_preview'}, self.folder)
        self.assertEqual(result['audio_frames_verified']['sample.wav'], 1)
        with self.assertRaisesRegex(ValueError, 'Thời lượng WAV'):
            verify_outputs({'files': ['sample.wav'], 'duration': 1},
                           self.project, {'kind': 'voice_preview'}, self.folder)

    def test_silent_or_truncated_speech_cache_is_not_reused(self):
        from narration import valid_wave
        signal(self.path, silent=True)
        self.assertFalse(valid_wave(self.path))
        signal(self.path)
        self.assertTrue(valid_wave(self.path))
        self.path.write_bytes(self.path.read_bytes()[:-100])
        self.assertFalse(valid_wave(self.path))

    def test_silent_paragraph_generation_cannot_poison_existing_cache(self):
        import narration
        entry = {'id': 'piper:test', 'name': 'Synthetic voice', 'provider': 'piper'}
        def synthesize(identifier, text, path, params):
            signal(path, silent=True)
        with patch.object(narration, 'DATA', self.folder), \
                patch('voices.resolve_voice', return_value=entry), patch('voices.audio_identity', return_value=['test']), \
                patch('voices.synthesize_text', side_effect=synthesize), self.assertRaisesRegex(ValueError, 'WAV hợp lệ'):
            narration.paragraph_audio([{'text': 'lời đọc'}], DEFAULTS, self.folder)
        self.assertFalse(list((self.folder / 'speech-cache').glob('*.wav')))
        self.assertFalse(list(self.folder.glob('*.part.wav')))

    def test_piper_bad_raw_audio_is_rejected_before_timeline_cache_or_publication(self):
        entry = {'id': 'piper:test', 'name': 'Synthetic Piper', 'provider': 'piper'}
        project = {'media': {'duration': 1., 'audio': False}, 'artifacts': {},
                   'params': DEFAULTS | {'voice_id': 'piper:test', 'tts_timing_mode': 'natural'},
                   'segments': [{'start': 0, 'end': 1., 'text': 'source', 'translated': 'lời đọc'}]}
        for damaged in ('silent', 'truncated'):
            folder = self.folder / damaged
            folder.mkdir()
            raw = folder / 'segment.wav'
            class BadPiper:
                def synthesize_wav(self, text, output, syn_config):
                    output.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
                    output.writeframes(array.array('h', [0 if damaged == 'silent' else 1000] * 4410).tobytes())
                    if damaged == 'truncated':
                        output.close()
                        raw.write_bytes(raw.read_bytes()[:-2000])
            with self.subTest(damaged=damaged), patch.object(worker, 'DATA', self.folder), \
                    patch('voices.resolve_voice', return_value=entry), \
                    patch('voices.audio_identity', return_value=['test']), \
                    patch('voices.load_voice', return_value=(BadPiper(), entry)), patch('worker.emit'), \
                    self.assertRaisesRegex(ValueError, 'Chưa lưu vào bộ nhớ đệm'):
                worker.tts(project, {'kind': 'tts'}, folder)
            self.assertFalse(list((self.folder / 'speech-cache').glob('*.wav')))
            self.assertFalse(list((self.folder / 'speech-cache').glob('*.tmp')))
            self.assertFalse((folder / 'voice.wav').exists())
            self.assertFalse((folder / 'timeline.pcm').exists())
            self.assertFalse((folder / 'segment.wav').exists())


class MediaClockIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='npk-media-clock-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)

    def source(self, name='source.mp4', *, video_seconds=2, audio_seconds=None, audio_offset=0):
        path = self.folder / name
        command = [binary('ffmpeg'), '-v', 'error', '-y', '-threads', '1', '-filter_threads', '1',
                   '-f', 'lavfi', '-i', f'testsrc2=s=64x48:r=30:d={video_seconds}']
        if audio_seconds is not None:
            command += ['-itsoffset', str(audio_offset), '-f', 'lavfi', '-i',
                        f'sine=frequency=400:sample_rate=16000:duration={audio_seconds}',
                        '-map', '0:v:0', '-map', '1:a:0', '-c:a', 'aac']
        command += ['-c:v', 'libx264', '-threads', '1', '-movflags', '+faststart', path]
        run(command, timeout=20)
        return {'media': probe(path), 'regions': [{'x': 2, 'y': 30, 'w': 30, 'h': 12,
                 'start': 0, 'end': max(video_seconds, audio_seconds or 0)}],
                'segments': [], 'artifacts': {}, 'params': DEFAULTS | {
                 'method': 'box', 'mask_mode': 'rectangle', 'dilate': 0,
                 'temporal': False, 'workload_mode': 'fast', 'video_encoder': 'cpu',
                 'use_clean': False, 'burn_subtitles': False, 'voice_gain': 0, 'original_gain': 1}}

    def test_full_clean_preserves_longer_audio_without_inventing_video_frames(self):
        project = self.source(video_seconds=1.2, audio_seconds=2)
        for method in ('box', 'telea'):
            folder = self.folder / method
            folder.mkdir()
            project['params']['method'] = method
            with self.subTest(method=method), patch('worker.emit'):
                result = worker.clean(project, {'kind': 'clean'}, folder)
                verification = verify_outputs(result, project, {'kind': 'clean'}, folder)
                info = probe(folder / 'clean.mp4')
                self.assertAlmostEqual(info['video_duration'], 1.2, delta=.05)
                self.assertAlmostEqual(info['duration'], 2, delta=.12)
                self.assertTrue(info['audio'])
                self.assertEqual(verification['video_frames_decoded']['clean.mp4'], 36)

    def test_full_export_audio_tail_and_muted_export_both_verify_correct_span(self):
        project = self.source(video_seconds=1.2, audio_seconds=2)
        for original_gain, expected in ((1, 2.), (0, 1.2)):
            folder = self.folder / str(original_gain)
            folder.mkdir()
            project['params']['original_gain'] = original_gain
            with self.subTest(original_gain=original_gain), patch('worker.emit'):
                result = worker.render(project, {'kind': 'render'}, folder)
                verify_outputs(result, project, {'kind': 'render'}, folder)
                info = probe(folder / 'final.mp4')
                self.assertAlmostEqual(info['duration'], expected, delta=.12)
                self.assertEqual(info['audio'], bool(original_gain))

    def test_short_video_output_is_not_hidden_by_a_full_audio_track(self):
        project = self.source(video_seconds=1.2, audio_seconds=2)
        short = self.source('short.mp4', video_seconds=.5, audio_seconds=2)
        (self.folder / 'clean.mp4').write_bytes(Path(short['media']['path']).read_bytes())
        with self.assertRaisesRegex(ValueError, 'Luồng hình'):
            verify_outputs({'files': ['clean.mp4']}, project, {'kind': 'clean'}, self.folder)

    def test_ducking_restores_background_after_short_external_voice_ends(self):
        project = self.source(video_seconds=2)
        voice, music = self.folder / 'external-voice.wav', self.folder / 'music.wav'
        signal(voice, seconds=.3)
        signal(music, seconds=2)
        project['params'].update(voice_path=str(voice), voice_gain=1, bgm_path=str(music),
                                 bgm_gain=.8, bgm_ducking=True, original_gain=0)
        with patch('worker.emit'):
            result = worker.render(project, {'kind': 'render'}, self.folder)
        verify_outputs(result, project, {'kind': 'render'}, self.folder)
        decoded = self.folder / 'mix.wav'
        run([binary('ffmpeg'), '-v', 'error', '-y', '-i', self.folder / 'final.mp4',
             '-vn', '-ac', '1', '-ar', '16000', decoded], timeout=15)
        with wave.open(str(decoded), 'rb') as audio:
            audio.setpos(16000)
            values = array.array('h', audio.readframes(8000))
        self.assertGreater(max(map(abs, values)), 1000)

    def test_external_voice_is_not_delayed_to_late_source_audio_start(self):
        project = self.source(video_seconds=4, audio_seconds=1, audio_offset=1.5)
        voice = self.folder / 'external-voice.wav'
        signal(voice, seconds=.3)
        project['params'].update(voice_path=str(voice), voice_gain=1, original_gain=1)
        with patch('worker.emit'):
            result = worker.render(project, {'kind': 'render'}, self.folder)
        verify_outputs(result, project, {'kind': 'render'}, self.folder)
        decoded = self.folder / 'mix.wav'
        run([binary('ffmpeg'), '-v', 'error', '-y', '-i', self.folder / 'final.mp4',
             '-vn', '-af', 'aresample=async=1:first_pts=0', '-ac', '1', '-ar', '16000', decoded], timeout=15)
        with wave.open(str(decoded), 'rb') as audio:
            values = array.array('h', audio.readframes(audio.getnframes()))
        self.assertGreater(max(map(abs, values[:4800])), 1000)
        self.assertLess(max(map(abs, values[8000:16000])), 200)
        self.assertGreater(max(map(abs, values[24000:28000])), 1000)

    def test_analysis_preserves_late_audio_and_measures_real_silent_tail(self):
        from audio_insights import analyze_audio
        project = self.source(video_seconds=4, audio_seconds=1, audio_offset=1.5)
        project['segments'] = [{'start': 0, 'end': 4, 'text': 'lời nguồn'}]
        report = analyze_audio(project, {}, self.folder)['insights']
        prosody = report['cues'][0]['source_prosody']
        self.assertAlmostEqual(report['duration'], 4, delta=.01)
        self.assertTrue(report['source_audio_span_known'])
        self.assertAlmostEqual(report['source_audio_end'], 2.5, delta=.05)
        self.assertAlmostEqual(prosody['pause_before_ms'], 1500, delta=35)
        self.assertAlmostEqual(prosody['pause_after_ms'], 1500, delta=35)
        self.assertAlmostEqual(prosody['silence_ratio'], .75, delta=.03)
        self.assertGreater(len(report['warnings']), 1)

    def test_short_audio_separation_keeps_full_video_clock_with_bounded_stub_inference(self):
        import audio_separation as separation
        import numpy as np
        project = self.source(video_seconds=4, audio_seconds=1, audio_offset=1.5)
        class IdentitySession:
            def run(self, outputs, inputs):
                return [inputs['input'].copy()]
        with patch.object(separation, '_load_session', return_value=IdentitySession()):
            result = separation.separate_audio(project, {}, self.folder)
        verification = verify_outputs(result, project, {'kind': 'separate_audio'}, self.folder)
        self.assertEqual(verification['audio_frames_verified'], {
            'instrumentals.wav': 4 * 44100, 'vocals.wav': 4 * 44100})
        stems = [np.frombuffer((self.folder / name).read_bytes()[44:], dtype='<f4').reshape(-1, 2)
                 for name in ('instrumentals.wav', 'vocals.wav')]
        reconstructed = stems[0] + stems[1]
        self.assertLess(float(np.max(np.abs(reconstructed[:44100]))), .0001)
        self.assertGreater(float(np.max(np.abs(reconstructed[70000:100000]))), .05)
        self.assertLess(float(np.max(np.abs(reconstructed[3*44100:]))), .0001)
        self.assertAlmostEqual(result['separation']['source_audio_end'], 2.5, delta=.05)
        self.assertGreater(len(result['warnings']), 1)

    def test_legacy_video_audio_timing_is_refreshed_without_mutating_project(self):
        from audio_insights import analyze_audio
        project = self.source(video_seconds=4, audio_seconds=1)
        project['media'].pop('audio_duration')
        project['media'].pop('audio_start_time')
        report = analyze_audio(project, {}, self.folder)['insights']
        self.assertTrue(report['source_audio_span_known'])
        self.assertAlmostEqual(report['source_audio_end'], 1, delta=.05)
        self.assertNotIn('audio_duration', project['media'])
        self.assertNotIn('audio_start_time', project['media'])

    def test_corrupted_video_decode_does_not_publish_cover_success(self):
        project = self.source(video_seconds=2, audio_seconds=2)
        path = Path(project['media']['path'])
        original = path.read_bytes()
        path.write_bytes(original[:int(len(original) * .65)])
        with patch('worker.emit'), self.assertRaisesRegex(RuntimeError, 'FFmpeg che nhẹ'):
            worker.clean(project, {'kind': 'clean'}, self.folder)

    def recognize(self, project):
        calls = []
        class WhisperStub:
            def __init__(self, *args, **kwargs):
                calls.append(('init', args, kwargs))
            def transcribe(self, path, **kwargs):
                with wave.open(path, 'rb') as audio:
                    rate, frames = audio.getframerate(), audio.getnframes()
                    values = array.array('h', audio.readframes(frames))
                first = next(i for i, value in enumerate(values) if abs(value) > 100) / rate
                calls.append(('decoded', frames/rate, first))
                segment = types.SimpleNamespace(start=first, end=first+.2, text='lời nguồn', words=[])
                return iter([segment]), types.SimpleNamespace(language='vi', duration=frames/rate)
        stub = types.SimpleNamespace(WhisperModel=WhisperStub)
        with patch.dict('sys.modules', {'faster_whisper': stub}), patch('worker.emit'):
            result = worker.asr(project, {'kind': 'asr'}, self.folder)
        return result, calls

    def test_asr_short_audio_on_long_video_keeps_valid_recognition(self):
        project = self.source(video_seconds=4, audio_seconds=1)
        project['params'].update(asr_device='cpu')
        result, calls = self.recognize(project)
        self.assertAlmostEqual(result['coverage']['analyzed_end'], 1., delta=.1)
        self.assertEqual(len(result['segments']), 1)
        self.assertTrue(result['warnings'])
        self.assertAlmostEqual(calls[-1][1], 1., delta=.1)

    def test_asr_late_audio_uses_original_video_clock_instead_of_shifting_speech_early(self):
        project = self.source(video_seconds=4, audio_seconds=1, audio_offset=1.5)
        project['params'].update(asr_device='cpu')
        result, calls = self.recognize(project)
        self.assertAlmostEqual(result['segments'][0]['start'], 1.5, delta=.02)
        self.assertAlmostEqual(result['coverage']['analyzed_end'], 2.5, delta=.1)
        self.assertAlmostEqual(calls[-1][1], 2.5, delta=.1)
        self.assertGreater(project['media']['audio_start_time'], 1.4)

    def test_asr_after_audio_end_fails_before_any_model_load(self):
        project = self.source(video_seconds=4, audio_seconds=1)
        project['params'].update(asr_device='cpu', extraction_start=3)
        model = unittest.mock.Mock()
        with patch.dict('sys.modules', {'faster_whisper': types.SimpleNamespace(WhisperModel=model)}), \
                patch('worker.emit'), self.assertRaisesRegex(ValueError, 'không có âm thanh'):
            worker.asr(project, {'kind': 'asr'}, self.folder)
        model.assert_not_called()
        # Also exercise the pure legacy/metadata contracts without filesystem changes.
        self.assertEqual(source_audio_span({'duration': 4}), (0., 4.))
        self.assertEqual(source_audio_span({'duration': 4, 'audio_duration': 1, 'audio_start_time': 2}), (2., 3.))


if __name__ == '__main__':
    unittest.main()
