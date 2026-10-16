"""Actual model receipts and protected authored text, with private fixtures.

HTTP is MockTransport only; the pacing context is isolated and no credential
vault, production project, provider or inference model is contacted.
"""
from contextlib import ExitStack, nullcontext
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx
from fastapi.testclient import TestClient
import core
import server
import studio_api
import providers
from cue_editor import prepare_editor_segments
from provider_usage import TranslationUsage, read_summary


class ModelProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='model-provenance-')))
        self.stack.enter_context(patch('providers.request_slot', side_effect=lambda *a, **k: nullcontext(None)))
        self.stack.enter_context(patch('providers.time.sleep'))
        self.stack.enter_context(patch('providers.random.uniform', return_value=0))
        self.configured = 'oc/muse-test-route'
        self.usage = TranslationUsage(self.folder, 3, 20, 'router9', self.configured,
                                      endpoint='http://127.0.0.1:20129/v1')
        self.usage.set_batch([0], 'Synthetic prompt')

    def generate(self, payload, *, configured=None, before_http=None, provider='router9', responses=None):
        calls = []
        def handler(request):
            calls.append(request)
            if before_http: before_http(request)
            if responses is not None:
                return responses[min(len(calls)-1, len(responses)-1)]
            return httpx.Response(200, json=payload)
        client = self.stack.enter_context(httpx.Client(transport=httpx.MockTransport(handler)))
        output = providers.generate(provider, 'SYNTHETIC-NOT-A-REAL-TOKEN', configured or self.configured,
            'Synthetic prompt', client=client, on_request=self.usage.observe,
            base_url='http://127.0.0.1:20129/v1' if provider == 'router9' else None)
        return output, calls

    def response(self, model=None):
        payload = {'choices': [{'message': {'content': 'Synthetic result'}}],
                   'usage': {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}}
        if model is not None: payload['model'] = model
        return payload

    def test_model_is_durable_before_http_and_response_alias_is_recorded_exactly(self):
        def before_http(request):
            ledger = json.loads((self.folder/'api-usage.json').read_text(encoding='utf-8'))
            self.assertEqual(ledger['requests'][0]['configured_model'], self.configured)
            self.assertNotIn('response_model', ledger['requests'][0])
            self.assertEqual(json.loads(request.content)['model'], self.configured)
        output, calls = self.generate(self.response('muse-upstream-1.3'), before_http=before_http)
        self.assertEqual(output, 'Synthetic result'); self.assertEqual(len(calls), 1)
        summary = read_summary(self.folder)
        self.assertEqual(summary['configured_model'], self.configured)
        self.assertEqual(summary['response_model'], 'muse-upstream-1.3')
        self.assertEqual(summary['model_mismatch_requests'], 1)
        self.assertEqual(summary['reported_tokens']['total_tokens'], 10)
        self.assertEqual(summary['model_provenance'][0]['response_model_matches_configured'], False)
        self.assertNotIn('SYNTHETIC-NOT-A-REAL-TOKEN', (self.folder/'api-usage.json').read_text())

    def test_missing_response_model_stays_unknown_not_configured_fallback(self):
        self.generate(self.response())
        summary = self.usage.summary()
        self.assertNotIn('response_model', summary)
        self.assertEqual(summary['response_models'], [])
        self.assertEqual(summary['response_model_missing_requests'], 1)
        self.assertEqual(summary['model_mismatch_requests'], 0)
        self.assertNotIn('response_model', self.usage.records[0])
        self.assertEqual(self.usage.records[0]['response_model_status'], 'missing')

    def test_unsafe_reported_model_is_not_echoed_or_assumed(self):
        self.generate(self.response('DO-NOT-LOG\nPRIVATE/HEADER'))
        summary = self.usage.summary()
        self.assertEqual(self.usage.records[0]['response_model_status'], 'invalid')
        self.assertNotIn('response_model', summary)
        self.assertNotIn('DO-NOT-LOG', json.dumps(summary))

    def test_retry_keeps_configured_model_on_every_wire_attempt(self):
        values = [httpx.Response(503), httpx.Response(200, json=self.response(self.configured))]
        _, calls = self.generate(None, responses=values)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(json.loads(request.content)['model'] == self.configured for request in calls))
        self.assertEqual([record['configured_model'] for record in self.usage.records], [self.configured]*2)
        self.assertEqual(self.usage.summary()['response_model_missing_requests'], 1)
        self.assertEqual(self.usage.summary()['response_model_reported_requests'], 1)
        self.assertTrue(self.usage.records[1]['response_model_matches_configured'])

    def test_mid_job_model_change_is_rejected_before_next_transport(self):
        self.generate(self.response())
        called = []
        with self.assertRaises(providers.ProviderError) as error:
            self.generate(self.response(), configured='cx/different-model', before_http=lambda request: called.append(request))
        self.assertEqual(error.exception.status, 'model_changed')
        self.assertEqual(called, [])
        self.assertEqual(len(self.usage.records), 1)
        self.assertNotIn('SYNTHETIC-NOT-A-REAL-TOKEN', str(error.exception))

    def test_gemini_reads_model_version_without_changing_configured_request(self):
        self.configured = 'gemini-test'
        self.usage = TranslationUsage(None, 1, 20, 'gemini', self.configured)
        self.usage.set_batch([0], 'Synthetic')
        payload = {'modelVersion': 'gemini-test-version-001', 'candidates': [
            {'content': {'parts': [{'text': 'Result'}]}}]}
        _, calls = self.generate(payload, provider='gemini')
        self.assertIn('/models/gemini-test:generateContent', str(calls[0].url))
        self.assertEqual(self.usage.summary()['response_model'], 'gemini-test-version-001')

    def test_ledger_round_trip_preserves_receipts_and_rejects_forged_configured_model(self):
        self.generate(self.response(self.configured))
        restored = TranslationUsage(self.folder, 3, 20, 'router9', self.configured,
                                    endpoint='http://127.0.0.1:20129/v1')
        self.assertEqual(restored.summary()['response_model'], self.configured)
        path = self.folder/'api-usage.json'
        document = json.loads(path.read_text()); document['requests'][0]['configured_model'] = 'different-model'
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            TranslationUsage(self.folder, 3, 20, 'router9', self.configured,
                             endpoint='http://127.0.0.1:20129/v1')

    def test_generation_probe_exposes_actual_report_without_inventing_missing_model(self):
        client = self.stack.enter_context(httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=self.response('upstream-probe-model')))))
        result = providers.check_connection('router9', 'SYNTHETIC', self.configured, 'generation', client,
                                             base_url='http://127.0.0.1:20129/v1')
        self.assertEqual(result['api_usage']['configured_model'], self.configured)
        self.assertEqual(result['api_usage']['response_model'], 'upstream-probe-model')


class AuthoredTextProtectionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.folder = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='authored-text-')))
        (self.folder/'jobs').mkdir()
        for module in (core, server, studio_api):
            self.stack.enter_context(patch.object(module, 'DATA', self.folder))
        self.stack.enter_context(patch('speech_word_alignment.prune_stale_timings'))
        self.stack.enter_context(patch('preflight.inspect', return_value={'ok': True, 'errors': [], 'warnings': []}))
        self.stack.enter_context(patch.object(server, 'active_secret', return_value='SYNTHETIC-FIXTURE'))
        self.stack.enter_context(patch.object(server, 'active_key_id', return_value='SYNTHETIC-ID'))
        source = self.folder/'source.mp4'; source.write_bytes(b'synthetic source identity')
        self.project = {'id': uuid.uuid4().hex, 'name': 'Authored fixture', 'created': 1., 'updated': 10.,
            'source_revision': 'synthetic-revision', 'regions': [], 'extraction_regions': [], 'artifacts': {},
            'media': {'path': str(source), 'duration': 10., 'width': 100, 'height': 100, 'audio': False},
            'params': core.DEFAULTS | {'voice_gain': 0, 'use_clean': False, 'pipeline_tts': False,
                                     'pipeline_clean': False, 'subtitle_automation': 'manual'},
            'segments': [{'start': 0., 'end': 5., 'text': 'Source one', 'translated': 'Translation one'},
                         {'start': 5., 'end': 10., 'text': 'Source two', 'translated': 'Translation two'}]}
        core.put('project', self.project['id'], self.project)
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token': server.TOKEN}))

    def test_manual_translation_and_spoken_edits_are_marked_but_untouched_rows_are_not(self):
        for field in ('translated', 'spoken_text'):
            rows = copy.deepcopy(self.project['segments']); rows[0][field] = 'A manually authored script'
            prepared = prepare_editor_segments(self.project, rows)
            self.assertIs(prepared[0]['user_edited'], True)
            self.assertNotIn('user_edited', prepared[1])
            self.assertIs(server.validate_segments(prepared, 10.)[0]['user_edited'], True)

    def test_existing_marker_cannot_be_removed_and_timing_only_edits_do_not_create_it(self):
        self.project['segments'][0]['user_edited'] = True
        rows = copy.deepcopy(self.project['segments']); rows[0].pop('user_edited'); rows[1]['end'] = 9.9
        result = prepare_editor_segments(self.project, rows)
        self.assertTrue(result[0]['user_edited']); self.assertNotIn('user_edited', result[1])

    def test_editor_put_persists_marker_and_rejects_invalid_marker_atomically(self):
        rows = copy.deepcopy(self.project['segments']); rows[0]['translated'] = 'Manual translation'
        response = self.client.put('/api/projects/'+self.project['id'], json={
            'expected_revision': 10., 'segments': rows})
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json(); self.assertTrue(saved['segments'][0]['user_edited'])
        rows = copy.deepcopy(saved['segments']); rows[0]['user_edited'] = 'true'
        rejected = self.client.put('/api/projects/'+self.project['id'], json={
            'expected_revision': saved['updated'], 'segments': rows})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(core.get('project', self.project['id']), saved)

    def test_new_script_and_stable_identity_reorder_preserve_authored_protection(self):
        self.project['segments'][0]['user_edited'] = True
        rows = list(reversed(copy.deepcopy(self.project['segments'])))
        prepared = prepare_editor_segments(self.project, rows)
        self.assertNotIn('user_edited', prepared[0]); self.assertTrue(prepared[1]['user_edited'])
        rows.append({'start': 2., 'end': 3., 'text': 'New source', 'translated': 'New authored script'})
        self.assertTrue(prepare_editor_segments(self.project, rows)[2]['user_edited'])

    def test_overwrite_is_a_strict_job_flag_and_request_ids_bind_the_decision(self):
        endpoint = '/api/jobs'
        body = {'kind': 'translate', 'project_id': self.project['id'], 'expected_revision': 10.,
                'replace_segments': True, 'overwrite_user_edits': True, 'request_id': 'synthetic-overwrite-request'}
        for invalid in ('true', 1, None):
            response = self.client.post(endpoint, json=body | {'overwrite_user_edits': invalid})
            self.assertEqual(response.status_code, 400, response.text)
        response = self.client.post(endpoint, json=body)
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json(); self.assertIs(job['overwrite_user_edits'], True)
        packet = json.loads((self.folder/'jobs'/job['id']/'input.json').read_text(encoding='utf-8'))
        self.assertIs(packet['job']['overwrite_user_edits'], True)
        self.assertNotIn('overwrite_user_edits', packet['project']['params'])
        repeated = self.client.post(endpoint, json=body)
        self.assertEqual(repeated.json()['id'], job['id'])
        changed = self.client.post(endpoint, json=body | {'overwrite_user_edits': False})
        self.assertEqual(changed.status_code, 400, changed.text)


if __name__ == '__main__':
    unittest.main()
