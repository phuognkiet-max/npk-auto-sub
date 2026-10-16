"""Generic multilingual fact arithmetic and resilient per-ID spoken proposals."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import DEFAULTS
from providers import ProviderError
from speech_rewrite import optimize_spoken
from translation_quality import (deduplicate_adjacent_vi_words, effective_words_per_second,
                                 facts, review_translation)
from speech_delivery import spoken_text, signature_payload


class NumericQuantityTests(unittest.TestCase):
    def test_vietnamese_syllable_budget_floor_keeps_higher_user_preference(self):
        for target in ('vi', 'Vietnamese', 'Tiếng Việt'):
            self.assertEqual(effective_words_per_second({'target_language': target, 'words_per_second': 3.6}), 4.8)
            self.assertEqual(effective_words_per_second({'target_language': target, 'words_per_second': 6}), 6)
        self.assertEqual(effective_words_per_second({'target_language': 'English', 'words_per_second': 3.6}), 3.6)

    def test_chinese_and_vietnamese_arithmetic_are_equal_not_factual_loss(self):
        self.assertEqual(facts('罚款1万比索'), facts('phạt 10.000 peso'))
        for source, output in (
                ('1万比索', '10.000 peso'), ('一万比索', '10 nghìn peso'),
                ('一万比索', 'mười nghìn peso'), ('一万比索', 'một vạn peso'),
                ('10000比索', '10.000peso'), ('百米', '100 mét'), ('一百米', '100m'),
                ('两米', 'hai mét'), ('两', '2'), ('半个月', 'nửa tháng'), ('半个月', '15 ngày'),
                ('1.5万比索', '15.000 peso'), ('20公斤', '20kg')):
            with self.subTest(source=source, output=output):
                self.assertEqual(facts(source), facts(output))
                self.assertEqual(review_translation(source, output), [])

    def test_real_fact_loss_addition_and_currency_changes_are_still_detected(self):
        for source, output in (('1万比索', '1.000 peso'), ('百米', '10 mét'),
                ('两米', '3 mét'), ('半个月', '5 ngày'), ('1万比索', '10.000 USD'),
                ('1万', '10.000 peso'), ('20公斤', '20m')):
            with self.subTest(source=source, output=output):
                self.assertNotEqual(facts(source), facts(output))
                self.assertTrue(review_translation(source, output))

    def test_names_and_lexical_chinese_suffixes_are_not_numeric_facts(self):
        for text in ('张三', '张三。', '统一', '两江', '一般', '但底下却是另一个世界'):
            with self.subTest(text=text):
                self.assertEqual(facts(text)['numbers'], {})
        self.assertEqual(facts('走了百米')['numbers'], {'100': 1})
        self.assertEqual(facts('分两次')['numbers'], {'2': 1})

    def test_adjacent_exact_echo_is_removed_without_crossing_punctuation_or_language(self):
        self.assertEqual(deduplicate_adjacent_vi_words('Nhưng nhưng đây cũng không phải'),
                         'Nhưng đây cũng không phải')
        self.assertEqual(deduplicate_adjacent_vi_words('Nhưng nhưng bên dưới có cá.'), 'Nhưng bên dưới có cá.')
        self.assertEqual(deduplicate_adjacent_vi_words('Nhưng nhưng nhưng có cá.'), 'Nhưng có cá.')
        self.assertEqual(deduplicate_adjacent_vi_words('Nhưngnhưng có cá.'), 'Nhưng có cá.')
        self.assertEqual(deduplicate_adjacent_vi_words('Ngày ngày đều đều nhìn cây xanh xanh.'),
                         'Ngày ngày đều đều nhìn cây xanh xanh.')
        self.assertEqual(deduplicate_adjacent_vi_words('Mãi mãi như thế.'), 'Mãi mãi như thế.')
        self.assertEqual(deduplicate_adjacent_vi_words('Rất, rất tốt. Không! Không!'), 'Rất, rất tốt. Không! Không!')
        self.assertEqual(deduplicate_adjacent_vi_words('Had had enough.', 'English'), 'Had had enough.')


class SharedSpeechSanitizationTests(unittest.TestCase):
    def test_default_spoken_text_always_strips_handles_and_stray_at_signs(self):
        row = {'text': '这是正常旁白', 'translated': 'Theo dõi @kenh.demo!' }
        self.assertEqual(spoken_text(row, {'tts_text_source': 'translated'}), 'Theo dõi !')

    def test_source_only_credit_is_never_spoken_even_when_cleanup_toggle_is_off(self):
        row = {'text': '视频来源: 当地市政府社交媒体账号 @GobiernoMunicipal',
               'translated': 'Nguồn video: tài khoản mạng xã hội của chính quyền địa phương'}
        self.assertEqual(spoken_text(row, {'tts_text_source': 'translated',
                                          'subtitle_text_cleanup': False}), '')

    def test_punctuation_only_ocr_is_not_passed_to_tts(self):
        row = {'text': '!!!', 'translated': '!!!'}
        self.assertEqual(spoken_text(row, {'tts_text_source':'translated'}), '')

    def test_sanitized_tts_identity_uses_the_actual_spoken_text(self):
        project = {'params': {'tts_text_source':'translated', 'voice_speed':1, 'voice_id':'test'},
                   'media':{'duration':4}, 'segments':[{'start':0,'end':2,
                       'text':'Lời thoại', 'translated':'Theo dõi @example'}]}
        payload = signature_payload(project)
        self.assertNotIn('@example', repr(payload))
        self.assertIn('Theo dõi', repr(payload))


class ResilientSpokenRewriteTests(unittest.TestCase):
    def project(self, count=1):
        return {'params': DEFAULTS | {'target_language': 'Vietnamese', 'translation_batch': 20},
                'segments': [dict(start=i*2, end=i*2+2, text='但底下却是另一个世界',
                    translated='Nhưng bên dưới lại là một thế giới khác.') for i in range(count)]}

    def invoke(self, project, folder, responses, job=None):
        with patch('credentials.active_secret', return_value='synthetic-test-no-vault'), \
                patch('providers.generate', side_effect=responses) as provider, patch('speech_rewrite.time.sleep'):
            result = optimize_spoken(project, job or {}, folder)
        return result, provider

    def test_two_second_nine_syllable_line_is_accepted_and_checkpoint_resumes(self):
        project = self.project()
        text = 'Nhưng bên dưới lại là một thế giới khác.'
        self.assertEqual(len(text.split()), 9)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result, _ = self.invoke(project, folder, [json.dumps({'lines': [{'id': 0, 'spoken_text': text}]})])
            self.assertTrue(result['suggestions'][0]['accepted'])
            self.assertEqual(result['suggestions'][0]['max_words'], 9)
            with patch('credentials.active_secret', side_effect=AssertionError('No provider needed')), \
                    patch('providers.generate') as provider:
                resumed = optimize_spoken(project, {}, folder)
            provider.assert_not_called()
            self.assertEqual(resumed['resumed_count'], 1)
            self.assertEqual(resumed['segments'][0]['spoken_text'], text)

    def test_factual_shorter_line_can_exceed_advisory_budget_with_warning(self):
        project = self.project()
        project['segments'][0].update(text='价格1万比索', end=1,
            translated='Món đồ này có một mức giá được công bố là 10.000 peso cho người mua.')
        text = 'Món đồ này giá 10.000 peso.'
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            result, _ = self.invoke(project, folder, [json.dumps({'lines': [{'id': 0, 'spoken_text': text}]})])
            proposal = result['suggestions'][0]
            self.assertTrue(proposal['accepted'])
            self.assertGreater(len(text.split()), proposal['max_words'])
            self.assertIn('ngân sách', proposal['timing_warning'])
            with patch('providers.generate') as provider:
                resumed = optimize_spoken(project, {}, folder)
            provider.assert_not_called()
            self.assertEqual(resumed['segments'][0]['spoken_text'], text)

    def test_factual_loss_is_not_accepted_to_meet_the_budget(self):
        project = self.project()
        project['segments'][0].update(text='价格1万比索', translated='Giá bán là 10.000 peso.')
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.invoke(project, Path(directory), ['{"lines":[{"id":0,"spoken_text":"Giá rẻ."}]}'])
        self.assertFalse(result['suggestions'][0]['accepted'])
        self.assertNotIn('spoken_text', result['segments'][0])

    def test_partial_valid_subset_is_checkpointed_before_missing_id_retry(self):
        project = self.project(2)
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            def generated(provider, secret, model, prompt, **kwargs):
                ids = [item['id'] for item in json.loads(prompt.split('Input:\n')[1])]
                seen.append(ids)
                if len(seen) == 1:
                    return '{"lines":[{"id":0,"spoken_text":"Bên dưới là một thế giới khác."},{"id":1,"spoken_text":null}]}'
                saved = json.loads((folder/'speech-rewrite-checkpoint.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['completed_indices'], [0])
                self.assertEqual(saved['rows'][0]['spoken_text'], 'Bên dưới là một thế giới khác.')
                return '{"lines":[{"id":1,"spoken_text":"Bên dưới là một thế giới khác."}]}'
            result, _ = self.invoke(project, folder, generated)
        self.assertEqual(seen, [[0, 1], [1]])
        self.assertEqual([p['index'] for p in result['suggestions']], [0, 1])

    def test_partial_checkpoint_survives_provider_failure_then_resumes_only_missing(self):
        project = self.project(2)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with patch('credentials.active_secret', return_value='synthetic-test'), patch('speech_rewrite.time.sleep'), \
                    patch('providers.generate', side_effect=[
                        '{"lines":[{"id":0,"spoken_text":"Bên dưới là thế giới khác."}]}',
                        ProviderError('quota', 'synthetic quota')]):
                with self.assertRaises(ProviderError):
                    optimize_spoken(project, {}, folder)
            seen = []
            def generated(provider, secret, model, prompt, **kwargs):
                seen.extend(item['id'] for item in json.loads(prompt.split('Input:\n')[1]))
                return '{"lines":[{"id":1,"spoken_text":"Bên dưới là thế giới khác."}]}'
            result, _ = self.invoke(project, folder, generated)
        self.assertEqual(seen, [1])
        self.assertEqual(result['resumed_count'], 1)

    def test_twenty_line_reply_saves_nineteen_and_only_retries_the_twentieth(self):
        project = self.project(20)
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            def generated(provider, secret, model, prompt, **kwargs):
                ids = [item['id'] for item in json.loads(prompt.split('Input:\n')[1])]
                seen.append(ids)
                if len(seen) == 1:
                    return json.dumps({'lines': [
                        {'id': i, 'spoken_text': 'Bên dưới là một thế giới khác.'} for i in range(19)] +
                        [{'id': 19, 'spoken_text': None}]})
                saved = json.loads((folder/'speech-rewrite-checkpoint.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['completed_indices'], list(range(19)))
                return '{"lines":[{"id":19,"spoken_text":"Bên dưới là một thế giới khác."}]}'
            result, provider = self.invoke(project, folder, generated)
        self.assertEqual(seen, [list(range(20)), [19]])
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(result['suggestions']), 20)

    def test_manual_row_is_protected_and_retained_in_context(self):
        project = self.project(2)
        project['segments'][0].update(user_edited=True, translated='Tên riêng tôi đã duyệt.', spoken_text='Lời tôi đã duyệt.')
        original = copy.deepcopy(project)
        prompts = []
        def generated(provider, secret, model, prompt, **kwargs):
            prompts.append(prompt)
            self.assertEqual([item['id'] for item in json.loads(prompt.split('Input:\n')[1])], [1])
            self.assertIn('Tên riêng tôi đã duyệt.', prompt)
            return '{"lines":[{"id":1,"spoken_text":"Bên dưới là thế giới khác."}]}'
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.invoke(project, Path(directory), generated)
        self.assertEqual(result['segments'][0], original['segments'][0])
        self.assertEqual(result['protected_indices'], [0])
        self.assertEqual(project, original)

    def test_manual_row_overwrite_requires_explicit_true(self):
        project = self.project()
        project['segments'][0]['user_edited'] = True
        with tempfile.TemporaryDirectory() as directory, \
                patch('credentials.active_secret', side_effect=AssertionError('Protected corpus needs no key')), \
                patch('providers.generate') as provider:
            protected = optimize_spoken(project, {}, Path(directory))
        provider.assert_not_called()
        self.assertEqual(protected['suggestions'], [])
        with tempfile.TemporaryDirectory() as directory:
            result, provider = self.invoke(project, Path(directory), [
                '{"lines":[{"id":0,"spoken_text":"Bên dưới là thế giới khác."}]}'],
                job={'overwrite_user_edits': True})
        self.assertTrue(result['suggestions'][0]['accepted'])
        self.assertEqual(provider.call_count, 1)

    def test_garbage_empty_and_credits_become_silent_without_provider(self):
        samples = ('@Account', '', '视频来源: @Account', '!!!', '…', '\u0301', '😀')
        project = self.project(len(samples))
        for row, text in zip(project['segments'], samples):
            row.update(text=text, translated='')
        with tempfile.TemporaryDirectory() as directory, \
                patch('credentials.active_secret', side_effect=AssertionError('No key for local silence')), \
                patch('providers.generate') as provider:
            result = optimize_spoken(project, {}, Path(directory))
            resumed = optimize_spoken(project, {}, Path(directory))
        provider.assert_not_called()
        self.assertTrue(all(row['speech_enabled'] is False for row in result['segments']))
        self.assertEqual(resumed['resumed_count'], len(samples))


if __name__ == '__main__':
    unittest.main()
