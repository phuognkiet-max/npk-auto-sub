"""Pure OCR-text preparation contracts; no media, credentials or providers."""
import copy
import unittest

from clean_timing import prepare_subtitle_segments
from subtitle_text import (sanitize_subtitle_text, subtitle_comparison_text,
                           subtitle_text_info)


def cue(text, start=0., end=2., **extra):
    return {'text': text, 'start': start, 'end': end, **extra}


class SubtitleTextTests(unittest.TestCase):
    def test_manual_duplicate_cues_remain_separate_and_audible(self):
        rows = [cue('A complete repeated sentence.', 0., 2.,
                    translated='Một câu lặp có chủ đích.', user_edited=True),
                cue('A complete repeated sentence.', 2., 4.,
                    translated='Một câu lặp có chủ đích.')]
        before = copy.deepcopy(rows)
        for stage in ('source', 'speech'):
            prepared = prepare_subtitle_segments(rows, stage=stage)
            self.assertEqual(prepared['segments'], before)
            self.assertTrue(prepared['needs_review'])
        self.assertEqual(rows, before)

    def test_removes_all_specified_handles_and_stray_at_signs(self):
        self.assertEqual(sanitize_subtitle_text('Hello @Account_12.foo-bar 世界 @中文账号 @ @'),
                         'Hello 世界')
        self.assertEqual(sanitize_subtitle_text('ＡＢＣ＠Account\u200b @'), 'ABC')
        self.assertEqual(sanitize_subtitle_text('Lời thoại @tài_khoản @हिन्दी @حساب @Имя'), 'Lời thoại')

    def test_sanitization_is_idempotent_and_retains_language_and_punctuation(self):
        for text in ('Xin chào, thế giới! @sample', 'مرحبا بالعالم', 'こんにちは、世界！',
                     '안녕하세요, 세계!', 'สวัสดีชาวโลก', 'école déjà vue', 'हिन्दी नमस्ते'):
            cleaned = sanitize_subtitle_text(text)
            self.assertNotIn('@', cleaned)
            self.assertEqual(sanitize_subtitle_text(cleaned), cleaned)

    def test_credit_only_retains_caption_and_reports_non_dialogue(self):
        text = '视频来源: 当地市政府社交媒体账号 @GobiernoMunicipal'
        info = subtitle_text_info(text)
        self.assertTrue(info['credit_only'])
        self.assertEqual(info['text'], '视频来源: 当地市政府社交媒体账号')
        row = prepare_subtitle_segments([cue(text)])['segments'][0]
        self.assertIs(row['speech_enabled'], False)
        self.assertNotIn('@', row['text'])

    def test_all_required_credit_labels_and_multilingual_sources(self):
        for label in ('来源:', '视频来源:', '文字来源:', '图片来源:', '公文编号:',
                      'Nguồn video:', 'Video source:', 'Image source:', 'Credits:'):
            with self.subTest(label=label):
                self.assertTrue(subtitle_text_info(label + ' Agency')['credit_only'])
        self.assertTrue(subtitle_text_info('📹 视频来源: Agency')['credit_only'])
        self.assertTrue(subtitle_text_info('【视频来源: Agency】')['credit_only'])

    def test_inline_credit_keeps_only_dialogue_prefix(self):
        self.assertEqual(sanitize_subtitle_text('Đây là câu thoại. Nguồn video: Agency @abc'),
                         'Đây là câu thoại.')
        self.assertEqual(sanitize_subtitle_text('这里是另一个世界 来源：社交媒体账号 @account'),
                         '这里是另一个世界')

    def test_separate_credit_line_does_not_eat_dialogue_lines(self):
        text = 'First real dialogue.\n视频来源: Agency @xyz\nSecond real dialogue.'
        info = subtitle_text_info(text)
        self.assertFalse(info['credit_only'])
        self.assertEqual(info['text'], 'First real dialogue.\nSecond real dialogue.')

    def test_normal_source_words_without_label_are_not_credits(self):
        for text in ('Nguồn nước sạch rất quan trọng', 'The source is a spring.',
                     '来源决定质量', 'Video source matters', 'A resource: clean water'):
            self.assertEqual(sanitize_subtitle_text(text), text)
            self.assertFalse(subtitle_text_info(text)['credit_only'])

    def test_comparison_ignores_case_whitespace_punctuation_and_symbols(self):
        self.assertEqual(subtitle_comparison_text('Hello, WORLD! @abc ★'), 'helloworld')
        self.assertEqual(subtitle_comparison_text('世界，您好！'), '世界您好')
        self.assertEqual(subtitle_comparison_text('ÉCOLE'), 'école')
        self.assertNotEqual(subtitle_comparison_text('कल'), subtitle_comparison_text('कला'))

    def test_required_script_joiners_are_not_misclassified_as_ocr_noise(self):
        text = 'می\u200cروم क्\u200dष'
        self.assertEqual(sanitize_subtitle_text(text), text)


