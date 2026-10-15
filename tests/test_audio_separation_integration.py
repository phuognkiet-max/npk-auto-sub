"""UVR job contracts and admission checks; synthetic stems, no model inference."""
import json
from pathlib import Path
import struct
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np
import audio_separation as separation
from core import DEFAULTS
from job_progress import verify_outputs
import preflight
import resource_budget
import worker


class SeparationIntegrationTests(unittest.TestCase):
    def setUp(self):
        memory = patch('psutil.virtual_memory', return_value=types.SimpleNamespace(available=4 * 1024**3))
        memory.start()
        self.addCleanup(memory.stop)
        self.temporary = tempfile.TemporaryDirectory(prefix='npk-separation-integration-')
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.source = self.folder / 'source.mp4'
        self.source.write_bytes(b'local source fixture')
        self.project = dict(media=dict(path=str(self.source), audio=True, duration=2.,
            bytes=1024, width=320, height=180, fps=25), params=DEFAULTS.copy(), segments=[])

    def fixture(self, frames=88200, silent=True):
        frames = int(frames)
        values = np.zeros((2, frames), dtype=np.float32)
        if not silent:
            values[0] = .2 * np.sin(np.arange(frames) * 2 * np.pi * 200 / 44100)
        for name in ('instrumentals.wav', 'vocals.wav'):
            writer = separation.FloatWaveWriter(self.folder / name)
            writer.write(values)
            writer.close()
        report = dict(schema_version=1, signature=separation.separation_signature(self.project),
                      model_sha256=separation.MODEL_SHA256, sample_rate=44100,
                      frames=frames, duration=frames / 44100)
        (self.folder / 'audio-separation.json').write_text(json.dumps(report), encoding='utf-8')
        return dict(type='separate_audio', files=['instrumentals.wav', 'vocals.wav', 'audio-separation.json'],
                    separation=report.copy())

    def test_worker_dispatch_preserves_output_contract_and_progress_stage(self):
        events = []
        output = {'type': 'separate_audio', 'files': ['instrumentals.wav', 'vocals.wav', 'audio-separation.json']}
        def separate(project, job, folder, emit=None):
            self.assertIs(project, self.project)
            emit(progress=.5, message='halfway')
            return output
        with patch.object(separation, 'separate_audio', side_effect=separate), patch.object(worker, 'emit', side_effect=lambda **e: events.append(e)):
            result = worker.TASKS['separate_audio'](self.project, {'kind': 'separate_audio'}, self.folder)
        self.assertIs(result, output)
        self.assertEqual([row['stage'] for row in events], ['audio_separation', 'audio_separation'])
        self.assertEqual(events[-1]['progress'], .5)

    def test_verified_float_stems_allow_silent_vocals_without_tts_rejection(self):
        result = self.fixture(silent=True)
        verification = verify_outputs(result, self.project, {'kind': 'separate_audio'}, self.folder)
        self.assertEqual(verification['files_checked'], 3)
        self.assertEqual(verification['audio_frames_verified'], {'instrumentals.wav': 88200, 'vocals.wav': 88200})

    def test_missing_required_stem_is_rejected(self):
        result = self.fixture()
        result['files'].remove('vocals.wav')
        with self.assertRaisesRegex(ValueError, 'bắt buộc'):
            verify_outputs(result, self.project, {'kind': 'separate_audio'}, self.folder)

    def test_truncated_or_wrong_float_format_is_rejected(self):
        for change in ('truncate', 'format'):
            result = self.fixture()
            path = self.folder / 'vocals.wav'
            data = bytearray(path.read_bytes())
            if change == 'truncate':
                data = data[:-8]
            else:
                data[20:22] = struct.pack('<H', 1)
            path.write_bytes(data)
            with self.subTest(change=change), self.assertRaises(ValueError):
                verify_outputs(result, self.project, {'kind': 'separate_audio'}, self.folder)

    def test_nonfinite_or_extreme_samples_are_rejected_beyond_header(self):
        for value in (float('nan'), float('inf'), 9.):
            result = self.fixture(silent=False)
            with (self.folder / 'vocals.wav').open('r+b') as stream:
                stream.seek(44 + 80000 * 8)
                stream.write(struct.pack('<f', value))
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'NaN/Inf'):
                verify_outputs(result, self.project, {'kind': 'separate_audio'}, self.folder)

    def test_mismatched_source_metadata_or_full_duration_is_rejected(self):
        result = self.fixture()
        result['separation']['frames'] -= 1
        with self.assertRaisesRegex(ValueError, 'Metadata'):
            verify_outputs(result, self.project, {'kind': 'separate_audio'}, self.folder)
        result = self.fixture(frames=44100)
        with self.assertRaisesRegex(ValueError, 'Thời lượng'):
            verify_outputs(result, self.project, {'kind': 'separate_audio'}, self.folder)
        result = self.fixture()
        self.source.write_bytes(b'changed after job')
        with self.assertRaisesRegex(ValueError, 'nguồn hoặc model'):
            verify_outputs(result, self.project, {'kind': 'separate_audio'}, self.folder)

    def test_admission_checks_audio_model_duration_without_loading_voice(self):
        available = types.SimpleNamespace(free=30 * 1024**3)
        with patch.object(separation, 'separation_status', return_value={'installed': False}), \
                patch.object(resource_budget.shutil, 'disk_usage', return_value=available), \
                patch('voices.resolve_voice') as resolve:
            self.project['media']['audio'] = False
            result = preflight.inspect(self.project, 'separate_audio')
            self.assertFalse(result['ok'])
            self.assertTrue(any('không có âm thanh' in value for value in result['errors']))
            self.assertTrue(any('Chưa cài model' in value for value in result['errors']))
            resolve.assert_not_called()
        with patch.object(separation, 'separation_status', return_value={'installed': True}), \
                patch.object(resource_budget.shutil, 'disk_usage', return_value=available):
            self.project['media']['audio'] = True
            self.assertTrue(preflight.inspect(self.project, 'separate_audio')['ok'])
            for duration in (0, -1, float('nan'), separation.MAX_DURATION + 1):
                self.project['media']['duration'] = duration
                with self.subTest(duration=duration):
                    result = preflight.inspect(self.project, 'separate_audio')
                    self.assertFalse(result['ok'])
                    self.assertTrue(any('tối đa 3 giờ' in value for value in result['errors']))

    def test_disk_forecast_counts_both_float32_stereo_stems(self):
        self.project['media']['duration'] = 3600
        self.project['media']['bytes'] = 20 * 1024**3
        with patch.object(resource_budget.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=30 * 1024**3)):
            budget = resource_budget.estimate_space(self.project, 'separate_audio')
        self.assertEqual(budget['estimated_output_bytes'], 3600 * 44100 * 16 + 88)
        self.assertGreater(budget['required_bytes'], budget['estimated_output_bytes'] + 512 * 1024**2)
        self.assertLess(budget['required_bytes'], 4 * 1024**3)

    def test_admission_blocks_disk_shortage_for_stem_output(self):
        self.project['media']['duration'] = 3600
        with patch.object(separation, 'separation_status', return_value={'installed': True}), \
                patch.object(resource_budget.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=2 * 1024**3)):
            result = preflight.inspect(self.project, 'separate_audio')
        self.assertFalse(result['ok'])
        self.assertTrue(any('không đủ chỗ dự phòng' in value for value in result['errors']))

    def test_admission_blocks_measured_ram_budget_without_gpu_fallback(self):
        self.project['params']['tts_device'] = 'cuda'
        with patch.object(separation, 'separation_status', return_value={'installed': True}), \
                patch.object(resource_budget.shutil, 'disk_usage', return_value=types.SimpleNamespace(free=30 * 1024**3)), \
                patch('psutil.virtual_memory', return_value=types.SimpleNamespace(available=3 * 1024**3 - 1)):
            result = preflight.inspect(self.project, 'separate_audio')
        self.assertFalse(result['ok'])
        self.assertTrue(any('3 GiB RAM' in value for value in result['errors']))


if __name__ == '__main__':
    unittest.main()
