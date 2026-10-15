"""9Router selects OCR evidence with the same boundaries as other directors."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

import auto_subtitle_director as director
from core import DATA
from execution_admission import AdmissionError
from provider_usage import read_summary
from test_auto_subtitle_director import fixture, response


class Router9DirectorTests(unittest.TestCase):
    def setUp(self):
        self.project, self.document = fixture()
        self.project['params'].update(subtitle_automation='router9', provider='router9',
            translation_model='ag/gemini-3.8-flash', router9_base_url='http://127.0.0.1:20129/v1')
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))
        self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))

    def run_decision(self, handler, *, parts=None, job=None, progress=None):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('httpx.Client', return_value=client), \
                patch('auto_subtitle_director.image_parts', return_value=[] if parts is None else parts):
            return director.request_decision(self.project, self.document, job,
                folder=self.folder, progress=progress)

    def success(self, request):
        return httpx.Response(200, json={'choices':[{'finish_reason':'stop',
            'message':{'content':json.dumps(response(self.document))}}],
            'usage':{'prompt_tokens':123, 'completion_tokens':45, 'total_tokens':168}})

    def test_request_uses_only_router_credential_chosen_model_endpoint_and_images(self):
        calls, progress = [], []
        def transport(request):
            calls.append(request)
            return self.success(request)
        def credential(provider):
            self.assertEqual(provider, 'router9')
            return 'TEST-ONLY-ROUTER-TOKEN'
        images = [{'text':'Sampled frame at 1.000 seconds; context only.'},
                  {'inlineData':{'mimeType':'image/jpeg', 'data':'anBlZw=='}}]
        original = copy.deepcopy(self.document)
        with patch('credentials.active_secret', side_effect=credential) as vault:
            value = self.run_decision(transport, parts=images,
                progress=lambda **event: progress.append(event))
        self.assertEqual(value, response(self.document))
        self.assertEqual(self.document, original)
        self.assertEqual(vault.call_args.args, ('router9',))
        self.assertEqual(len(calls), 1)
        request = calls[0]
        self.assertEqual(str(request.url), 'http://127.0.0.1:20129/v1/chat/completions')
        self.assertEqual(request.headers['authorization'], 'Bearer TEST-ONLY-ROUTER-TOKEN')
        self.assertNotIn('x-goog-api-key', request.headers)
        body = json.loads(request.content)
        self.assertEqual(body['model'], 'ag/gemini-3.8-flash')
        self.assertEqual(body['max_tokens'], 16384)
        self.assertEqual(body['response_format'], {'type':'json_object'})
        content = body['messages'][0]['content']
        self.assertIn('Sampled frame at 1.000 seconds', content[0]['text'])
        self.assertIn('Classify EVERY track ID exactly once', content[0]['text'])
        self.assertEqual(content[1]['image_url']['url'], 'data:image/jpeg;base64,anBlZw==')
        self.assertNotIn('tools', body)
        summary = read_summary(self.folder/'subtitle-director')
        self.assertEqual(summary['request_attempts'], 1)
        self.assertEqual(summary['reported_tokens']['total_tokens'], 168)
        self.assertTrue(any('director_api_usage' in event for event in progress))
        ledger = (self.folder/'subtitle-director/api-usage.json').read_text(encoding='utf-8')
        self.assertNotIn('TEST-ONLY-ROUTER-TOKEN', ledger)
        self.assertNotIn('Caption 0', ledger)

    def test_wrong_provider_or_missing_model_never_reads_any_credentials(self):
        for changes in ({'provider':'deepseek'}, {'translation_model':''}):
            project = copy.deepcopy(self.project)
            project['params'].update(changes)
            with self.subTest(changes=changes), patch('auto_subtitle_director._secret') as vault, \
                    patch('httpx.Client') as client, self.assertRaises(ValueError):
                director.request_decision(project, self.document)
            vault.assert_not_called()
            client.assert_not_called()

    def test_invalid_endpoint_never_reads_credentials(self):
        self.project['params']['router9_base_url'] = 'http://user:password@127.0.0.1:20129/v1'
        with patch('auto_subtitle_director._secret') as vault, self.assertRaises(ValueError):
            director.request_decision(self.project, self.document)
        vault.assert_not_called()

    def test_manual_mode_preserves_recognition_checkpoint_and_editor_bytes(self):
        self.project['params']['subtitle_automation'] = 'manual'
        self.project['segments'] = [{'start':0., 'end':1., 'text':'Kept source', 'translated':'Kept edit'}]
        checkpoint = self.folder/'ocr-checkpoint.json'
        checkpoint.write_bytes(b'fixture recognition checkpoint; preserve byte-for-byte')
        before = json.dumps(self.project, sort_keys=True, ensure_ascii=False).encode('utf-8')
        checkpoint_before = checkpoint.read_bytes()
        with patch('auto_subtitle_director._secret') as vault, patch('httpx.Client') as client:
            with self.assertRaises(ValueError):
                director.request_decision(self.project, self.document, folder=self.folder)
            with self.assertRaises(ValueError):
                director.decide(self.project, self.document, self.folder)
        vault.assert_not_called()
        client.assert_not_called()
        self.assertEqual(json.dumps(self.project, sort_keys=True, ensure_ascii=False).encode('utf-8'), before)
        self.assertEqual(checkpoint.read_bytes(), checkpoint_before)
        self.assertFalse((self.folder/'subtitle-director').exists())

    def test_same_job_exhausted_director_budget_blocks_vault_and_new_send(self):
        from provider_usage import TranslationUsage
        from providers import ProviderError
        usage_folder = self.folder/'subtitle-director'
        usage_folder.mkdir()
        usage = TranslationUsage(usage_folder, 1, 1, 'router9', 'ag/gemini-3.8-flash',
            endpoint='http://127.0.0.1:20129/v1')
        usage.set_batch([0], 'Fixture prompt')
        for _ in range(usage.limit):
            usage.observe({'event':'before_send', 'attempt':1})
        with patch('auto_subtitle_director._secret') as vault, \
                patch('auto_subtitle_director.image_parts', return_value=[]), \
                patch('httpx.Client') as client, self.assertRaises(ProviderError) as caught:
            director.request_decision(self.project, self.document, folder=self.folder)
        self.assertEqual(caught.exception.status, 'budget')
        vault.assert_not_called()
        client.assert_not_called()
        self.assertEqual(read_summary(usage_folder)['request_attempts'], usage.limit)

    def test_cached_router_plan_reuses_evidence_then_invalidates_model_and_endpoint(self):
        with patch('auto_subtitle_director.request_decision', return_value=response(self.document)) as request:
            first = director.decide(self.project, self.document, self.folder)
            again = director.decide(self.project, self.document, self.folder)
            self.assertEqual(request.call_count, 1)
            self.assertEqual(first['segments'], again['segments'])
            self.assertEqual(first['selection']['mode'], 'router9_director')
            self.project['params']['translation_model'] = 'sol/model-6.1'
            director.decide(self.project, self.document, self.folder)
            self.assertEqual(request.call_count, 2)
            self.project['params']['router9_base_url'] = 'http://127.0.0.1:20130/v1'
            director.decide(self.project, self.document, self.folder)
            self.assertEqual(request.call_count, 3)

    def test_low_confidence_keeps_manual_rows_and_marks_review(self):
        import worker
        self.project['segments'] = [{'start':0., 'end':8., 'text':'Manual source', 'translated':'Kept translation'}]
        value = response(self.document)
        for item in value['tracks']:
            item['confidence'] = .3
        with patch('auto_subtitle_director.request_decision', return_value=value), patch('worker.emit'):
            result = worker._automate_ocr_result(self.project, {'id':'a'*32}, self.folder,
                self.document, {'type':'auto_subtitles', 'files':['ocr-tracks.json'], 'coverage':{}})
        self.assertTrue(result['needs_review'])
        self.assertEqual(result['segments'], self.project['segments'])
        self.assertEqual(result['coverage']['ocr_selection']['automation_mode'], 'router9')

    def test_invalid_json_decision_is_not_applied_or_automatically_retried(self):
        calls = []
        def transport(request):
            calls.append(request)
            return httpx.Response(200, json={'choices':[{'finish_reason':'stop',
                'message':{'content':json.dumps({'tracks':[]})}}]})
        client = httpx.Client(transport=httpx.MockTransport(transport))
        original = copy.deepcopy(self.project['segments'])
        with patch('httpx.Client', return_value=client), \
                patch('auto_subtitle_director.image_parts', return_value=[]), \
                patch('auto_subtitle_director._secret', return_value='TEST-ONLY'), self.assertRaises(ValueError):
            director.decide(self.project, self.document, self.folder)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.project['segments'], original)
        self.assertFalse((self.folder/'subtitle-automation.json').exists())
        summary = read_summary(self.folder/'subtitle-director')
        self.assertEqual((summary['request_attempts'], summary['usage_unknown_requests']), (1,1))

    def test_hold_before_send_stops_router_transport_and_budget_reservation(self):
        calls = []
        admission_count = 0
        def admission(*args):
            nonlocal admission_count
            admission_count += 1
            if admission_count >= 3:
                raise AdmissionError({'blocked_reason':'Fixture hold', 'blocked_code':'audit_hold'})
        with patch('auto_subtitle_director._require_provider_admission', side_effect=admission), \
                patch('auto_subtitle_director._secret', return_value='TEST-ONLY'), \
                self.assertRaises(AdmissionError):
            self.run_decision(lambda request: calls.append(request) or self.success(request))
        self.assertEqual(calls, [])
        self.assertEqual(read_summary(self.folder/'subtitle-director')['request_attempts'], 0)

    def test_hold_after_first_service_error_stops_retry_and_preserves_measured_attempt(self):
        calls = []
        held = False
        def admission(*args):
            if held:
                raise AdmissionError({'blocked_reason':'Fixture hold', 'blocked_code':'audit_hold'})
        def transport(request):
            nonlocal held
            calls.append(request)
            held = True
            return httpx.Response(503)
        with patch('auto_subtitle_director._require_provider_admission', side_effect=admission), \
                patch('auto_subtitle_director._secret', return_value='TEST-ONLY'), \
                self.assertRaises(AdmissionError):
            self.run_decision(transport)
        self.assertEqual(len(calls), 1)
        summary = read_summary(self.folder/'subtitle-director')
        self.assertEqual((summary['request_attempts'], summary['usage_unknown_requests']), (1,1))

    def test_router_automation_status_and_pipeline_recovery_can_process_existing_evidence(self):
        import pipeline_resume
        with patch('auto_subtitle_director.evidence_for_project',
                return_value=({'id':'a'*32}, self.document)):
            self.assertTrue(director.automation_status(self.project)['can_process'])
            self.assertTrue(pipeline_resume._pending_selection(self.project))

    def test_translation_uses_router_endpoint_and_keeps_completed_translations(self):
        import worker
        self.project['params'].update(translation_scope='missing', translation_batch=20)
        self.project['segments'] = [
            {'start':0., 'end':1., 'text':'Source one', 'translated':'Manual translation'},
            {'start':1., 'end':2., 'text':'Source two', 'translated':''}]
        def generate(provider, secret, model, prompt, **options):
            self.assertEqual((provider, secret, model), ('router9', 'TEST-ONLY', 'ag/gemini-3.8-flash'))
            self.assertEqual(options['base_url'], 'http://127.0.0.1:20129/v1')
            options['on_request']({'event':'before_send', 'attempt':1})
            options['on_request']({'event':'response', 'http_status':200})
            return json.dumps({'lines':[{'id':1, 'text':'Bản dịch đầy đủ'}]})
        with patch('credentials.active_secret', return_value='TEST-ONLY') as vault, \
                patch('providers.generate', side_effect=generate), patch('worker.emit'):
            result = worker.translation(self.project, {'id':'fixture'}, self.folder)
        vault.assert_called_once_with('router9')
        self.assertEqual([row['translated'] for row in result['segments']],
                         ['Manual translation', 'Bản dịch đầy đủ'])
        ledger = json.loads((self.folder/'api-usage.json').read_text(encoding='utf-8'))
        self.assertEqual(ledger['endpoint'], 'http://127.0.0.1:20129/v1')
        self.assertEqual(ledger['summary']['request_attempts'], 1)
