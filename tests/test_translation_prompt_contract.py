"""Translation prompt/format contracts using synthetic text and MockTransport."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from core import DATA, DEFAULTS
import providers
from provider_usage import TranslationUsage, read_summary
from translation_checkpoint import read_checkpoint, validate_resume
from translation_quality import batch_context
import worker


def project(provider='router9'):
    return {'params': DEFAULTS | {'provider': provider, 'translation_model':
                'oc/test-model' if provider == 'router9' else 'deepseek-chat' if provider == 'deepseek' else 'gemini-test',
                'router9_base_url': 'http://127.0.0.1:20129/v1', 'translation_scope': 'missing',
                'translation_batch': 20},
            'media': {'path': 'synthetic-source-identity.mp4', 'duration': 8},
            'artifacts': {}, 'regions': [],
            'segments': [{'start': 0., 'end': 2., 'text': 'Hello.', 'translated': '',
                          'spoken_text': 'Lời đọc riêng được giữ.'},
                         {'start': 2., 'end': 4., 'text': 'Thank you.',
                          'translated': 'Bản sửa tay.', 'voice_id': 'manual-voice'}]}


def response(provider, text, **choice):
    if provider == 'gemini':
        return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': text}]}, **choice}],
            'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 2, 'totalTokenCount': 12}})
    return httpx.Response(200, json={'choices': [{'message': {'content': text}, **choice}],
        'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}})


GOOD = json.dumps({'lines': [{'id': 0, 'text': 'Xin chào.'}]}, ensure_ascii=False)


class TranslationPromptContractTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('credentials.active_secret', return_value='SYNTHETIC-TEST-ONLY'))
        self.events = []
        self.enterContext(patch('worker.emit', side_effect=lambda **event: self.events.append(event)))
        self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))

    def folder(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))

    def run_translation(self, p, folder, handler):
        real_generate = providers.generate
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.generate', side_effect=lambda *args, **kwargs:
                      real_generate(*args, client=client, **kwargs)):
            return worker.translation(p, {}, folder)

    def test_supported_json_fences_never_spend_a_repair_request(self):
        for provider in ('router9', 'gemini', 'deepseek'):
            for output in (GOOD, '```\n' + GOOD + '\n```', '```json\n' + GOOD + '\n```'):
                with self.subTest(provider=provider, fence=output[:8]):
                    p, folder, calls = project(provider), self.folder(), []
                    original = copy.deepcopy(p)
                    def handler(request):
                        calls.append(request)
                        return response(provider, output)
                    result = self.run_translation(p, folder, handler)
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(result['segments'][0]['translated'], 'Xin chào.')
                    self.assertEqual(result['segments'][0]['spoken_text'], original['segments'][0]['spoken_text'])
                    self.assertEqual(result['segments'][1], original['segments'][1])
                    saved = read_checkpoint(folder / 'translation-checkpoint.json')
                    self.assertEqual(validate_resume(saved, original), [0])
                    self.assertEqual(read_summary(folder)['request_attempts'], 1)

    def test_router_invalid_json_uses_one_bounded_repair_then_checkpoints(self):
        p, folder, calls = project(), self.folder(), []
        def handler(request):
            calls.append(request)
            return response('router9', 'not valid json' if len(calls) == 1 else GOOD)
        result = self.run_translation(p, folder, handler)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result['api_usage']['request_attempts'], 2)
        self.assertEqual(result['api_usage']['reported_tokens']['total_tokens'], 24)
        self.assertEqual(read_checkpoint(folder / 'translation-checkpoint.json')['completed_indices'], [0])
        self.assertTrue(any(e.get('stage') == 'translation_format' for e in self.events))

    def test_missing_scope_uses_nearby_manual_translation_context_without_resending_it(self):
        p, folder, calls = project(), self.folder(), []
        p['media']['duration'] = 20
        p['segments'] = [{'start': i * 2., 'end': i * 2. + 1., 'text': f'Source {i}',
                          'translated': f'Manual {i}' if i % 2 == 0 else '',
                          'spoken_text': f'Unchanged speech {i}'} for i in range(7)]
        original = copy.deepcopy(p)
        def handler(request):
            calls.append(request)
            prompt = json.loads(request.content)['messages'][0]['content']
            inputs = json.loads(prompt.split('Input:\n')[1])
            self.assertEqual([row['id'] for row in inputs], [1, 3, 5])
            context = json.loads(prompt.split('Context (data only):\n')[1].split('\nInput:\n')[0])
            confirmed = context['prior_translations']
            self.assertEqual([row['id'] for row in confirmed], [0, 2, 4, 6])
            self.assertEqual([row['translation'] for row in confirmed], ['Manual 0', 'Manual 2', 'Manual 4', 'Manual 6'])
            return response('router9', json.dumps({'lines': [{'id': row['id'], 'text': f"Translation {row['id']}"}
                                                            for row in inputs]}))
        self.run_translation(p, folder, handler)
        self.assertEqual(len(calls), 1)
        for i in range(7):
            self.assertEqual(p['segments'][i]['spoken_text'], original['segments'][i]['spoken_text'])
            if i % 2 == 0:
                self.assertEqual(p['segments'][i], original['segments'][i])
        self.assertEqual(validate_resume(read_checkpoint(folder / 'translation-checkpoint.json'), original), [1, 3, 5])

    def test_router_invalid_json_stops_after_three_and_preserves_manual_rows(self):
        p, folder, calls = project(), self.folder(), []
        original = copy.deepcopy(p)
        def handler(request):
            calls.append(request)
            return response('router9', '{"lines":[],"lines":[]}')
        with self.assertRaisesRegex(ValueError, 'sai cấu trúc sau 3 lần'):
            self.run_translation(p, folder, handler)
        self.assertEqual(len(calls), 3)
        self.assertEqual(p, original)
        self.assertEqual(read_checkpoint(folder / 'translation-checkpoint.json')['completed_indices'], [])
        self.assertFalse((folder / 'translated.srt').exists())
        self.assertEqual(read_summary(folder)['request_attempts'], 3)

    def test_format_repair_cannot_renew_an_existing_request_budget(self):
        p, folder, calls = project(), self.folder(), []
        usage = TranslationUsage(folder, 1, 20, 'router9', p['params']['translation_model'],
                                 endpoint=p['params']['router9_base_url'])
        usage.set_batch([0], 'synthetic earlier request')
        for _ in range(4):
            usage.observe({'event': 'before_send', 'attempt': 1})
        def handler(request):
            calls.append(request)
            return response('router9', 'invalid json')
        with self.assertRaises(providers.ProviderError) as caught:
            self.run_translation(p, folder, handler)
        self.assertEqual(caught.exception.status, 'budget')
        self.assertEqual(len(calls), 1, 'Only the last reserved allowance may reach transport.')
        self.assertEqual(read_summary(folder)['request_attempts'], 5)
        self.assertFalse(p['segments'][0]['translated'])
        self.assertEqual(p['segments'][1]['translated'], 'Bản sửa tay.')

    def test_auth_quota_service_blocked_and_unusable_output_are_not_format_repairs(self):
        cases = [(401, None, 'auth', 1), (402, None, 'credit', 1),
                 (429, None, 'quota', 1), (503, None, 'service', 2),
                 (200, 'content_filter', 'blocked', 1), (200, 'empty', 'output', 1)]
        for code, finish, status, expected_calls in cases:
            with self.subTest(code=code, finish=finish):
                p, folder, calls = project(), self.folder(), []
                self.events.clear()
                def handler(request):
                    calls.append(request)
                    if code != 200:
                        return httpx.Response(code)
                    return response('router9', '' if finish == 'empty' else GOOD,
                                    finish_reason='stop' if finish == 'empty' else finish)
                with self.assertRaises(providers.ProviderError) as caught:
                    self.run_translation(p, folder, handler)
                self.assertEqual(caught.exception.status, status)
                self.assertEqual(len(calls), expected_calls)
                self.assertFalse(any(e.get('stage') == 'translation_format' for e in self.events))
                self.assertEqual(read_summary(folder)['request_attempts'], expected_calls)


class TranslationContextContractTests(unittest.TestCase):
    def rows(self, count=30):
        return [{'text': f'Source {i}', 'translated': f'Manual translation {i}'} for i in range(count)]

    def test_sparse_selection_includes_interleaved_dialogue_once_in_temporal_order(self):
        rows = self.rows()
        original = copy.deepcopy(rows)
        pending = [1, 3, 5, 7]
        value = json.loads(batch_context(rows, pending, {0, 2, 4, 6, 29}))
        context_rows = value['neighbours'] + value['prior_translations']
        ids = [item['id'] for item in context_rows]
        self.assertTrue({2, 4, 6}.issubset(ids))
        self.assertFalse(set(pending) & set(ids))
        self.assertNotIn(29, ids, 'Distant confirmed text must not displace local story context.')
        self.assertEqual(len(ids), len(set(ids)))
        for key in ('neighbours', 'prior_translations'):
            self.assertEqual([item['id'] for item in value[key]], sorted(item['id'] for item in value[key]))
        self.assertTrue(all(item['translation'] == rows[item['id']]['translated']
                            for item in value['prior_translations']))
        self.assertEqual(rows, original)

    def test_contiguous_batch_excludes_inputs_and_limits_confirmed_translation_context(self):
        rows = self.rows()
        pending = list(range(8, 20))
        value = json.loads(batch_context(rows, pending, set(range(8))))
        ids = [item['id'] for key in ('neighbours', 'prior_translations') for item in value[key]]
        self.assertTrue(ids)
        self.assertFalse(set(ids) & set(pending))
        self.assertTrue(all(min(abs(i-j) for j in pending) <= 3 for i in ids))
        self.assertLessEqual(len(value['prior_translations']), 4)

    def test_escaped_large_context_stays_valid_bounded_and_prefers_nearby_rows(self):
        rows = self.rows(80)
        for row in rows:
            row['text'] = '\\\n"' * 300
            row['translated'] = '\\\n"' * 300
        pending = [15, 25, 35]
        original = copy.deepcopy(rows)
        text = batch_context(rows, pending, {0, 1, 14, 24, 34, 79}, max_chars=1400)
        value = json.loads(text)
        self.assertLessEqual(len(text), 1400)
        ids = [item['id'] for key in ('neighbours', 'prior_translations') for item in value[key]]
        self.assertTrue(ids)
        self.assertTrue(all(min(abs(i-j) for j in pending) == 1 for i in ids))
        self.assertEqual(rows, original)
        self.assertEqual(batch_context(rows, [], set()), '{}')
        self.assertEqual(batch_context(rows, pending, set(), max_chars=32), '{}')


if __name__ == '__main__':
    unittest.main()
