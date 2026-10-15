import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
import wave

import numpy as np
import audio_separation as separation
from core import DATA, DEFAULTS, NO_WINDOW, binary
from tools import setup_audio_separation as setup


class IdentitySession:
    def __init__(self, invalid=False):
        self.shapes = []
        self.invalid = invalid

    def run(self, outputs, inputs):
        values = inputs['input'].copy()
        self.shapes.append(values.shape)
        if self.invalid:
            values[0, 0, 0, 0] = np.nan
        return [values]


class AudioSeparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=DATA)
        self.addCleanup(self.cleanup_temporary)
        self.root = Path(self.temporary.name)
        self.source = self.root/'source.wav'
        self.output = self.root/'result'
        self.session = IdentitySession()

    def cleanup_temporary(self):
        # Windows scanners can briefly hold a just-written report. Keep the
        # fixture inside the private test store and retry sharing violations;
        # persistent locks and other permission errors still fail the test.
        for attempt in range(5):
            try:
                self.temporary.cleanup()
                return
            except PermissionError as error:
                if getattr(error, 'winerror', None) not in (32, 33) or attempt == 4:
                    raise
                time.sleep(.2 * (attempt + 1))

    def source_project(self, duration=12.2):
        count = int(separation.SAMPLE_RATE * duration)
        times = np.arange(count)/separation.SAMPLE_RATE
        audio = np.stack((.25*np.sin(2*np.pi*1000*times), .2*np.sin(2*np.pi*1500*times)), axis=1)
        with wave.open(str(self.source), 'wb') as output:
            output.setparams((2, 2, separation.SAMPLE_RATE, 0, 'NONE', 'not compressed'))
            output.writeframes((audio*32767).astype('<i2').tobytes())
        self.original = (audio*32767).astype('<i2').T.astype('float32')/32768
        return {'media':{'path':str(self.source), 'audio':True, 'duration':duration},
                'params':DEFAULTS | {'workload_mode':'fast'}, 'segments':[]}

    def read_float(self, name):
        value = (self.output/name).read_bytes()
        self.assertEqual(value[:4], b'RIFF')
        self.assertEqual(int.from_bytes(value[20:22], 'little'), 3)
        return np.frombuffer(value[44:], dtype='<f4').reshape(-1, 2).T

    def test_stft_center_layout_and_roundtrip(self):
        transform = separation.NumpySTFT()
        t = np.arange(separation.CHUNK_SIZE)/separation.SAMPLE_RATE
        stereo = np.stack((np.sin(t*2*np.pi*1000), np.cos(t*2*np.pi*1500))).astype('float32')
        packed = transform.forward(stereo)
        self.assertEqual(packed.shape, (1, 4, 2560, 256))
        result = transform.inverse(packed)
        self.assertEqual(result.shape, stereo.shape)
        # Nyquist bin excluded by model layout; the interior is reconstructed.
        self.assertLess(float(np.max(np.abs(result[:, 6000:-6000]-stereo[:, 6000:-6000]))), 2e-5)

    def test_stream_overlaps_preserves_full_length_sum_and_no_boundary_drop(self):
        project = self.source_project()
        with patch.object(separation, '_load_session', return_value=self.session):
            result = separation.separate_audio(project, {}, self.output)
        instrumental, vocal = self.read_float('instrumentals.wav'), self.read_float('vocals.wav')
        self.assertEqual(instrumental.shape, self.original.shape)
        self.assertEqual(vocal.shape, self.original.shape)
        self.assertLess(float(np.max(np.abs(instrumental+vocal-self.original))), 4e-8)
        self.assertEqual(set(self.session.shapes), {(1, 4, 2560, 256)})
        self.assertGreaterEqual(len(self.session.shapes), 3)
        for edge in (separation.STRIDE-separation.TRIM, 2*separation.STRIDE-separation.TRIM):
            expected = self.original[:, edge-512:edge+512]*separation.COMPENSATE
            self.assertLess(float(np.max(np.abs(instrumental[:, edge-512:edge+512]-expected))), .002)
        self.assertEqual(result['separation']['frames'], self.original.shape[1])
        self.assertEqual(result['separation']['provider'], 'CPUExecutionProvider')
        self.assertTrue(result['warnings'])
        for name in ('instrumentals.wav', 'vocals.wav'):
            checked = subprocess.run([binary('ffprobe'), '-v', 'error', '-show_format', '-show_streams',
                                      '-of', 'json', str(self.output/name)],
                                     capture_output=True, text=True, creationflags=NO_WINDOW)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            info = json.loads(checked.stdout)
            self.assertEqual(info['streams'][0]['codec_name'], 'pcm_f32le')
            self.assertEqual(info['streams'][0]['channels'], 2)
            self.assertAlmostEqual(float(info['format']['duration']), 12.2, delta=.001)
            completed = subprocess.run([binary('ffmpeg'), '-v', 'error', '-threads', '1', '-i',
                                        str(self.output/name), '-f', 'null', '-'],
                                       capture_output=True, creationflags=NO_WINDOW)
            self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_very_short_source_and_exact_chunk_tail_are_not_lost(self):
        for duration in (.01, (separation.CHUNK_SIZE-separation.TRIM)/separation.SAMPLE_RATE):
            project = self.source_project(duration)
            with patch.object(separation, '_load_session', return_value=self.session):
                separation.separate_audio(project, {}, self.output)
            combined = self.read_float('instrumentals.wav')+self.read_float('vocals.wav')
            self.assertEqual(combined.shape, self.original.shape)
            self.assertLess(float(np.max(np.abs(combined-self.original))), 4e-8)

    def test_invalid_inference_and_truncated_source_publish_nothing(self):
        project = self.source_project(.1)
        with patch.object(separation, '_load_session', return_value=IdentitySession(invalid=True)):
            with self.assertRaisesRegex(ValueError, 'hữu hạn'):
                separation.separate_audio(project, {}, self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        project['media']['duration'] = 1
        with patch.object(separation, '_load_session', return_value=self.session):
            with self.assertRaisesRegex(ValueError, 'Độ dài'):
                separation.separate_audio(project, {}, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_no_audio_rejected_before_loading_model(self):
        project = self.source_project(.1)
        project['media']['audio'] = False
        with patch.object(separation, '_load_session') as load:
            with self.assertRaisesRegex(ValueError, 'không có âm thanh'):
                separation.separate_audio(project, {}, self.output)
        load.assert_not_called()

    def test_source_longer_than_metadata_stops_before_expensive_inference(self):
        project = self.source_project(1)
        project['media']['duration'] = .1
        with patch.object(separation, '_load_session', return_value=self.session):
            with self.assertRaisesRegex(ValueError, 'dài hơn metadata'):
                separation.separate_audio(project, {}, self.output)
        self.assertEqual(self.session.shapes, [])
        self.assertEqual(list(self.output.iterdir()), [])

    def test_pinned_hash_rejects_equal_size_modified_file(self):
        target = self.root/'model.onnx'
        target.write_bytes(b'bad!')
        with patch.object(separation, 'MODEL_BYTES', 4), patch.object(separation, 'MODEL_SHA256', hashlib.sha256(b'good').hexdigest()):
            with self.assertRaisesRegex(ValueError, 'SHA256'):
                separation.verify_model(target)

    def test_installer_bad_download_preserves_existing_model_and_cleans_partial(self):
        destination = self.root/'models'
        destination.mkdir()
        target = destination/setup.MODEL_NAME
        target.write_bytes(b'previous')
        def opener(request, timeout):
            return io.BytesIO(b'incomplete')
        with self.assertRaisesRegex(ValueError, 'thiếu|SHA256'):
            setup.install(destination, opener=opener)
        self.assertEqual(target.read_bytes(), b'previous')
        self.assertEqual([path.name for path in destination.iterdir()], [setup.MODEL_NAME])


if __name__ == '__main__':
    unittest.main()
