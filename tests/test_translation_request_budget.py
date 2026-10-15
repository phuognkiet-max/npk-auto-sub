"""Real provider adapter with MockTransport: sends, cost counters and quota guards."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import providers
import worker
from core import DATA, DEFAULTS, ROOT
from provider_usage import TranslationUsage, read_summary
from translation_checkpoint import read_checkpoint


def project(count=40, batch=20):
    return {'params': DEFAULTS | {'translation_batch': batch, 'translation_scope': 'missing'},
            'media': {'path': str(ROOT/'tests/fixtures/demo.mp4'), 'duration': count * 2},
            'regions': [], 'artifacts': {},
            'segments': [{'start': i * 2., 'end': i * 2. + 1, 'text': 'Source '+str(i),
                          'translated': ''} for i in range(count)]}


def lines(request):
    prompt = json.loads(request.content)['contents'][0]['parts'][0]['text']
    return json.loads(prompt.split('Input:\n')[1])


def success(rows, *, wrong=False):
    value = {'lines': [] if wrong else [{'id': r['id'], 'text': 'Dịch đầy đủ '+str(r['id'])} for r in rows]}
    return httpx.Response(200, json={'candidates': [{'content': {'parts': [{'text': json.dumps(value)}]}}],
        'usageMetadata': {'promptTokenCount': 10, 'candidatesTokenCount': 3,
                          'totalTokenCount': 13, 'thoughtsTokenCount': 0}})


class TranslationRequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('credentials.active_secret', return_value='TEST-ONLY'))
        self.enterContext(patch('worker.emit'))
        self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))

    def run_translation(self, p, folder, handler):
        original = providers.generate
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.generate', side_effect=lambda *a, **kw: original(*a, client=client, **kw)):
            return worker.translation(p, {}, folder)

    def temporary(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))

    def test_healthy_375_cues_send_19_and_record_actual_response_tokens(self):
        p, folder, calls = project(375), self.temporary(), []
        def handler(request):
            rows = lines(request); calls.append([r['id'] for r in rows]); return success(rows)
        result = self.run_translation(p, folder, handler)
        measured = read_summary(folder)
        self.assertEqual(len(calls), 19)
        self.assertEqual(measured['request_limit'], 24)
        self.assertEqual(measured['request_attempts'], 19)
        self.assertEqual(measured['reported_tokens']['total_tokens'], 19 * 13)
        self.assertEqual(measured['usage_unknown_requests'], 0)
        self.assertEqual(result['api_usage'], measured)
        self.assertEqual([i for group in calls for i in group], list(range(375)))

    def test_only_failing_chunk_is_split_later_normal_chunk_stays_20(self):
        p, folder, calls = project(), self.temporary(), []
        def handler(request):
            rows = lines(request); calls.append([r['id'] for r in rows])
            if len(calls) == 1: raise httpx.ReadTimeout('DO NOT LOG THIS SECRET', request=request)
            return success(rows)
        self.run_translation(p, folder, handler)
        self.assertEqual([len(group) for group in calls], [20, 10, 10, 20])
        measured = read_summary(folder)
        self.assertEqual((measured['request_attempts'], measured['usage_unknown_requests']), (4, 1))
        self.assertNotIn('SECRET', (folder/'api-usage.json').read_text(encoding='utf-8'))

    def test_budget_is_shared_between_transport_and_format_repairs_and_keeps_good_cue(self):
        p, folder, calls = project(2, 1), self.temporary(), []
        def handler(request):
            rows = lines(request); calls.append(rows)
            if len(calls) % 2: return httpx.Response(503)
            return success(rows, wrong=len(calls) < 6)
        with self.assertRaises(providers.ProviderError) as result:
            self.run_translation(p, folder, handler)
        self.assertEqual(result.exception.status, 'budget')
        self.assertEqual(len(calls), 6, 'The seventh send must be denied before reaching transport.')
        self.assertEqual(read_checkpoint(folder/'translation-checkpoint.json')['completed_indices'], [0])
        self.assertTrue(p['segments'][0]['translated'])
        self.assertFalse(p['segments'][1]['translated'])
        measured = read_summary(folder)
        self.assertEqual((measured['request_attempts'], measured['request_limit']), (6, 6))
        self.assertEqual(measured['usage_reported_requests'], 3, 'Malformed usable HTTP200 output still consumed reported tokens.')
        self.assertEqual(measured['reported_tokens']['total_tokens'], 39)
        self.assertEqual(measured['completed_cues'], 1)
        self.assertFalse((folder/'translated.srt').exists())

    def test_429_stops_one_send_with_daily_scope_no_format_or_split(self):
        p, folder, calls = project(), self.temporary(), []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={'error': {'details': [
                {'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                 'violations': [{'quotaId': 'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}]}})
        with self.assertRaises(providers.ProviderError) as result:
            self.run_translation(p, folder, handler)
        self.assertEqual((result.exception.http_status, result.exception.quota_scope), (429, 'daily'))
        self.assertEqual(len(calls), 1)
        report = json.loads((folder/'api-usage.json').read_text(encoding='utf-8'))
        self.assertEqual(report['requests'][0]['quota_scope'], 'daily')
        self.assertEqual(report['summary']['usage_unknown_requests'], 1)
        self.assertEqual(read_checkpoint(folder/'translation-checkpoint.json')['completed_indices'], [])

    def test_resume_never_sends_already_verified_cue_again(self):
        p, folder = project(2, 1), self.temporary()
        original = copy.deepcopy(p)
        calls = []
        def handler(request):
            rows = lines(request); calls.append([r['id'] for r in rows])
            return success(rows) if rows[0]['id'] == 0 else httpx.Response(429)
        with self.assertRaises(providers.ProviderError): self.run_translation(p, folder, handler)
        resume_calls = []
        def resumed(request):
            rows = lines(request); resume_calls.append([r['id'] for r in rows]); return success(rows)
        result = self.run_translation(original, folder, resumed)
        self.assertEqual(resume_calls, [[1]])
        self.assertEqual(result['resumed_count'], 1)
        self.assertEqual(result['api_usage']['completed_cues'], 2)
        self.assertEqual(result['api_usage']['request_attempts'], 3, 'Resuming the same job cannot reset its attempt ledger/budget.')

    def test_ledger_never_accepts_prompt_or_raw_body_fields(self):
        folder = self.temporary()
        usage = TranslationUsage(folder, 20, 20, 'gemini', 'fixture')
        usage.set_batch([0], 'DO NOT LOG SOURCE TEXT')
        usage.observe({'event':'before_send','attempt':1,'prompt':'SECRET'})
        usage.observe({'event':'response','http_status':200,'body':'SECRET'})
        usage.observe({'event':'usage','usage':{'total_tokens':12,'input_tokens':True,'output_tokens':-1,'raw':'SECRET'}})
        self.assertNotIn('SECRET', (folder/'api-usage.json').read_text(encoding='utf-8'))
        self.assertNotIn('DO NOT LOG', (folder/'api-usage.json').read_text(encoding='utf-8'))
        self.assertEqual(read_summary(folder)['reported_tokens'],
                         {'input_tokens':0,'output_tokens':0,'total_tokens':12,'thinking_tokens':0,'cached_input_tokens':0})