class SourcePreparationTests(unittest.TestCase):
    def test_required_three_chinese_segments_merge_to_one_without_at(self):
        words = ['但底下却是另一个世界', '但底下却是另一个世界 @', '但底下却是另一个世界']
        rows = [cue(text, i * 2., (i + 1) * 2.) for i, text in enumerate(words)]
        before = copy.deepcopy(rows)
        result = prepare_subtitle_segments(rows)
        self.assertEqual(rows, before)
        self.assertEqual(result['input_count'], 3)
        self.assertEqual(result['output_count'], 1)
        self.assertEqual(result['source_indices'], [[0, 1, 2]])
        self.assertEqual(result['segments'], [cue(words[0], 0., 6.)])
        self.assertNotIn('@', result['segments'][0]['text'])
        self.assertFalse(result['needs_review'])
        self.assertTrue(any(' @' in r.get('text', '') for x in result['changes'] for r in x['before']))

    def test_vietnamese_triple_and_cleanest_caption_selection(self):
        rows = [cue('Một thế giới khác nằm ở bên dưới @abc', 0., 2.),
                cue('Một thế giới khác nằm ở bên dưới.', 2., 4.),
                cue('Một thế giới khác nằm ở bên dưới', 4., 6.)]
        result = prepare_subtitle_segments(rows)
        self.assertEqual(len(result['segments']), 1)
        self.assertEqual(result['segments'][0]['text'], 'Một thế giới khác nằm ở bên dưới.')

    def test_fuzzy_and_substring_merges_are_flagged_for_review(self):
        pairs = [('The whole complete sentence here.', 'The whole complete sentence there.'),
                 ('但底下却是另一个世界', '底下却是另一个世界')]
        for a, b in pairs:
            result = prepare_subtitle_segments([cue(a), cue(b, 2.5, 4.5)])
            self.assertEqual(result['output_count'], 1)
            self.assertTrue(result['needs_review'])
            self.assertEqual(result['changes'][-1]['kind'], 'duplicate_merge')

    def test_requested_fuzzy_policy_does_not_claim_different_numbers_are_equal(self):
        result = prepare_subtitle_segments([cue('There are exactly 100 fishes.'),
                                            cue('There are exactly 101 fishes.', 2., 4.)])
        self.assertEqual(result['output_count'], 1)
        self.assertTrue(result['needs_review'])

    def test_gap_one_point_five_is_inclusive_but_larger_gap_does_not_merge(self):
        a = 'A complete sentence with many words.'
        for gap, expected in ((1.5, 1), (1.5001, 2)):
            result = prepare_subtitle_segments([cue(a), cue(a, 2 + gap, 4 + gap)])
            self.assertEqual(result['output_count'], expected)

    def test_sliding_window_twelve_seconds_limits_long_duplicate_chain(self):
        a = 'A complete sentence with many words.'
        rows = [cue(a, i * 2., i * 2. + 2.) for i in range(7)]
        result = prepare_subtitle_segments(rows)
        self.assertEqual(result['source_indices'], [list(range(6)), [6]])

    def test_overlap_merges_using_minimum_start_and_maximum_end(self):
        text = 'A sufficiently long complete sentence.'
        result = prepare_subtitle_segments([cue(text, 1., 4.), cue(text, 3., 5.)])
        self.assertEqual(result['segments'][0], cue(text, 1., 5.))

    def test_orphan_short_duration_merges_and_records_both_inputs(self):
        rows = [cue('This short opening clause', 0., .8),
                cue('continues with a fully formed sentence.', 1., 3.)]
        result = prepare_subtitle_segments(rows)
        self.assertEqual(result['output_count'], 1)
        self.assertEqual(result['segments'][0]['text'],
                         'This short opening clause continues with a fully formed sentence.')
        self.assertTrue(result['needs_review'])
        self.assertEqual(result['changes'][-1]['indices'], [0, 1])

    def test_latin_short_word_and_han_short_character_orphans(self):
        for a, b, expected in (('hy vọng', 'rằng mọi việc sẽ thành công.', 'hy vọng rằng mọi việc sẽ thành công.'),
                               ('他', '终于来到了另一个世界', '他终于来到了另一个世界')):
            result = prepare_subtitle_segments([cue(a), cue(b, 2.2, 4.2)])
            self.assertEqual(result['output_count'], 1)
            self.assertEqual(result['segments'][0]['text'], expected)

    def test_long_han_kana_and_thai_captions_are_not_single_word_orphans(self):
        for a, b in (('这是一个完整句子', '另外完全不同内容'),
                     ('こんにちはみなさん', 'さようならともだち'),
                     ('สวัสดีทุกคนครับ', 'ประเทศไทยสวยงาม')):
            result = prepare_subtitle_segments([cue(a), cue(b, 2.1, 4.1)])
            self.assertEqual(result['output_count'], 2)

    def test_orphan_gap_and_six_second_limit(self):
        for start, end, expected in ((2.35, 6., 1), (2.3501, 6., 2), (2.35, 6.0001, 2)):
            result = prepare_subtitle_segments([cue('tiny word'),
                                                cue('An entirely different and full sentence.', start, end)])
            self.assertEqual(result['output_count'], expected)

    def test_credit_and_watermark_only_rows_never_merge_into_dialogue(self):
        rows = [cue('短', 0., .5), cue('视频来源: Agency @abc', .5, 1.),
                cue('@onlyhandle', 1., 1.5), cue('这是完全不同的字幕', 1.5, 3.)]
        result = prepare_subtitle_segments(rows)
        self.assertEqual(result['output_count'], 3)
        self.assertIs(result['segments'][1]['speech_enabled'], False)
        self.assertEqual(result['removed_indices'], [2])
        self.assertEqual(result['source_indices'], [[0], [1], [3]])

    def test_watermark_only_does_not_erase_custom_script_translation_or_manual_metadata(self):
        for edit in ({'translated': 'Reviewed target'}, {'spoken_text': 'My narration'},
                     {'voice_id': 'custom voice'}, {'manual': True}):
            with self.subTest(edit=edit):
                result = prepare_subtitle_segments([cue('@onlyhandle', **edit)])
                self.assertEqual(result['output_count'], 1)
                self.assertEqual(result['segments'][0]['text'], '')
                self.assertIs(result['segments'][0]['speech_enabled'], False)
                self.assertTrue(result['needs_review'])
                for key, value in edit.items():
                    self.assertEqual(result['segments'][0][key], value)

    def test_different_translation_or_custom_delivery_is_not_lost_in_merge(self):
        variants = ({'translated': 'Already translated'}, {'spoken_text': 'My reviewed narration'},
                    {'voice_id': 'different voice'}, {'speech_rate': 1.1},
                    {'pause_before_ms': 100}, {'character_name': 'Other speaker'})
        text = 'A complete sentence with many words.'
        for variant in variants:
            with self.subTest(variant=variant):
                rows = [cue(text), cue(text, 2., 4., **variant)]
                result = prepare_subtitle_segments(rows)
                self.assertEqual(result['segments'], rows)
                self.assertTrue(result['needs_review'])

    def test_equal_existing_translation_can_survive_duplicate_merge(self):
        text = 'A complete sentence with many words.'
        result = prepare_subtitle_segments([cue(text, translated='Same translation'),
                                            cue(text, 2., 4., translated='Same translation')])
        self.assertEqual(result['output_count'], 1)
        self.assertEqual(result['segments'][0]['translated'], 'Same translation')

    def test_existing_translation_is_not_attached_to_new_orphan_source(self):
        rows = [cue('Go', translated='Reviewed meaning'),
                cue('to the other part of the world.', 2.1, 4., translated='Reviewed meaning')]
        result = prepare_subtitle_segments(rows)
        self.assertEqual(result['segments'], rows)
        self.assertTrue(result['needs_review'])

    def test_selected_scope_cannot_modify_or_merge_unselected_rows(self):
        text = 'A complete sentence with many words.'
        rows = [cue(text + ' @abc'), cue(text + ' @abc', 2., 4.), cue(text + ' @abc', 4., 6.)]
        result = prepare_subtitle_segments(rows, selected_indices=[1, 2])
        self.assertEqual(result['segments'][0], rows[0])
        self.assertEqual(result['source_indices'], [[0], [1, 2]])
        self.assertNotIn('@', result['segments'][1]['text'])
        self.assertEqual(prepare_subtitle_segments(rows, selected_indices=[])['segments'], rows)

    def test_preparation_is_idempotent_and_does_not_reuse_stale_word_times(self):
        rows = [cue('hy vọng', 0., .5, words=[{'text': 'hy'}]),
                cue('rằng mọi việc sẽ thành công.', .7, 2.), cue('rằng mọi việc sẽ thành công.', 2., 4.)]
        once = prepare_subtitle_segments(rows)['segments']
        twice = prepare_subtitle_segments(once)['segments']
        self.assertEqual(once, twice)
        self.assertNotIn('words', once[0])

    def test_invalid_timing_stage_and_selection_are_rejected(self):
        for row in (cue('x', -1., 2.), cue('x', 2., 1.), cue('x', float('nan'), 2.),
                    cue('x', True, 2.), {'text': 'x'}):
            with self.subTest(row=row), self.assertRaises(ValueError):
                prepare_subtitle_segments([row])
        for selection in ([True], [100], 'all'):
            with self.assertRaises(ValueError):
                prepare_subtitle_segments([cue('x')], selected_indices=selection)
        with self.assertRaises(ValueError):
            prepare_subtitle_segments([], stage='unknown')


