"""Pure local preview/application boundaries; no OCR, media or provider calls."""
import copy
import unittest

from dialogue_review import apply_dialogue_review, build_dialogue_review


def row(text='Đây là lời thoại nguyên vẹn', start=0, end=2, **extra):
    return {'start': start, 'end': end, 'text': text, 'translated': text, **extra}


def project(rows=None):
    return {'id': 'synthetic-project', 'updated': 20, 'source_revision': 'source-v1',
            'coverage': {'method': 'ocr'},
            'params': {'tts_text_source': 'translated', 'voice_id': 'synthetic-voice', 'voice_speed': 1},
            'segments': rows if rows is not None else [row(), row(start=2, end=4)]}


class DialogueReviewTests(unittest.TestCase):
    def test_preview_is_pure_and_stable_then_apply_returns_full_copy(self):
        p = project(); before = copy.deepcopy(p)
        preview = build_dialogue_review(p)
        self.assertEqual(p, before)
        self.assertEqual(preview, build_dialogue_review(p))
        self.assertEqual(preview['expected_revision'], 20)
        proposal = preview['proposals'][0]
        self.assertEqual(proposal['kind'], 'duplicate_group')
        self.assertEqual(proposal['indices'], [0, 1])
        self.assertEqual(proposal['keep_index'], 0)
        self.assertEqual(proposal['changes'], [{'index': 1, 'speech_enabled': False}])
        result = apply_dialogue_review(p, preview['signature'], [proposal['id']])
        expected = copy.deepcopy(before); expected['segments'][1]['speech_enabled'] = False
        self.assertEqual(result, expected)
        self.assertEqual(p, before)

    def test_overlay_suffix_group_retains_clean_row_without_editing_text(self):
        p = project([row(start=0, end=.5), row('Đây là lời thoại nguyên vẹn @tài_khoản', .5, 1),
                     row(start=1, end=2)])
        preview = build_dialogue_review(p)
        self.assertEqual(preview['proposals'][0]['indices'], [0, 1, 2])
        result = apply_dialogue_review(p, preview['signature'], [preview['proposals'][0]['id']])
        self.assertTrue(result['segments'][0].get('speech_enabled', True))
        self.assertFalse(result['segments'][1]['speech_enabled'])
        self.assertFalse(result['segments'][2]['speech_enabled'])
        self.assertEqual(result['segments'][1]['text'], p['segments'][1]['text'])
        self.assertEqual(len(preview['issues']), 1)

    def test_credit_label_requires_explicit_apply_and_targeted_ids(self):
        p = project([row(), row(start=2, end=4),
                     row('视频来源：@Municipal', 4, 6), row('Nguồn nước sạch rất quan trọng', 6, 8)])
        preview = build_dialogue_review(p)
        credit = next(x for x in preview['proposals'] if x['kind'] == 'source_credit')
        self.assertEqual(credit['indices'], [2])
        result = apply_dialogue_review(p, preview['signature'], [credit['id']])
        self.assertFalse(result['segments'][2]['speech_enabled'])
        self.assertNotIn('speech_enabled', result['segments'][1])
        self.assertNotIn('speech_enabled', result['segments'][3])

    def test_anchored_credit_variants_and_embedded_label(self):
        for label in ('视频来源：账号', '画面来源: Agency', 'Nguồn video: Agency', 'Nguồn hình ảnh: Agency'):
            with self.subTest(label=label):
                self.assertEqual(build_dialogue_review(project([row(label)]))['proposals'][0]['kind'], 'source_credit')
        for label in ('Tôi nói rằng Nguồn video: Agency', 'Nguồn nước: sông', 'Nguồn video',
                      'Nguồn video:   '):
            with self.subTest(label=label):
                self.assertFalse(build_dialogue_review(project([row(label)]))['proposals'])

    def test_changed_source_cannot_be_hidden_by_identical_translation(self):
        for text in ('Không phải lời thoại nguyên vẹn', 'Đây là lời thoại nguyên vẹn 13',
                     'Đây là lời thoại nguyên vẹn của Nam'):
            p = project([row(), row(text, 2, 4, translated='Đây là lời thoại nguyên vẹn')])
            self.assertFalse(build_dialogue_review(p)['proposals'])

    def test_different_translation_cannot_be_hidden_by_identical_source(self):
        p = project(); p['segments'][1]['translated'] = 'Đây không phải lời thoại nguyên vẹn'
        self.assertFalse(build_dialogue_review(p)['proposals'])

    def test_speech_edit_voice_rate_character_pause_or_disabled_break_group(self):
        variants = ({'spoken_text': 'Đây là lời thoại nguyên vẹn'}, {'voice_id': 'another'},
                    {'speech_rate': 1.1}, {'character_name': 'Người khác'}, {'pause_before_ms': 10},
                    {'pause_after_ms': 10}, {'speech_enabled': False})
        for change in variants:
            with self.subTest(change=change):
                p = project(); p['segments'][1].update(change)
                self.assertFalse(build_dialogue_review(p)['proposals'])

    def test_non_ocr_short_text_real_repetition_gap_and_long_group_are_not_suppressed(self):
        p = project(); p['coverage']['method'] = 'asr'
        self.assertFalse(build_dialogue_review(p)['proposals'])
        for rows in ([row('Không!'), row('Không!', 2, 4)], [row(), row(start=2.1, end=4)],
                     [row(end=7), row(start=7, end=14)], [row(), row(start=1, end=4)]):
            with self.subTest(rows=rows):
                self.assertFalse(build_dialogue_review(project(rows))['proposals'])

    def test_nfkc_whitespace_only_and_touch_tolerance(self):
        p = project([row('Đây là lời thoại ABC nguyên vẹn'),
                     row('Đây  là lời thoại ＡＢＣ nguyên vẹn', 2.08, 4)])
        self.assertEqual(len(build_dialogue_review(p)['proposals']), 1)

    def test_all_suffixes_and_email_remain_issues_without_automatic_cleaning(self):
        for text in ('Đây là lời thoại nguyên vẹn @tài_khoản', 'Liên hệ tác giả qua user@example.com'):
            p = project([row(text), row(text, 2, 4)])
            preview = build_dialogue_review(p)
            if '@tài' in text:
                self.assertFalse(preview['proposals'])
            self.assertEqual(len(preview['issues']), 2)
            self.assertTrue(all('speech_enabled' not in r for r in p['segments']))

    def test_signature_binds_revision_source_rows_and_delivery_params(self):
        p = project(); preview = build_dialogue_review(p); ids = [preview['proposals'][0]['id']]
        variants = []
        for key, value in (('updated', 21), ('source_revision', 'source-v2')):
            changed = copy.deepcopy(p); changed[key] = value; variants.append(changed)
        changed = copy.deepcopy(p); changed['segments'][0]['translated'] += ' mới'; variants.append(changed)
        changed = copy.deepcopy(p); changed['params']['tts_text_source'] = 'source'; variants.append(changed)
        changed = copy.deepcopy(p); changed['params']['voice_speed'] = 1.1; variants.append(changed)
        for changed in variants:
            before = copy.deepcopy(changed)
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                apply_dialogue_review(changed, preview['signature'], ids)
            self.assertEqual(changed, before)

    def test_unknown_duplicate_or_empty_selection_fails_before_mutation(self):
        p = project(); preview = build_dialogue_review(p); ident = preview['proposals'][0]['id']
        before = copy.deepcopy(p)
        for ids in (['unknown'], [ident, 'unknown'], [ident, ident], [], ident, [False]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                apply_dialogue_review(p, preview['signature'], ids)
            self.assertEqual(p, before)

    def test_untrusted_signature_is_a_safe_validation_error(self):
        p = project(); preview = build_dialogue_review(p); ids = [preview['proposals'][0]['id']]
        for signature in ('chữ Việt', '', None, 123, '0' * 64):
            with self.subTest(signature=signature), self.assertRaises(ValueError):
                apply_dialogue_review(p, signature, ids)


if __name__ == '__main__':
    unittest.main()
