"""Real FFmpeg regression: mono dubbing must retain a stereo music bed."""
import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import wave

import numpy as np
from core import DEFAULTS, ROOT, binary, probe, run, NO_WINDOW
import worker


class RenderStereoTests(unittest.TestCase):
    def test_normalized_mono_voice_and_ducked_stereo_bed_keep_channels_at_48khz(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            duration = 2
            def tone_file(name, rate, frequencies):
                t = np.arange(rate * duration) / rate
                samples = np.stack([.08 * np.sin(2 * np.pi * hz * t) for hz in frequencies], axis=1)
                path = folder / name
                with wave.open(str(path), 'wb') as audio:
                    audio.setparams((len(frequencies), 2, rate, 0, 'NONE', 'not compressed'))
                    audio.writeframes((samples * 32767).astype('<i2').tobytes())
                return str(path)
            voice = tone_file('voice.wav', 48000, [440])
            bed = tone_file('bed.wav', 44100, [880, 1320])
            media = probe(ROOT / 'tests/fixtures/demo.mp4')
            media['duration'] = duration
            params = copy.deepcopy(DEFAULTS) | {
                'use_clean': False, 'burn_subtitles': False, 'voice_path': voice,
                'bgm_path': bed, 'original_gain': 0, 'voice_gain': 1, 'bgm_gain': 1,
                'voice_normalize': True, 'bgm_ducking': True,
                'video_encoder': 'cpu', 'workload_mode': 'quiet',
            }
            project = {'media': media, 'params': params, 'segments': [], 'artifacts': {}}
            with patch.object(worker, 'emit'):
                worker.render(project, {}, folder)
            final = folder / 'final.mp4'
            info = json.loads(run([binary('ffprobe'), '-v', 'error', '-show_streams',
                '-of', 'json', final]).stdout)
            stream = next(item for item in info['streams'] if item['codec_type'] == 'audio')
            self.assertEqual(stream['sample_rate'], '48000')
            self.assertEqual(stream['channels'], 2)
            result = subprocess.run([binary('ffmpeg'), '-v', 'error', '-i', str(final),
                '-vn', '-f', 'f32le', '-acodec', 'pcm_f32le', '-'], capture_output=True,
                check=True, creationflags=NO_WINDOW, timeout=40)
            stereo = np.frombuffer(result.stdout, '<f4').reshape(-1, 2)
            self.assertGreater(len(stereo), 48000)
            left, right = stereo[12000:60000].T
            spectrum = np.abs(np.fft.rfft(np.stack([left, right])))
            frequencies = np.fft.rfftfreq(len(left), 1 / 48000)
            def peak(channel, hz):
                return spectrum[channel, np.abs(frequencies - hz) < 3].max()
            self.assertGreater(peak(0, 880), peak(1, 880) * 5)
            self.assertGreater(peak(1, 1320), peak(0, 1320) * 5)
            for channel in (0, 1):
                self.assertGreater(peak(channel, 440), 1)
            self.assertGreater(float(np.sqrt(np.mean((left - right) ** 2))), .001)


if __name__ == '__main__':
    unittest.main()
