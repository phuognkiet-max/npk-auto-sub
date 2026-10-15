"""Spoken-edit admission and request caps; no real network or credentials."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx
import core
from core import DATA, DEFAULTS
from execution_admission import AdmissionError
from providers import ProviderError
from provider_usage import read_summary
from speech_rewrite import optimize_spoken


class RouterRewriteTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))
        self.project = {'id': uuid.uuid4().hex, 'source_revision': uuid.uuid4().hex,
            'params': DEFAULTS | {'provider': 'router9', 'translation_model': 'cx/gpt-test',
                'router9_base_url': 'http://127.0.0.1:20129/v1', 'translation_batch': 1},
            'segments': [{'start': i * 3., 'end': i * 3. + 2., 'text': 'Hello',
                          'translated': 'Xin chào', 'spoken_text': 'Lời đã sửa tay'} for i in range(3)]}
        self.job = {'id': uuid.uuid4().hex, 'kind': 'optimize_spoken', 'state': 'running',
                    'project_id': self.project['id'], 'source_revision': self.project['source_revision']}
        self.monitor = self.folder/'monitor.json'
        self.set_hold(False)
        self.enterContext(patch('execution_admission.default_monitor_path', return_value=self.monitor))
        self.enterContext(patch('core.all_items', return_value=[self.job]))
        self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))

    def set_hold(self, held):
        self.monitor.write_text(json.dumps({'project_id': self.project['id'], 'audit_hold': held,
                                            'quota_scope': 'requests_per_minute'}), encoding='utf-8')

    def install_transport(self, handler):
        native_client = httpx.Client
        self.enterContext(patch('providers.httpx.Client', side_effect=lambda **kwargs:
            native_client(**kwargs, transport=httpx.MockTransport(handler))))

    def response(self, cue):
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(
            {'lines': [{'id': cue, 'spoken_text': 'Xin chào'}]})}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 11, 'completion_tokens': 4, 'total_tokens': 15}})

    def test_initial_hold_blocks_before_vault_and_ledger_creation(self):
        self.set_hold(True)
        with patch('credentials.active_secret') as vault, patch('providers.generate') as send:
            with self.assertRaises(AdmissionError):
                optimize_spoken(self.project, self.job, self.folder)
        vault.assert_not_called()
        send.assert_not_called()
        self.assertFalse((self.folder/'speech-rewrite-api').exists())

    def test_invalid_router_model_or_endpoint_blocks_before_vault(self):
        for field, value in (('translation_model', 'cx/../wrong'), ('router9_base_url', 'https://user:PRIVATE@example.com/v1')):
            project = copy.deepcopy(self.project)
            project['params'][field] = value
            with self.subTest(field=field), patch('credentials.active_secret') as vault, patch('providers.generate') as send:
                with self.assertRaises(ValueError):
                    optimize_spoken(project, self.job, self.folder)
                vault.assert_not_called()
                send.assert_not_called()

    def test_hold_applied_during_transport_retry_stops_before_second_reservation(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.set_hold(True)
            return httpx.Response(503)

        self.install_transport(handler)
        with patch('credentials.active_secret', return_value='SYNTHETIC-TOKEN'):
            with self.assertRaises(AdmissionError):
                optimize_spoken(self.project, self.job, self.folder)
        self.assertEqual(len(calls), 1)
        summary = read_summary(self.folder/'speech-rewrite-api')
        self.assertEqual(summary['request_attempts'], 1)
        self.assertEqual(summary['usage_unknown_requests'], 1)
        self.assertFalse((self.folder/'speech-rewrite-checkpoint.json').exists())

    def test_all_retries_share_durable_cap_and_keep_completed_proposals(self):
        calls, per_cue = [], {}

        def handler(request):
            calls.append(request)
            body = json.loads(request.content)
            cue = json.loads(body['messages'][0]['content'].split('Input:\n')[1])[0]['id']
            per_cue[cue] = per_cue.get(cue, 0) + 1
            if cue == 2:
                return httpx.Response(503)
            if per_cue[cue] < 3:
                return httpx.Response(200, json={'choices': [{'message': {'content': '{"lines":[]}'}}]})
            return self.response(cue)

        self.install_transport(handler)
        before = copy.deepcopy(self.project)
        with patch('credentials.active_secret', return_value='SYNTHETIC-TOKEN'):
            with self.assertRaises(ProviderError) as caught:
                optimize_spoken(self.project, self.job, self.folder)
            self.assertEqual(caught.exception.status, 'budget')
            self.assertEqual(len(calls), 7)
            saved = json.loads((self.folder/'speech-rewrite-checkpoint.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['completed_indices'], [0, 1])
            self.assertEqual(self.project, before)
            summary = read_summary(self.folder/'speech-rewrite-api')
            self.assertEqual((summary['request_attempts'], summary['request_limit']), (7, 7))
            with self.assertRaises(ProviderError):
                optimize_spoken(self.project, self.job, self.folder)
            self.assertEqual(len(calls), 7)
            self.assertEqual(read_summary(self.folder/'speech-rewrite-api')['request_attempts'], 7)

    def test_success_tracks_usage_without_changing_subtitles_or_manual_edits(self):
        calls = []

        def handler(request):
            calls.append(request)
            self.assertEqual(str(request.url), 'http://127.0.0.1:20129/v1/chat/completions')
            body = json.loads(request.content)
            self.assertEqual(body['model'], 'cx/gpt-test')
            cue = json.loads(body['messages'][0]['content'].split('Input:\n')[1])[0]['id']
            return self.response(cue)

        self.install_transport(handler)
        before = copy.deepcopy(self.project)
        with patch('credentials.active_secret', return_value='SYNTHETIC-TOKEN'):
            result = optimize_spoken(self.project, self.job, self.folder)
        self.assertEqual(len(calls), 3)
        self.assertEqual(self.project, before)
        self.assertTrue(result['review_required'])
        self.assertEqual(result['api_usage']['request_attempts'], 3)
        self.assertEqual(result['api_usage']['reported_tokens']['total_tokens'], 45)
        self.assertEqual(result['api_usage']['completed_cues'], 3)
        ledger = json.loads((self.folder/'speech-rewrite-api/api-usage.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['endpoint'], self.project['params']['router9_base_url'])
        for private in ('SYNTHETIC-TOKEN', 'Hello', 'Lời đã sửa tay'):
            self.assertNotIn(private, json.dumps(ledger, ensure_ascii=False))
        for original, changed in zip(before['segments'], result['segments']):
            self.assertEqual({key: value for key, value in original.items() if key != 'spoken_text'},
                             {key: value for key, value in changed.items() if key != 'spoken_text'})
        self.project['params']['router9_base_url'] = 'http://127.0.0.1:20130/v1'
        with patch('credentials.active_secret') as vault, patch('providers.generate') as send:
            with self.assertRaises(ValueError):
                optimize_spoken(self.project, self.job, self.folder)
        vault.assert_not_called()
        send.assert_not_called()

    def test_read_timeout_is_not_retried_and_attempt_remains_measured(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout('PRIVATE')

        self.install_transport(handler)
        with patch('credentials.active_secret', return_value='SYNTHETIC-TOKEN'):
            with self.assertRaises(ProviderError) as caught:
                optimize_spoken(self.project, self.job, self.folder)
        self.assertEqual(caught.exception.timeout_phase, 'read')
        self.assertEqual(len(calls), 1)
        self.assertEqual(read_summary(self.folder/'speech-rewrite-api')['usage_unknown_requests'], 1)


if __name__ == '__main__':
    unittest.main()
