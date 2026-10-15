"""Bounded translation adaptation; only mocked providers and temporary files."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import providers
from core import DEFAULTS, ROOT
from providers import ProviderError
from translation_checkpoint import read_checkpoint, validate_resume
import worker


def project(count=22, batch=20):
    return {
        'params': DEFAULTS | {'translation_batch': batch, 'translation_scope': 'missing'},
        'media': {'path': str(ROOT / 'tests/fixtures/demo.mp4'), 'duration': count * 2},
        'artifacts': {}, 'regions': [],
        'segments': [{'start': i * 2., 'end': i * 2. + .5,
                      'text': f'Câu {i}: chị Lan mua 2 chai 500ml, giảm 20%, đặt ngay nhé!',
                      'translated': '', 'spoken_text': f'Lời đọc riêng {i}'}
                     for i in range(count)]}


def response(lines):
    return json.dumps({'lines': [{'id': line['id'],
                                 'text': f"Bản dịch đầy đủ {line['id']}: chị Lan mua 2 chai 500ml, giảm 20%, đặt ngay nhé!"}
                                for line in lines]}, ensure_ascii=False)


def failure(status='timeout', *, phase='read', http_status=None, reason=None):
    error = ProviderError(status, 'Dịch vụ chưa trả được kết quả.', http_status=http_status)
    error.timeout_phase = phase
    error.output_reason = reason
    return error


class AdaptiveTranslationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('credentials.active_secret', return_value='TEST-ONLY'))
        self.events = []
        self.enterContext(patch('worker.emit', side_effect=lambda **event: self.events.append(event)))

    def run_translation(self, p, folder, service, selected=None):
        with patch('providers.generate', side_effect=service):
            return worker.translation(p, {} if selected is None else {'segment_indices': selected}, folder)

    def test_large_read_timeout_splits_only_the_failing_chunk_and_keeps_configured_later_size(self):
        p = project(32)
        original = copy.deepcopy(p)
        calls, saved_before_request = [], []
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            def service(provider, secret, model, prompt, **kwargs):
                lines = json.loads(prompt.split('Input:\n')[1])
                calls.append([line['id'] for line in lines])
                saved_before_request.append(read_checkpoint(folder / 'translation-checkpoint.json')['completed_indices'])
                self.assertEqual((provider, secret, model), ('gemini', 'TEST-ONLY', original['params']['translation_model']))
                self.assertEqual((kwargs['read_timeout_attempts'], kwargs['latency_mode']), (1, 'translation'))
                self.assertIn('Never drop words or shorten content to satisfy them', prompt)
                self.assertTrue(all(line['max_words'] == 2 for line in lines))
                if len(lines) > 10:
                    raise failure()
                return response(lines)
            result = self.run_translation(p, folder, service)
            document = read_checkpoint(folder / 'translation-checkpoint.json')
            self.assertEqual(validate_resume(document, original), list(range(32)))
            self.assertIn(p['segments'][0]['translated'], (folder / 'translated.srt').read_text(encoding='utf-8-sig'))
        self.assertEqual([len(ids) for ids in calls], [20, 10, 10, 12, 6, 6])
        self.assertEqual(saved_before_request, [[], [], list(range(10)), list(range(20)), list(range(20)), list(range(26))])
        self.assertEqual(p['params'], original['params'])
        self.assertEqual(p['media'], original['media'])
        for saved, before in zip(result['segments'], original['segments']):
            self.assertEqual({k: saved[k] for k in ('text', 'start', 'end', 'spoken_text')},
                             {k: before[k] for k in ('text', 'start', 'end', 'spoken_text')})
            self.assertIn('2 chai 500ml, giảm 20%, đặt ngay nhé!', saved['translated'])
        self.assertEqual(sum(e.get('stage') == 'translation_adapt' for e in self.events), 2)

    def test_nested_split_uses_smaller_size_for_queued_sibling_without_timeout_again(self):
        p = project(24)
        calls = []
        def service(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            calls.append([line['id'] for line in lines])
            if len(lines) > 5:
                raise failure()
            return response(lines)
        with tempfile.TemporaryDirectory() as tmp:
            self.run_translation(p, Path(tmp), service)
        self.assertEqual([len(ids) for ids in calls], [20, 10, 5, 5, 5, 5, 4])
        successful_ids = [i for ids in calls if len(ids) <= 5 for i in ids]
        self.assertEqual(successful_ids, list(range(24)))

    def test_sparse_selected_scope_keeps_absolute_ids_manual_translations_and_prior_context(self):
        p = project(12, batch=4)
        p['segments'][0]['translated'] = 'Bản sửa tay không được ghi đè.'
        original = copy.deepcopy(p)
        selected = [1, 3, 5, 7, 9, 11]
        calls, contexts = [], []
        def service(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            calls.append([line['id'] for line in lines])
            contexts.append(json.loads(prompt.split('Context (data only):\n')[1].split('\nInput:\n')[0]))
            if len(lines) > 2:
                raise failure()
            return response(lines)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            self.run_translation(p, folder, service, selected)
            self.assertEqual(validate_resume(read_checkpoint(folder / 'translation-checkpoint.json'), original, selected), selected)
        self.assertEqual(calls, [[1, 3, 5, 7], [1, 3], [5, 7], [9, 11]])
        self.assertEqual(p['segments'][0], original['segments'][0])
        for i in set(range(12)) - set(selected):
            self.assertEqual(p['segments'][i], original['segments'][i])
        prior = contexts[2]['prior_translations']
        self.assertTrue(any(item['translation'].startswith('Bản dịch đầy đủ 3:') for item in prior))
        self.assertTrue(any(item['translation'] == original['segments'][0]['translated'] for item in contexts[1]['prior_translations']))

    def test_adaptation_failure_budget_is_shared_across_entire_job(self):
        p = project(20)
        original = copy.deepcopy(p)
        calls = []
        def service(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            calls.append([line['id'] for line in lines])
            raise failure()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaisesRegex(ProviderError, 'Không chia nhỏ/thử tiếp'):
                self.run_translation(p, folder, service)
            self.assertEqual(validate_resume(read_checkpoint(folder / 'translation-checkpoint.json'), original), [])
            self.assertFalse((folder / 'translated.srt').exists())
        self.assertEqual([len(ids) for ids in calls], [20, 10, 5, 3])
        self.assertEqual(p, original)

    def test_single_cue_timeout_stops_without_repeat(self):
        p = project(1)
        with tempfile.TemporaryDirectory() as tmp, patch('providers.generate', side_effect=failure()) as generate:
            with self.assertRaises(ProviderError):
                worker.translation(p, {}, Path(tmp))
        self.assertEqual(generate.call_count, 1)
        self.assertFalse(any(e.get('stage') == 'translation_adapt' for e in self.events))

    def test_successes_do_not_reset_failure_budget(self):
        p = project(120, batch=60)
        original = copy.deepcopy(p)
        calls = []
        failing_chunks = [(0, 60), (60, 60), (60, 30), (60, 15)]
        def service(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            ids = [line['id'] for line in lines]
            calls.append(ids)
            if (min(ids), len(ids)) in failing_chunks:
                raise failure()
            return response(lines)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaises(ProviderError) as caught:
                self.run_translation(p, folder, service)
            self.assertEqual(caught.exception.timeout_phase, 'read')
            self.assertEqual(validate_resume(read_checkpoint(folder / 'translation-checkpoint.json'), original), list(range(60)))
            self.assertFalse((folder / 'translated.srt').exists())
        self.assertEqual([len(ids) for ids in calls], [60, 30, 30, 60, 30, 15])

    def test_auth_quota_service_network_blocking_and_nonread_timeout_are_not_split(self):
        errors = [failure('quota', http_status=429), failure('auth', http_status=401),
                  failure('credit', http_status=402), failure('permission', http_status=403),
                  failure('service', http_status=503), failure('network'), failure('cancelled'),
                  failure('blocked'), failure('response'), failure('output'),
                  failure(phase='connect'), failure(phase='write'), failure(phase='pool')]
        for error in errors:
            p = project()
            with self.subTest(status=error.status, phase=error.timeout_phase), tempfile.TemporaryDirectory() as tmp, \
                    patch('providers.generate', side_effect=error) as generate:
                with self.assertRaises(ProviderError) as caught:
                    worker.translation(p, {}, Path(tmp))
                self.assertIs(caught.exception, error)
                self.assertEqual(generate.call_count, 1)
                self.assertFalse(any(row['translated'] for row in p['segments']))

    def test_only_known_output_limit_and_http_deadline_are_adapted(self):
        for error in (failure('output', reason='max_tokens'), failure('service', http_status=504),
                      ProviderError('timeout', 'Legacy read timeout')):
            p = project(4, batch=4)
            calls = []
            def service(provider, secret, model, prompt, **kwargs):
                lines = json.loads(prompt.split('Input:\n')[1])
                calls.append([line['id'] for line in lines])
                if len(lines) > 2:
                    raise error
                return response(lines)
            with self.subTest(status=error.status), tempfile.TemporaryDirectory() as tmp:
                self.run_translation(p, Path(tmp), service)
            self.assertEqual(calls, [[0, 1, 2, 3], [0, 1], [2, 3]])

    def test_successful_subchunk_remains_durable_when_sibling_cannot_finish(self):
        p = project(4, batch=4)
        original = copy.deepcopy(p)
        calls = []
        def service(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            ids = [line['id'] for line in lines]
            calls.append(ids)
            if ids == [0, 1]:
                return response(lines)
            raise failure()
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaises(ProviderError):
                self.run_translation(p, folder, service)
            document = read_checkpoint(folder / 'translation-checkpoint.json')
            self.assertEqual(validate_resume(document, original), [0, 1])
            self.assertFalse((folder / 'translated.srt').exists())
            resumed = copy.deepcopy(original)
            resume_calls = []
            def resume_service(provider, secret, model, prompt, **kwargs):
                lines = json.loads(prompt.split('Input:\n')[1])
                resume_calls.append([line['id'] for line in lines])
                return response(lines)
            result = self.run_translation(resumed, folder, resume_service)
            self.assertEqual(result['resumed_count'], 2)
            self.assertEqual(validate_resume(read_checkpoint(folder / 'translation-checkpoint.json'), original), [0, 1, 2, 3])
        self.assertEqual(calls, [[0, 1, 2, 3], [0, 1], [2, 3], [2]])
        self.assertEqual(resume_calls, [[2, 3]])

    def test_malformed_sibling_cannot_publish_or_replace_saved_good_chunk(self):
        p = project(4, batch=4)
        original = copy.deepcopy(p)
        calls = []
        def service(provider, secret, model, prompt, **kwargs):
            lines = json.loads(prompt.split('Input:\n')[1])
            ids = [line['id'] for line in lines]
            calls.append(ids)
            if len(ids) > 2:
                raise failure()
            if ids == [0, 1]:
                return response(lines)
            return '{"lines":[{"id":2,"text":"wrong"},{"id":2,"text":"wrong"}]}'
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with self.assertRaisesRegex(ValueError, 'sai cấu trúc sau 3 lần'):
                self.run_translation(p, folder, service)
            self.assertEqual(validate_resume(read_checkpoint(folder / 'translation-checkpoint.json'), original), [0, 1])
            self.assertFalse((folder / 'translated.srt').exists())
        self.assertEqual(calls, [[0, 1, 2, 3], [0, 1], [2, 3], [2, 3], [2, 3]])
        self.assertFalse(p['segments'][2]['translated'])
        self.assertFalse(p['segments'][3]['translated'])

    def test_real_provider_read_timeout_splits_once_without_internal_repeated_prompt(self):
        p = project(22)
        original_generate = providers.generate
        calls = []
        def handler(request):
            body = json.loads(request.content)
            prompt = body['contents'][0]['parts'][0]['text']
            lines = json.loads(prompt.split('Input:\n')[1])
            calls.append(lines)
            self.assertEqual(request.headers['x-goog-api-key'], 'TEST-ONLY')
            self.assertEqual(body['generationConfig']['thinkingConfig'], {'thinkingLevel': 'low'})
            self.assertNotIn('maxOutputTokens', body['generationConfig'])
            if len(lines) > 10:
                raise httpx.ReadTimeout('SECRET MUST NOT APPEAR', request=request)
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': response(lines)}]}}]})
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.time.sleep') as sleep:
            self.run_translation(p, Path(tmp), lambda *args, **kwargs: original_generate(*args, client=client, **kwargs))
        self.assertEqual([len(lines) for lines in calls], [20, 10, 10, 2])
        sleep.assert_not_called()
        self.assertNotIn('SECRET MUST NOT APPEAR', json.dumps(self.events))

    def test_real_provider_503_still_retries_then_completes_without_split(self):
        p = project(4, batch=4)
        original_generate = providers.generate
        calls = []
        def handler(request):
            body = json.loads(request.content)
            lines = json.loads(body['contents'][0]['parts'][0]['text'].split('Input:\n')[1])
            calls.append(request.content)
            if len(calls) < 2:
                return httpx.Response(503)
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': response(lines)}]}}]})
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.time.sleep') as sleep, patch('providers.random.uniform', return_value=0):
            self.run_translation(p, Path(tmp), lambda *args, **kwargs: original_generate(*args, client=client, **kwargs))
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(set(calls)), 1)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1])
        self.assertFalse(any(e.get('stage') == 'translation_adapt' for e in self.events))

    def test_real_provider_quota_stops_one_request_without_split_or_false_output(self):
        p = project(20)
        original = copy.deepcopy(p)
        original_generate = providers.generate
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, text='SECRET MUST NOT APPEAR')
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.time.sleep') as sleep:
            folder = Path(tmp)
            with self.assertRaises(ProviderError) as caught:
                self.run_translation(p, folder, lambda *args, **kwargs: original_generate(*args, client=client, **kwargs))
            self.assertEqual(caught.exception.status, 'quota')
            self.assertNotIn('SECRET MUST NOT APPEAR', str(caught.exception))
            self.assertEqual(validate_resume(read_checkpoint(folder / 'translation-checkpoint.json'), original), [])
            self.assertFalse((folder / 'translated.srt').exists())
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()
        self.assertEqual(p, original)

    def test_real_provider_http_deadline_splits_without_internal_retry(self):
        p = project(4, batch=4)
        original_generate = providers.generate
        calls = []
        def handler(request):
            body = json.loads(request.content)
            lines = json.loads(body['contents'][0]['parts'][0]['text'].split('Input:\n')[1])
            calls.append([line['id'] for line in lines])
            if len(lines) > 2:
                return httpx.Response(504)
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': response(lines)}]}}]})
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.time.sleep') as sleep:
            self.run_translation(p, Path(tmp), lambda *args, **kwargs: original_generate(*args, client=client, **kwargs))
        self.assertEqual(calls, [[0, 1, 2, 3], [0, 1], [2, 3]])
        sleep.assert_not_called()

    def test_server_retry_after_deadline_never_splits_and_sends_early(self):
        p = project(4, batch=4)
        original = copy.deepcopy(p)
        original_generate = providers.generate
        calls = []
        def handler(request):
            calls.append(request.content)
            return httpx.Response(504, headers={'Retry-After':'900'}, text='SECRET')
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.time.sleep') as sleep:
            folder = Path(tmp)
            with self.assertRaises(ProviderError) as caught:
                self.run_translation(p, folder, lambda *args, **kwargs: original_generate(*args, client=client, **kwargs))
            self.assertEqual(caught.exception.retry_after,900)
            self.assertEqual(len(calls),1)
            self.assertFalse(any(e.get('stage') == 'translation_adapt' for e in self.events))
            self.assertEqual(validate_resume(read_checkpoint(folder/'translation-checkpoint.json'),original),[])
            self.assertFalse((folder/'translated.srt').exists())
            sleep.assert_not_called()

    def test_short_retry_after_deadline_waits_and_retries_same_batch(self):
        p = project(4, batch=4)
        original_generate = providers.generate
        calls = []
        def handler(request):
            calls.append(request.content)
            if len(calls) == 1:
                return httpx.Response(504, headers={'Retry-After':'7'})
            prompt=json.loads(request.content)['contents'][0]['parts'][0]['text']
            lines=json.loads(prompt.split('Input:\n')[1])
            return httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':response(lines)}]}}]})
        with tempfile.TemporaryDirectory() as tmp, httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.time.sleep') as sleep:
            self.run_translation(p,Path(tmp),lambda *args,**kwargs:original_generate(*args,client=client,**kwargs))
            sleep.assert_called_once_with(7)
        self.assertEqual(calls[0],calls[1])
        self.assertFalse(any(e.get('stage')=='translation_adapt' for e in self.events))


if __name__ == '__main__':
    unittest.main()