class SpeechPreparationTests(unittest.TestCase):
    def test_post_translation_triple_mutes_only_later_rows(self):
        rows = [cue('A source sentence.', i * 2., (i + 1) * 2., translated='Một thế giới khác ở bên dưới.')
                for i in range(3)]
        before = copy.deepcopy(rows)
        result = prepare_subtitle_segments(rows, stage='speech')
        self.assertEqual(rows, before)
        self.assertEqual(result['output_count'], 3)
        self.assertTrue(result['segments'][0].get('speech_enabled', True))
        for index in (1, 2):
            expected = dict(rows[index], speech_enabled=False)
            self.assertEqual(result['segments'][index], expected)
        self.assertEqual(prepare_subtitle_segments(result['segments'], stage='speech')['segments'],
                         result['segments'])

    def test_strict_selected_text_private_field_including_empty_is_respected(self):
        rows = [cue('Source one', _speech_comparison_text='Chosen speech'),
                cue('Source two', 2., 4., _speech_comparison_text='Chosen speech'),
                cue('Source three', 4., 6., translated='Do not fall back', _speech_comparison_text='')]
        result = prepare_subtitle_segments(rows, stage='speech')
        self.assertIs(result['segments'][1]['speech_enabled'], False)
        self.assertNotIn('speech_enabled', result['segments'][2])
        self.assertTrue(all('_speech_comparison_text' not in r for r in result['segments']))
        self.assertEqual(result['segments'][1]['text'], 'Source two')

    def test_source_credit_is_muted_even_if_translation_has_no_label(self):
        rows = [cue('视频来源: Agency @abc', translated='The local government social account')]
        result = prepare_subtitle_segments(rows, stage='speech')['segments']
        self.assertEqual(result, [dict(rows[0], speech_enabled=False)])

    def test_target_script_is_not_assumed_to_be_vietnamese(self):
        for translated in ('こんにちは世界のみなさん', 'Bonjour tout le monde.', 'مرحبا بجميع الأصدقاء'):
            rows = [cue('Source 1', translated=translated), cue('Source 2', 2., 4., translated=translated)]
            result = prepare_subtitle_segments(rows, stage='speech')
            self.assertIs(result['segments'][1]['speech_enabled'], False)

    def test_fuzzy_target_duplicate_is_muted_and_reviewed(self):
        rows = [cue('Source 1', translated='There are exactly 100 fishes here.'),
                cue('Source 2', 2., 4., translated='There are exactly 101 fishes here.')]
        result = prepare_subtitle_segments(rows, stage='speech')
        self.assertIs(result['segments'][1]['speech_enabled'], False)
        self.assertTrue(result['needs_review'])

    def test_changed_custom_voice_or_spoken_script_is_never_silently_muted(self):
        for extra in ({'spoken_text': 'An explicit edited script.'}, {'voice_id': 'Other voice'}):
            rows = [cue('Source 1', translated='Same target text.'),
                    cue('Source 2', 2., 4., translated='Same target text.', **extra)]
            result = prepare_subtitle_segments(rows, stage='speech')
            if 'spoken_text' not in extra:
                self.assertTrue(result['needs_review'])
            self.assertEqual(result['segments'], rows)


if __name__ == '__main__':
    unittest.main()
