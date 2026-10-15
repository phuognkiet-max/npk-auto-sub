"""Complete, measured subtitle pages without changing the edited speech text."""
import copy
import math
import os
from pathlib import Path
import random
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from subtitle_flow import available_width, caption_chunks, text_width


class SubtitleFlowTests(unittest.TestCase):
    def setUp(self):
        self.params = {'subtitle_flow': 'single', 'subtitle_chunk_words': 6,
            'font': 'Arial', 'font_size': 48, 'outline': 3, 'subtitle_shadow': 0,
            'subtitle_margin_x': 40, 'subtitle_preset': 'clean', 'subtitle_bold': False,
            'subtitle_line_chars': 42}
        self.media = {'width': 1280, 'height': 720}

    @staticmethod
    def measure(text, size):
        return len(text) * size / 2

    @staticmethod
    def row(text, duration=8., start=1.):
        words = text.split()
        gap = duration / len(words)
        return {'start': start, 'end': start + duration,
            'translated': text, 'text': 'Original transcription',
            'caption_words': [{'word': word, 'start': start + i * gap,
                'end': start + (i + .7) * gap} for i, word in enumerate(words)],
            'word_timing_source': 'aligned'}

    def chunks(self, row, text=None, params=None, media=None):
        return caption_chunks(row, text if text is not None else row['translated'],
            params or self.params, media or self.media, measure=self.measure)

    def assert_complete(self, row, pages, text=None, params=None, media=None):
        source = text if text is not None else row['translated']
        self.assertEqual(' '.join(page['text'] for page in pages).split(), source.split())
        self.assertAlmostEqual(pages[0]['start'], row['start'])
        self.assertAlmostEqual(pages[-1]['end'], row['end'])
        seen = 0
        for page in pages:
            self.assertLess(page['start'], page['end'])
            self.assertNotIn('\n', page['text'])
            self.assertNotIn('\r', page['text'])
            self.assertEqual(page['word_start'], seen)
            seen += len(page['text'].split())
            self.assertEqual(page['word_end'], seen)
            if not page.get('layout_overflow'):
                self.assertLessEqual(self.measure(page['text'], page['font_size']),
                    available_width(params or self.params, media or self.media) + .001)
        for previous, following in zip(pages, pages[1:]):
            self.assertAlmostEqual(previous['end'], following['start'])

    def test_user_long_cues_keep_every_word_in_one_measured_line(self):
        for text in ('Á! Đau, đau quá! Em cũng có mặt mà! Sao mẹ chỉ đánh mỗi con?',
            'Xem hai đứa yêu nhau chưa kìa! Sau này chắc sẽ đẻ cả đàn sâu con! Hì hì hì!',
            'Uy Tiểu Manh! Viên kim cương của mẹ đâu rồi?'):
            with self.subTest(text=text):
                row = self.row(text)
                original = copy.deepcopy(row)
                pages = self.chunks(row)
                self.assert_complete(row, pages)
                self.assertTrue(all(len(page['text'].split()) <= 6 for page in pages))
                self.assertTrue(all(page['font_size'] == self.params['font_size'] for page in pages))
                self.assertEqual(row, original)

    def test_page_transitions_follow_first_word_and_retain_silence(self):
        text = 'Một hai ba bốn năm sáu bảy tám'
        row = self.row(text, duration=16.)
        row['caption_words'][4]['start'] += 1
        params = self.params | {'subtitle_chunk_words': 4}
        pages = self.chunks(row, params=params)
        self.assert_complete(row, pages, params=params)
        self.assertEqual(pages[1]['start'], row['caption_words'][4]['start'])
        self.assertGreater(pages[0]['end'], pages[0]['caption_words'][-1]['end'])
        self.assertEqual(pages[0]['word_timing_source'], 'aligned')
        self.assertEqual(pages[1]['caption_words'], row['caption_words'][4:])

    def test_dense_cues_increase_page_cadence_without_shortening(self):
        text = 'một hai ba bốn năm sáu bảy tám chín mười'
        slow, fast = self.row(text, 10), self.row(text, 2)
        slow_pages, fast_pages = self.chunks(slow), self.chunks(fast)
        self.assertEqual([p['text'] for p in slow_pages], [p['text'] for p in fast_pages])
        self.assertTrue(all(b['end'] - b['start'] < a['end'] - a['start']
            for a, b in zip(slow_pages, fast_pages)))
        self.assert_complete(fast, fast_pages)

    def test_whitespace_and_explicit_newlines_collapse_without_lost_words(self):
        text = 'Xin\nchào\t bạn!\r\nHôm nay\u00a0trời   đẹp.'
        row = self.row(text)
        pages = self.chunks(row)
        self.assert_complete(row, pages, text)
        self.assertTrue(all('\t' not in page['text'] for page in pages))

    def test_short_remainder_is_rebalanced_without_tiny_final_page(self):
        text = 'a b c d e f g'
        row = self.row(text)
        pages = self.chunks(row)
        self.assertEqual([len(page['text'].split()) for page in pages], [5, 2])
        self.assert_complete(row, pages)

    def test_natural_clause_boundaries_preserve_punctuation(self):
        text = 'Á! Đau, đau quá! Em cũng có mặt mà! Sao mẹ chỉ đánh mỗi con?'
        row = self.row(text)
        pages = self.chunks(row)
        self.assertEqual(pages[0]['text'], 'Á! Đau, đau quá!')
        self.assert_complete(row, pages)

    def test_narrow_portrait_frame_packs_fewer_words_at_selected_size(self):
        text = 'Chúng ta đang kiểm tra phụ đề trên video dọc bằng điện thoại'
        row = self.row(text)
        media = {'width': 360, 'height': 640}
        pages = self.chunks(row, media=media)
        self.assert_complete(row, pages, media=media)
        self.assertTrue(all(page['font_size'] == 48 for page in pages))
        self.assertTrue(any(len(page['text'].split()) < 6 for page in pages))

    def test_oversized_word_uses_consistent_cue_font_without_splitting_letters(self):
        text = 'xin supercalifragilisticexpialidocious chào bạn'
        row = self.row(text)
        media = {'width': 360, 'height': 640}
        pages = self.chunks(row, media=media)
        self.assert_complete(row, pages, media=media)
        self.assertEqual(len({page['font_size'] for page in pages}), 1)
        self.assertLess(pages[0]['font_size'], 48)
        self.assertIn('supercalifragilisticexpialidocious', ' '.join(p['text'] for p in pages))

    def test_unfit_word_is_reported_at_readable_font_floor(self):
        row = self.row('x' * 1000)
        media = {'width': 320, 'height': 640}
        pages = self.chunks(row, media=media)
        self.assert_complete(row, pages, media=media)
        self.assertEqual(pages[0]['font_size'], 8)
        self.assertTrue(pages[0]['layout_overflow'])
        self.assertEqual(pages[0]['text'], row['translated'])

    def test_literal_ass_controls_are_measured_as_the_safe_visible_glyphs(self):
        # ASS safety replaces slash/braces with visible full-width glyphs.
        # Those glyphs must fit too, rather than fitting only the narrower input.
        row = self.row(r'{\an8} test')
        media = {'width': 240, 'height': 640}
        pages = caption_chunks(row, row['translated'], self.params, media)
        for page in pages:
            safe = page['text'].replace('\\', '＼').replace('{', '｛').replace('}', '｝')
            if not page.get('layout_overflow'):
                self.assertLessEqual(text_width(safe, page['font_size'], self.params),
                    available_width(self.params, media) + .001)

    def test_missing_mismatched_or_explicit_stale_timing_is_honestly_estimated(self):
        text = 'Xin chào bạn và chúc một ngày tốt lành'
        for variant in ('missing', 'mismatch', 'stale'):
            with self.subTest(variant=variant):
                row = self.row(text)
                if variant == 'missing':
                    row.pop('caption_words')
                elif variant == 'mismatch':
                    row['caption_words'][2]['word'] = 'other'
                else:
                    row['words'] = copy.deepcopy(row['caption_words'])
                    row['caption_words'] = []
                original = copy.deepcopy(row)
                pages = self.chunks(row)
                self.assert_complete(row, pages)
                self.assertTrue(all(page['word_timing_source'] == 'estimated' for page in pages))
                self.assertEqual(row, original)

    def test_legacy_missing_flow_keeps_one_wrapped_cue(self):
        text = 'Xin chào bạn! Đây là phụ đề dài có nhiều dòng giữ nguyên dạng cũ.'
        row = self.row(text)
        params = {k: v for k, v in self.params.items() if k != 'subtitle_flow'}
        pages = self.chunks(row, params=params)
        self.assertEqual(len(pages), 1)
        self.assertIn('\n', pages[0]['text'])
        self.assertEqual(pages[0]['text'].split(), text.split())

    def test_schema_rejects_fractional_words_and_keeps_legacy_project_layout(self):
        import core
        import server
        self.assertEqual(core.DEFAULTS['subtitle_flow'], 'single')
        self.assertEqual(core.restored_params({})['subtitle_flow'], 'wrap')
        self.assertEqual(core.restored_params({'subtitle_flow': 'single'})['subtitle_flow'], 'single')
        for value in (3.5, '4.2', True, False, 1, 13, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                server.validate_params({'subtitle_chunk_words': value})
        self.assertEqual(server.validate_params({'subtitle_chunk_words': 4})['subtitle_chunk_words'], 4)

    def test_caption_fit_error_is_export_only_and_quality_remains_read_only(self):
        import core
        from subtitle_quality import inspect_subtitles
        row = self.row('x' * 1000)
        project = {'media': {'width': 320, 'height': 640, 'duration': 12,
            'path': str(Path(__file__).resolve())}, 'params': core.DEFAULTS | self.params,
            'segments': [row]}
        before = copy.deepcopy(project)
        review = inspect_subtitles(project)
        self.assertIn('caption_fit', [issue['code'] for issue in review['issues']])
        self.assertEqual(project, before)
        project['params']['burn_subtitles'] = False
        before = copy.deepcopy(project)
        review = inspect_subtitles(project)
        self.assertNotIn('caption_fit', [issue['code'] for issue in review['issues']])
        self.assertEqual(project, before)

    def test_centisecond_collapsing_pages_merge_without_missing_tokens(self):
        text = 'a b c d e f g h i j k l m n o p q r s t'
        row = {'start': 0., 'end': .012, 'translated': text, 'caption_words': [],
            'word_timing_source': 'estimated'}
        pages = self.chunks(row)
        self.assert_complete(row, pages)
        for page in pages:
            first = (round(page['start'] * 1000) + 5) // 10
            last = (round(page['end'] * 1000) + 5) // 10
            self.assertLess(first, last)

    def test_invalid_cues_produce_no_pages(self):
        for row in ({'start': -1, 'end': 1}, {'start': 1, 'end': 1},
            {'start': 2, 'end': 1}, {'start': 0, 'end': math.inf}, {'start': 'bad', 'end': 1}, {}):
            with self.subTest(row=row):
                self.assertEqual(self.chunks(row, 'hello'), [])
        self.assertEqual(self.chunks({'start': 0, 'end': 2}, '  \n'), [])

    def test_deterministic_random_phrases_preserve_width_and_word_order(self):
        rng = random.Random(2417)
        vocabulary = 'Á! Xin chào mọi người mình đang làm phụ đề cảm xúc đọc chậm nhanh hi hi!'.split()
        for iteration in range(100):
            text = ' '.join(rng.choice(vocabulary) for _ in range(rng.randint(1, 80)))
            row = self.row(text, duration=rng.uniform(.05, 30.))
            params = self.params | {'subtitle_chunk_words': rng.randint(2, 12)}
            media = {'width': rng.randint(320, 1920), 'height': 1080}
            with self.subTest(iteration=iteration):
                pages = self.chunks(row, params=params, media=media)
                self.assert_complete(row, pages, params=params, media=media)

    def test_installed_font_measurements_fit_pages_for_all_four_presets(self):
        text = 'Xem hai đứa yêu nhau chưa kìa! Sau này chắc sẽ đẻ cả đàn sâu con! Hì hì hì!'
        row = self.row(text)
        for preset in ('clean', 'pop', 'highlight', 'karaoke'):
            for width in (360, 720, 1280):
                params = self.params | {'subtitle_preset': preset, 'subtitle_bold': preset != 'clean'}
                media = {'width': width, 'height': 720}
                with self.subTest(preset=preset, width=width):
                    pages = caption_chunks(row, text, params, media)
                    self.assertEqual(' '.join(p['text'] for p in pages).split(), text.split())
                    for page in pages:
                        self.assertLessEqual(text_width(page['text'], page['font_size'], params),
                            available_width(params, media) + .001)

    def test_ass_pages_preserve_complete_plain_srt_and_literal_backslash(self):
        import core
        import pysubs2
        row = self.row('Xin chào bạn! Tệp C:\\Video cần giữ nguyên mọi chữ và dấu.')
        before = copy.deepcopy(row)
        params = core.DEFAULTS | self.params | {'subtitle_preset': 'highlight'}
        with tempfile.TemporaryDirectory(prefix='npk-one-line-srt-') as directory:
            srt, ass = Path(directory) / 'captions.srt', Path(directory) / 'captions.ass'
            core.save_srt([row], srt)
            core.save_srt([row], ass, ass_style=(params, self.media))
            plain, styled = pysubs2.load(str(srt)), pysubs2.load(str(ass))
            self.assertEqual(len(plain), 1)
            self.assertEqual(plain[0].plaintext, row['translated'])
            self.assertGreater(len(styled), 1)
            self.assertTrue(all(r'{\q2}' in event.text and r'\N' not in event.text for event in styled))
            display = ' '.join(event.plaintext for event in styled).replace('＼', '\\')
            self.assertEqual(display.split(), row['translated'].split())
        self.assertEqual(row, before)

    def test_user_ass_tags_cannot_inject_line_breaks_position_or_color(self):
        import core
        import pysubs2
        row = self.row(r'Xin {\an8\1c&H000000&} bạn \N và \n giữ \h nguyên mọi chữ')
        safe = row['translated'].replace('\\', '＼').replace('{', '｛').replace('}', '｝')
        with tempfile.TemporaryDirectory(prefix='npk-one-line-literal-') as directory:
            for preset in ('clean', 'pop', 'highlight', 'karaoke'):
                path = Path(directory) / (preset + '.ass')
                core.save_srt([row], path, ass_style=(core.DEFAULTS | self.params |
                    {'subtitle_preset': preset, 'subtitle_karaoke': preset == 'karaoke'}, self.media))
                doc = pysubs2.load(str(path))
                self.assertEqual(' '.join(event.plaintext for event in doc).split(), safe.split())
                self.assertTrue(all(r'\N' not in event.text and r'\an8' not in event.text for event in doc))


if __name__ == '__main__':
    with tempfile.TemporaryDirectory(prefix='npk-flow-test-data-') as isolated:
        os.environ['VIETDUB_DATA_DIR'] = isolated
        unittest.main(verbosity=2)
