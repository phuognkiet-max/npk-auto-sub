"""A durable hold applied after startup blocks each actual provider boundary."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import httpx
import providers
import worker
import auto_subtitle_director as director
import execution_admission
import core
from core import DATA, DEFAULTS
from provider_usage import prepare_lineage, read_summary
from test_auto_subtitle_director import fixture as director_fixture, response as director_response


class ProviderDispatchAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))
        self.enterContext(patch.object(core, 'DATA', self.base))
        self.monitor = self.base/'monitor.json'
        self.enterContext(patch('execution_admission.default_monitor_path', return_value=self.monitor))
        self.enterContext(patch('worker.emit'))
        self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))
        source = self.base/'source.mp4'; source.write_bytes(b'private source identity')
        self.p = {'id':uuid.uuid4().hex, 'source_revision':uuid.uuid4().hex,
                  'params':DEFAULTS | {'translation_scope':'missing', 'translation_batch':20},
                  'media':{'path':str(source), 'duration':100}, 'regions':[], 'artifacts':{},
                  'segments':[{'start':i*2., 'end':i*2.+1, 'text':'Source '+str(i),
                               'translated':'Existing '+str(i) if i<5 else ''} for i in range(25)]}
        self.jid = uuid.uuid4().hex
        self.folder = self.base/'jobs'/self.jid; self.folder.mkdir(parents=True)
        self.job = {'id':self.jid, 'kind':'pipeline', 'project_id':self.p['id'], 'automatic':True}
        self.job['api_usage_lineage'] = prepare_lineage(self.p, 'pipeline', self.jid, jobs_root=self.folder.parent)
        self.hold(False)

    def hold(self, active, project=None):
        project = project or self.p
        self.monitor.write_text(json.dumps({'project_id':project['id'], 'audit_hold':active,
            'quota_scope':'requests_per_minute', 'automatic_attempts_by_local_date':{}}), encoding='utf-8')

    def run_translation(self, handler):
        original = providers.generate
        with httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.generate', side_effect=lambda *a, **kw: original(*a, client=client, **kw)):
            return worker.translation(copy.deepcopy(self.p), self.job, self.folder)

    def director_inputs(self):
        p, document = director_fixture()
        p['params'].update(subtitle_automation='gemini', provider='gemini')
        self.hold(False, p)
        job = {'id':self.jid, 'kind':'pipeline', 'project_id':p['id'], 'automatic':True}
        return p, document, job

    def test_worker_guard_forwards_automatic_identity(self):
        with patch('execution_admission.require_admission') as admission:
            worker.require_execution_admission(self.p, self.job)
        self.assertTrue(admission.call_args.kwargs['automatic'])
        self.assertEqual(admission.call_args.kwargs['current_job_id'], self.jid)

    def test_translation_hold_after_first_response_prevents_second_reservation_and_transport(self):
        calls = []
        def handler(request):
            calls.append(request)
            self.hold(True)
            return httpx.Response(503)
        with patch('credentials.active_secret', return_value='TEST-ONLY') as vault:
            with self.assertRaises(execution_admission.AdmissionError) as caught:
                self.run_translation(handler)
        self.assertEqual(caught.exception.report['blocked_code'], 'audit_hold')
        self.assertEqual(len(calls), 1)
        self.assertEqual(vault.call_count, 1)
        summary = read_summary(self.folder)
        self.assertEqual((summary['request_attempts'], summary['lineage_request_attempts']), (1,1))
        checkpoint = json.loads((self.folder/'translation-checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(bool(r['translated']) for r in checkpoint['rows']), 5)

    def test_hold_between_vault_and_first_translation_send_preserves_checkpoint_and_sends_zero(self):
        def vault(provider):
            self.hold(True)
            return 'TEST-ONLY'
        calls = []
        with patch('credentials.active_secret', side_effect=vault):
            with self.assertRaises(execution_admission.AdmissionError):
                self.run_translation(lambda request: calls.append(request) or httpx.Response(503))
        self.assertEqual(calls, [])
        summary = read_summary(self.folder)
        self.assertEqual((summary['request_attempts'], summary['lineage_request_attempts']), (0,0))
        checkpoint = json.loads((self.folder/'translation-checkpoint.json').read_text(encoding='utf-8'))
        self.assertEqual(sum(bool(r['translated']) for r in checkpoint['rows']), 5)

    def test_director_hold_applied_during_recognition_evidence_blocks_vault_and_transport(self):
        p, document, job = self.director_inputs()
        checkpoint = self.folder/'recognition-checkpoint.json'
        checkpoint.write_bytes(b'private recognized checkpoint; preserve it')
        before = checkpoint.read_bytes()
        original = director.evidence_packet
        def after_recognition(*args):
            packet = original(*args)
            self.hold(True, p)
            return packet
        with patch('auto_subtitle_director.evidence_packet', side_effect=after_recognition), \
                patch('auto_subtitle_director._secret') as vault, patch('httpx.Client') as client:
            with self.assertRaises(execution_admission.AdmissionError):
                director.request_decision(p, document, job)
        vault.assert_not_called(); client.assert_not_called()
        self.assertEqual(checkpoint.read_bytes(), before)

    def test_director_hold_after_first_response_blocks_retry_without_second_vault_read(self):
        p, document, job = self.director_inputs()
        calls = []
        def handler(request):
            calls.append(request)
            self.hold(True, p)
            return httpx.Response(503)
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('auto_subtitle_director._secret', return_value='TEST-ONLY') as vault, \
                patch('auto_subtitle_director.image_parts', return_value=[]), patch('httpx.Client', return_value=client):
            with self.assertRaises(execution_admission.AdmissionError) as caught:
                director.request_decision(p, document, job)
        self.assertEqual(caught.exception.report['blocked_code'], 'audit_hold')
        self.assertEqual(len(calls), 1)
        self.assertEqual(vault.call_count, 1)

    def test_director_hold_after_image_collection_blocks_first_transport(self):
        p, document, job = self.director_inputs()
        calls = []
        def image_parts(*args):
            self.hold(True, p)
            return []
        client = httpx.Client(transport=httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(503)))
        with patch('auto_subtitle_director._secret', return_value='TEST-ONLY'), \
                patch('auto_subtitle_director.image_parts', side_effect=image_parts), patch('httpx.Client', return_value=client):
            with self.assertRaises(execution_admission.AdmissionError):
                director.request_decision(p, document, job)
        self.assertEqual(calls, [])
