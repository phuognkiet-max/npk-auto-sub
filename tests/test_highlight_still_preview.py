"""Still previews must evaluate moving accents at the requested video time."""
import copy
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from core import DEFAULTS, binary, probe, run
from subtitle_presets import apply_subtitle_preset
import worker


class HighlightStillPreviewTests(unittest.TestCase):
    def setUp(self):
        self.files = tempfile.TemporaryDirectory(prefix='npk-highlight-still-')
        self.addCleanup(self.files.cleanup)
        self.folder = Path(self.files.name)
        source = self.folder / 'source.mp4'
        run([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
             'color=c=0x14202c:s=1280x720:d=7:r=10', '-an', '-c:v', 'libx264',
             '-preset', 'ultrafast', '-threads', '1', '-pix_fmt', 'yuv420p', source], timeout=30)
        params = apply_subtitle_preset(DEFAULTS, 'highlight') | {
            'use_clean': False, 'workload_mode': 'quiet', 'video_encoder': 'cpu'}
        self.project = {'id': 'still-preview-fixture', 'name': 'Still preview fixture',
                        'media': probe(source), 'regions': [], 'artifacts': {}, 'params': params,
                        'segments': [{'start': 2., 'end': 6., 'text': '你好',
                                      'translated': 'Xin chào bạn', 'word_timing_source': 'aligned',
                                      'caption_words': [
                                          {'word': 'Xin', 'start': 2.2, 'end': 3.},
                                          {'word': 'chào', 'start': 3.4, 'end': 4.2},
                                          {'word': 'bạn', 'start': 4.7, 'end': 5.6}]}]}

    def test_worker_uses_absolute_time_and_moves_accent_with_pauses(self):
        import pysubs2
        before = copy.deepcopy(self.project)
        centers = []
        # Nonzero cue starts ensure a rebased/stale frame-time implementation
        # cannot accidentally pass by evaluating every image at cue time zero.
        for moment in (2.5, 3.15, 3.8, 5.):
            output = self.folder / ('preview-' + str(moment))
            output.mkdir()
            result = worker.subtitle_preview(self.project, {'kind': 'subtitle_preview', 'time': moment}, output)
            self.assertEqual(result['time'], moment)
            self.assertEqual(result['files'], ['subtitle-preview.png'])
            with Image.open(output / 'subtitle-preview.png') as image:
                self.assertEqual(image.size, (1280, 720))
                rgb = np.asarray(image.convert('RGB'))
                yellow = (rgb[:, :, 0] > 220) & (rgb[:, :, 1] > 160) & (rgb[:, :, 2] < 130)
                _, xs = np.where(yellow)
                centers.append(float(xs.mean()) if len(xs) else None)
            doc = pysubs2.load(str(output / 'preview.ass'))
            self.assertEqual(len(doc), 1)
            self.assertEqual((doc[0].start, doc[0].end), (2000, 6000))
            self.assertEqual(doc[0].plaintext, 'Xin chào bạn')
        self.assertIsNone(centers[1], 'A spoken pause must restore all words to white.')
        self.assertTrue(all(centers[index] is not None for index in (0, 2, 3)))
        self.assertLess(centers[0], centers[2], 'Second word should become yellow later in the cue.')
        self.assertLess(centers[2], centers[3], 'Third word should become yellow last.')
        self.assertEqual(self.project, before, 'A style preview must not edit the project or align audio.')

    def test_preview_outside_cue_requires_a_real_displayed_caption(self):
        output = self.folder / 'outside-cue'
        output.mkdir()
        with self.assertRaisesRegex(ValueError, 'Tua đến một câu phụ đề'):
            worker.subtitle_preview(self.project, {'kind': 'subtitle_preview', 'time': 6.5}, output)
        self.assertFalse((output / 'subtitle-preview.png').exists())


if __name__ == '__main__':
    unittest.main()
