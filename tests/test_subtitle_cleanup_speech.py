"""Cleanup contracts before raw TTS cache creation; no model or provider calls."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from speech_delivery import (delivery_window, effective_speech_rows, missing_speech_rows,
                             require_speech_text, signature_payload, spoken_text)
from speech_preparation import cache_items


PARAMS = {'subtitle_text_cleanup': True, 'tts_text_source': 'translated',
          'tts_timing_mode': 'natural', 'voice_speed': 1, 'voice_id': 'piper:fixture'}


def row(start=0, end=2, text='原文内容', translated='Đây là lời thoại.', **extra):
    return dict(start=start, end=end, text=text, translated=translated, **extra)


def project(rows, **params):
    return {'media': {'duration': 40}, 'segments': rows, 'params': PARAMS | params}


class SpeechTextCleanupTests(unittest.TestCase):
    def test_spoken_text_removes_handles_and_trailing_at_without_caption_edits(self):
        cue = row(translated='Một thế giới khác @TaiKhoan @')
        before = copy.deepcopy(cue)
        self.assertEqual(spoken_text(cue, PARAMS), 'Một thế giới khác')
        self.assertEqual(cue, before)

    def test_credit_source_is_silent_even_after_translation_loses_marker(self):
        cue = row(text='视频来源: 当地市政府社交媒体账号 @GobiernoMunicipal',
                  translated='Tài khoản mạng xã hội của chính quyền địa phương')
        self.assertEqual(spoken_text(cue, PARAMS), '')
        self.assertFalse(effective_speech_rows([cue], PARAMS)[0]['speech_enabled'])
        self.assertEqual(missing_speech_rows([cue], PARAMS), [])

    def test_legacy_cleanup_off_still_mutes_credit_and_punctuation_only_rows(self):
        params = PARAMS | {'subtitle_text_cleanup': False}
        rows = [row(text='视频来源: @Publisher', translated='Nguồn video: Publisher'),
                row(text='Lời thoại gốc', translated='@@@')]
        effective = effective_speech_rows(rows, params)
        self.assertEqual([item.get('speech_enabled') for item in effective], [False, False])
        self.assertEqual([spoken_text(item, params) for item in effective], ['', ''])
        self.assertEqual(missing_speech_rows(rows, params), [])
        self.assertEqual(rows[0]['translated'], 'Nguồn video: Publisher')

    def test_selected_credit_never_falls_back_to_other_text(self):
        cue = row(spoken_text='Nguồn video: @Author', translated='Câu dịch bình thường.')
        self.assertEqual(spoken_text(cue, PARAMS | {'tts_text_source': 'edited'}), '')
        self.assertEqual(missing_speech_rows([cue], PARAMS | {'tts_text_source': 'edited'}), [])

    def test_handle_only_source_is_silent_instead_of_reading_translated_name(self):
        cue = row(text='@Author', translated='Tác giả')
        self.assertEqual(spoken_text(cue, PARAMS), '')
        self.assertEqual(missing_speech_rows([cue], PARAMS), [])

    def test_mixed_credit_keeps_the_dialogue(self):
        cue = row(translated='Họ đã trở về. Nguồn video: @Author')
        self.assertEqual(spoken_text(cue, PARAMS), 'Họ đã trở về.')

    def test_translated_choice_is_strict_after_sanitizing_empty_translation(self):
        cue = row(translated='@Author', text='Lời thoại gốc không được đọc thay.')
        self.assertEqual(spoken_text(cue, PARAMS), '')
        self.assertEqual(missing_speech_rows([cue], PARAMS), [])

    def test_missing_translation_is_still_reported_for_dialogue(self):
        cue = row(translated='', text='Lời thoại gốc')
        self.assertEqual(missing_speech_rows([cue], PARAMS), [0])
        with self.assertRaisesRegex(ValueError, 'thiếu nội dung'):
            require_speech_text([cue], PARAMS)

    def test_only_credits_gives_actionable_no_narration_error(self):
        cue = row(text='来源: @Author', translated='')
        with self.assertRaisesRegex(ValueError, 'Không có câu bật lồng tiếng'):
            require_speech_text([cue], PARAMS)

    def test_translated_triplet_speaks_once_and_preserves_all_caption_rows(self):
        rows = [row(0, 2, translated='Nhưng bên dưới là một thế giới khác.'),
                row(2.1, 4, translated='Nhưng bên dưới là một thế giới khác. @Author'),
                row(4.1, 6, translated='Nhưng bên dưới là một thế giới khác.')]
        before = copy.deepcopy(rows)
        result = effective_speech_rows(rows, PARAMS)
        self.assertEqual(len(result), 3)
        self.assertEqual([spoken_text(cue, PARAMS) for cue in result],
                         ['Nhưng bên dưới là một thế giới khác.', '', ''])
        self.assertEqual([(cue['start'], cue['end']) for cue in result], [(0, 2), (2.1, 4), (4.1, 6)])
        self.assertEqual([cue['translated'] for cue in result], [cue['translated'] for cue in rows])
        self.assertEqual(rows, before)
        self.assertTrue(all('_speech_comparison_text' not in cue for cue in result))
        self.assertEqual(effective_speech_rows(result, PARAMS), result)

    def test_dedup_uses_the_chosen_translation_instead_of_stale_edited_copy(self):
        rows = [row(0, 2, translated='Đây là một lời thoại dài hơn.', spoken_text='Edited first.'),
                row(2.1, 4, translated='Đây là một lời thoại dài hơn.', spoken_text='Edited second.')]
        # Deliberately edited narration is protected; changing the chosen text
        # source must at least keep both source texts stable and never read @.
        result = effective_speech_rows(rows, PARAMS)
        self.assertEqual([cue['spoken_text'] for cue in result], ['Edited first.', 'Edited second.'])
        self.assertEqual(result[0]['translated'], result[1]['translated'])

    def test_source_choice_does_not_dedup_distinct_source_from_equal_translation(self):
        rows = [row(0, 2, text='Bầu trời hôm nay trong xanh.', translated='Same translation.'),
                row(2.1, 4, text='Người thợ xây bước vào nhà.', translated='Same translation.')]
        params = PARAMS | {'tts_text_source': 'source'}
        result = effective_speech_rows(rows, params)
        self.assertTrue(all(cue.get('speech_enabled', True) for cue in result))
        self.assertEqual([spoken_text(cue, params) for cue in result], [cue['text'] for cue in rows])

    def test_distant_repetition_is_retained(self):
        rows = [row(0, 2), row(15, 17)]
        result = effective_speech_rows(rows, PARAMS)
        self.assertTrue(all(cue.get('speech_enabled', True) for cue in result))

    def test_legacy_snapshots_always_sanitize_voice_but_keep_caption_text(self):
        cue = row(translated='Lời thoại @Author @')
        legacy = project([cue], subtitle_text_cleanup=False)
        absent = copy.deepcopy(legacy)
        absent['params'].pop('subtitle_text_cleanup')
        self.assertEqual(spoken_text(cue, absent['params']), 'Lời thoại')
        self.assertEqual(effective_speech_rows([cue], absent['params']), [cue])
        self.assertEqual(signature_payload(legacy), signature_payload(absent))
        clean = project([cue])
        self.assertNotEqual(signature_payload(clean), signature_payload(legacy))
        self.assertNotIn('@', str(signature_payload(legacy)))
        self.assertEqual(cue['translated'], 'Lời thoại @Author @')


class SpeechCleanupCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='npk-cleanup-cache-')
        self.addCleanup(temp.cleanup)
        self.cache = Path(temp.name)
        self.entry = {'id': 'piper:fixture', 'provider': 'piper'}
        for name, target, value in (
                ('resolve', 'voices.resolve_voice', self.entry),
                ('identity', 'voices.audio_identity', ['fixture']),
                ('measured', 'speech_preparation.measured_duration', None)):
            mocked = patch(target, return_value=value)
            setattr(self, name, mocked.start())
            self.addCleanup(mocked.stop)

    def items(self, data, **options):
        return cache_items(data, cache_root=self.cache, **options)

    def test_duplicate_and_credit_do_not_resolve_voices_or_generate_cache_keys(self):
        rows = [row(0, 2), row(2.1, 4),
                row(4.1, 6, text='视频来源: @Publisher', translated='Nguồn video: @Publisher')]
        before = copy.deepcopy(rows)
        items = self.items(project(rows))
        self.assertEqual([bool(item['key']) for item in items], [True, False, False])
        self.assertEqual([item['plan']['text'] for item in items], ['Đây là lời thoại.', '', ''])
        self.resolve.assert_called_once_with('piper:fixture', require_ready=True)
        self.identity.assert_called_once()
        self.assertEqual(rows, before)

    def test_cache_identity_is_based_on_cleaned_text(self):
        dirty = self.items(project([row(translated='Họ trở về nhà. @Account @')]))[0]
        clean = self.items(project([row(translated='Họ trở về nhà.')]))[0]
        self.assertEqual(dirty['key'], clean['key'])
        legacy = self.items(project([row(translated='Họ trở về nhà. @Account @')],
                                    subtitle_text_cleanup=False))[0]
        self.assertEqual(dirty['key'], legacy['key'])

    def test_preflight_and_worker_plans_share_exact_text_and_count(self):
        data = project([row(0, 2), row(2.1, 4)])
        rows = effective_speech_rows(data['segments'], data['params'])
        plans = [delivery_window(cue, data['params'], i + 1, 40) for i, cue in enumerate(rows)]
        expected = self.items(data)
        actual = self.items(data, entries=[self.entry, self.entry], plans=plans)
        self.assertEqual([item['key'] for item in actual], [item['key'] for item in expected])
        self.assertEqual([item['plan'] for item in actual], [item['plan'] for item in expected])

    def test_mismatched_supplied_rows_fail_before_cache_or_model_resolution(self):
        data = project([row()])
        with self.assertRaisesRegex(ValueError, 'khớp số câu'):
            self.items(data, plans=[])
        self.resolve.assert_not_called()
        self.identity.assert_not_called()

    def test_legacy_worker_plan_is_valid_when_effective_speech_text_matches(self):
        data = project([row(translated='Lời thoại @Account')])
        stale = delivery_window(data['segments'][0], data['params'] | {'subtitle_text_cleanup': False}, 1, 40)
        items = self.items(data, plans=[stale])
        self.assertEqual(items[0]['plan']['text'], 'Lời thoại')
        self.resolve.assert_called_once()
        self.identity.assert_called_once()

    def test_stale_timing_policy_cannot_bypass_cache_admission(self):
        data = project([row()])
        stale = delivery_window(data['segments'][0], data['params'], 1, 40)
        stale['mode'] = 'legacy'
        with self.assertRaisesRegex(ValueError, 'khác nội dung'):
            self.items(data, plans=[stale])
        self.resolve.assert_not_called()
        self.identity.assert_not_called()


if __name__ == '__main__':
    unittest.main()
