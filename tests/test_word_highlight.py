"""Moving word accents follow display-text timings without losing captions."""
import copy
from pathlib import Path
import re
import tempfile
import unittest

from core import DEFAULTS, binary, run, save_srt
from subtitle_presets import apply_subtitle_preset, highlight_caption, highlight_word_timings


class WordHighlightTests(unittest.TestCase):
    def row(self):
        return {'start': 0., 'end': 2., 'text': '你好', 'translated': 'Xin chào bạn',
                'words': [{'word': 'Xin', 'start': .15, 'end': .5},
                          {'word': 'chào', 'start': .8, 'end': 1.3},
                          {'word': 'bạn', 'start': 1.55, 'end': 1.9}]}

    def test_exact_displayed_words_prefer_absolute_times_and_keep_pauses(self):
        row = self.row()
        before = copy.deepcopy(row)
        result = highlight_word_timings(row, 'Xin chào,\nbạn!')
        self.assertEqual(result['source'], 'aligned')
        self.assertEqual(result['words'][1], {'word': 'chào,', 'start': .8, 'end': 1.3})
        self.assertLess(result['words'][0]['end'], result['words'][1]['start'])
        self.assertEqual(row, before)
        row['word_timing_source'] = 'estimated'
        self.assertEqual(highlight_word_timings(row, 'Xin chào bạn')['source'], 'estimated')

    def test_mismatched_source_words_estimate_without_mutating_recognition(self):
        row = self.row()
        row['words'] = [{'word': '你好', 'start': 0, 'end': 2}]
        before = copy.deepcopy(row)
        result = highlight_word_timings(row, 'Xin chào\nbạn')
        self.assertEqual(result['source'], 'estimated')
        self.assertEqual([word['word'] for word in result['words']], ['Xin', 'chào', 'bạn'])
        self.assertEqual(result['words'][0]['start'], 0)
        self.assertEqual(result['words'][-1]['end'], 2)
        self.assertTrue(all(word['end'] > word['start'] for word in result['words']))
        self.assertEqual(row, before)

    def test_caption_words_take_priority_without_overwriting_source_recognition(self):
        row = self.row()
        aligned = copy.deepcopy(row['words'])
        row['words'] = [{'word': '你好', 'start': 0, 'end': 2}]
        row['caption_words'] = aligned
        before = copy.deepcopy(row)
        result = highlight_word_timings(row, row['translated'])
        self.assertEqual(result['source'], 'aligned')
        self.assertEqual(result['words'], aligned)
        self.assertEqual(row, before)
        row['caption_words'] = []
        self.assertEqual(highlight_word_timings(row, row['translated'])['source'], 'estimated')

    def test_clips_cue_bounds_and_trims_overlap_so_only_one_word_is_active(self):
        row = self.row()
        row['words'][0].update(start=-.2, end=.95)
        row['words'][2]['end'] = 3
        result = highlight_word_timings(row, row['translated'])
        self.assertEqual(result['source'], 'aligned')
        self.assertEqual(result['words'][0]['start'], 0)
        self.assertEqual(result['words'][0]['end'], .8)
        self.assertEqual(result['words'][-1]['end'], 2)
        for left, right in zip(result['words'], result['words'][1:]):
            self.assertLessEqual(left['end'], right['start'])

    def test_invalid_and_reversed_times_cannot_drive_highlight(self):
        for changes in ({'start': float('nan')}, {'end': .1}, {'start': -5, 'end': -4}):
            row = self.row()
            row['words'][0].update(changes)
            self.assertEqual(highlight_word_timings(row, row['translated'])['source'], 'estimated')
        row = self.row()
        row['words'][1]['start'] = .1
        self.assertEqual(highlight_word_timings(row, row['translated'])['source'], 'estimated')
        self.assertEqual(highlight_word_timings({'start': 0, 'end': .001}, 'Xin chào')['source'], 'static')

    def test_inline_transforms_reset_every_word_and_preserve_wrapping(self):
        row = self.row()
        caption = highlight_caption(row, 'Xin chào,\nbạn!', {'font_color': '#FFFFFF'})
        self.assertIn(r'\t(150,151,\1c&H4DD8FF&)', caption)
        self.assertIn(r'\t(499,500,\1c&HFFFFFF&)', caption)
        self.assertIn(r'\t(800,801,\1c&H4DD8FF&)', caption)
        self.assertIn(r'\N', caption)
        self.assertEqual(re.sub(r'\{[^}]*\}', '', caption).replace(r'\N', '\n'), 'Xin chào,\nbạn!')
        self.assertEqual(caption.count(r'{\1c&HFFFFFF&}'), 3)

    def test_ass_one_event_keeps_full_text_style_dimensions_and_plain_srt(self):
        import pysubs2
        row = self.row()
        row['translated'] = 'Xin chào,\nbạn!'
        # The historical full-cue layout preserves explicit line breaks.
        params = apply_subtitle_preset(DEFAULTS | {'subtitle_flow': 'wrap'}, 'highlight')
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            save_srt([row], folder / 'highlight.ass', ass_style=(params, {'width': 640, 'height': 360}))
            doc = pysubs2.load(str(folder / 'highlight.ass'))
            self.assertEqual(len(doc), 1)
            self.assertEqual(doc[0].plaintext, row['translated'])
            self.assertEqual((doc[0].start, doc[0].end), (0, 2000))
            self.assertEqual((doc.info['PlayResX'], doc.info['PlayResY']), ('640', '360'))
            self.assertEqual(doc.styles['Default'].borderstyle, 3)
            self.assertTrue(doc.styles['Default'].bold)
            save_srt([row], folder / 'captions.srt')
            self.assertEqual(pysubs2.load(str(folder / 'captions.srt'))[0].plaintext, row['translated'])

    def test_invalid_cues_do_not_emit_zero_length_ass_events(self):
        import pysubs2
        rows = [self.row(), {'start': 2, 'end': 2, 'text': 'Zero'},
                {'start': 2.001, 'end': 2.002, 'text': 'Invisible'},
                {'start': float('nan'), 'end': 3, 'text': 'Invalid'}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'bounded.ass'
            save_srt(rows, path, ass_style=(apply_subtitle_preset(DEFAULTS, 'highlight'),
                                           {'width': 640, 'height': 360}))
            doc = pysubs2.load(str(path))
            self.assertEqual(len(doc), 1)
            self.assertTrue(all(event.end > event.start for event in doc))

    def test_real_ffmpeg_moves_yellow_word_and_resets_during_pause(self):
        import numpy as np
        from PIL import Image
        params = apply_subtitle_preset(DEFAULTS, 'highlight') | {'font_size': 36}
        centers = []
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            save_srt([self.row()], folder / 'highlight.ass',
                     ass_style=(params, {'width': 640, 'height': 360}))
            for position in (.3, .65, 1., 1.7):
                target = folder / (str(position) + '.png')
                run([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
                     'color=c=0x14202c:s=640x360:d=2:r=20', '-vf', 'subtitles=highlight.ass',
                     '-ss', str(position), '-frames:v', '1', '-threads', '1', target],
                    cwd=folder, timeout=30)
                with Image.open(target) as image:
                    rgb = np.asarray(image.convert('RGB'))
                    yellow = (rgb[:, :, 0] > 220) & (rgb[:, :, 1] > 160) & (rgb[:, :, 2] < 130)
                    ys, xs = np.where(yellow)
                    centers.append(float(xs.mean()) if len(xs) else None)
        self.assertIsNone(centers[1], 'Words must return to white during a spoken pause.')
        self.assertTrue(all(centers[index] is not None for index in (0, 2, 3)))
        self.assertLess(centers[0], centers[2])
        self.assertLess(centers[2], centers[3])


if __name__ == '__main__':
    unittest.main()
