"""Regression: valid final video clock ticks must survive low-fps output EOF.

Fixtures are synthetic and tiny. No production video or OCR engine is used.
"""
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from core import NO_WINDOW, binary, probe
from video_sampling import sample_count, sample_end, sample_tail_tolerance, sampled_frames


class VideoSamplingEOFTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.params = {'workload_mode':'quiet'}
        self.log = self.directory/'decoder.log'

    def make_video(self, name='video.mp4', *, duration=3.1, audio_duration=None, vfr=False):
        path = self.directory/name
        rate = 10 if vfr else 30
        command = [binary('ffmpeg'), '-y', '-v', 'error', '-threads', '1', '-filter_threads', '1',
                   '-f', 'lavfi', '-i', f'testsrc2=size=64x48:rate={rate}:duration={duration}']
        if audio_duration is not None:
            command += ['-f', 'lavfi', '-i', f'sine=sample_rate=16000:duration={audio_duration}']
        if vfr:
            command += ['-vf', 'select=eq(n\\,0)+eq(n\\,4)+eq(n\\,11)', '-fps_mode', 'vfr']
        command += ['-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p']
        if audio_duration is not None:
            command += ['-c:a', 'aac']
        command += [str(path)]
        output = subprocess.run(command, capture_output=True, creationflags=NO_WINDOW)
        self.assertEqual(output.returncode, 0, output.stderr)
        return probe(path)

    def samples(self, media, start=0, end=None, interval=.5):
        return list(sampled_frames(media, self.params, start, media['duration'] if end is None else end,
                                   interval, self.log))

    def test_non_multiple_tail_keeps_last_sample_at_3_seconds(self):
        media = self.make_video()
        frames = self.samples(media)
        self.assertEqual([time for time, frame in frames], [0, .5, 1, 1.5, 2, 2.5, 3])
        self.assertTrue(all(frame.shape == (48, 64, 3) for time, frame in frames))
        self.assertEqual(self.log.read_bytes(), b'')

    def test_seeked_tail_and_resume_one_sample_are_valid(self):
        media = self.make_video()
        self.assertEqual([time for time, frame in self.samples(media, start=2)], [2, 2.5, 3])
        self.assertEqual([time for time, frame in self.samples(media, start=3)], [3])

    def test_audio_container_tail_does_not_request_nonexistent_video(self):
        media = self.make_video(audio_duration=4.3)
        self.assertGreater(media['duration'], media['video_duration']+1)
        self.assertAlmostEqual(sample_end(media, media['duration']), 3.1, places=5)
        self.assertEqual([time for time, frame in self.samples(media)], [0, .5, 1, 1.5, 2, 2.5, 3])
        with patch('video_sampling.subprocess.Popen') as launch:
            self.assertEqual(self.samples(media, start=3.5), [])
        launch.assert_not_called()

    def test_vfr_final_frame_uses_stream_timing_instead_of_average_fps_tail(self):
        media = self.make_video(duration=1.2, vfr=True)
        self.assertLess(media['fps'], 3)
        self.assertAlmostEqual(media['video_duration'], 1.2, places=5)
        # The legacy half-average-frame rule would incorrectly skip t=1.0.
        self.assertEqual(sample_count(0, 1.2, .5, media['fps']), 2)
        self.assertEqual([time for time, frame in self.samples(media)], [0, .5, 1])

    def test_clean_early_eof_with_wrong_metadata_is_still_failure(self):
        media = self.make_video(duration=1)
        media.update(duration=2, video_duration=2)
        with self.assertRaisesRegex(RuntimeError, 'Không đọc được khung hình ở 1.00'):
            self.samples(media)

    def test_decoder_nonzero_exit_even_after_all_frames_is_failure(self):
        media = {'path':'synthetic.mp4', 'width':2, 'height':2, 'fps':30,
                 'duration':1, 'video_duration':1}
        process = Mock(stdout=io.BytesIO(b'\0'*(2*2*3*2)))
        process.wait.return_value = 1
        process.poll.return_value = 1
        with patch('video_sampling.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'lỗi giải mã'):
                self.samples(media)
        process.kill.assert_not_called()

    def test_partial_raw_frame_is_failure_and_cleanup_runs(self):
        media = {'path':'synthetic.mp4', 'width':2, 'height':2, 'fps':30,
                 'duration':1, 'video_duration':1}
        process = Mock(stdout=io.BytesIO(b'\0'*11))
        process.wait.return_value = 0
        process.poll.return_value = None
        with patch('video_sampling.subprocess.Popen', return_value=process):
            with self.assertRaisesRegex(RuntimeError, 'Không đọc được khung hình'):
                self.samples(media)
        process.kill.assert_called_once()
        self.assertTrue(process.stdout.closed)

    def test_legacy_tail_and_invalid_clocks_are_explicit(self):
        old = {'duration':1.000952, 'fps':30}
        self.assertAlmostEqual(sample_tail_tolerance(old, .5), 1/60)
        self.assertEqual(sample_count(0, old['duration'], .5, 30), 2)
        self.assertEqual(sample_count(1, 1, .5, 30), 0)
        for value in (float('nan'), -1):
            with self.assertRaises(ValueError):
                sample_end({'duration':2, 'video_duration':value}, 2)
        with self.assertRaises(ValueError):
            sample_count(0, 1, 0, 30)


if __name__ == '__main__':
    unittest.main()
