"""Model edits stop immutable jobs; connection allowances survive those edits."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import httpx

import core
import execution_admission as admission
import providers
import worker
from provider_usage import TranslationUsage, prepare_lineage, lineage_status, validate_lineage


class ExecutionModelContractTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(dir=core.DATA)))
        self.source = self.base/'source.mp4'; self.source.write_bytes(b'fixture source')
        self.jobs = self.base/'jobs'; self.jobs.mkdir()
        self.root = uuid.uuid4().hex
        (self.jobs/self.root).mkdir()
        self.p = {'id': uuid.uuid4().hex, 'source_revision': 'fixture-revision',
            'media': {'path': str(self.source), 'duration': 20}, 'regions': [],
            'params': core.DEFAULTS | {'provider':'router9', 'translation_model':'cx/gpt-6-luna',
                'router9_base_url':'http://127.0.0.1:20129/v1', 'translation_scope':'missing'},
            'segments':[{'start':0.,'end':2.,'text':'Source','translated':''}]}

    def row(self, jid, model):
        return {'id':jid, 'project_id':self.p['id'], 'source_revision':self.p['source_revision'],
                'kind':'translate','state':'error','stage':'translate','created':1,
                'continuation_from': None if jid == self.root else self.root,
                'configuration':self.p['params'] | {'translation_model':model}}

    def prepare(self, p, history=()):
        return prepare_lineage(p, 'translate', self.root, history, jobs_root=self.jobs)

    def usage(self, jid, p, descriptor):
        folder = self.jobs/jid; folder.mkdir(exist_ok=True)
        return TranslationUsage(folder,1,20,'router9',p['params']['translation_model'],
            endpoint=p['params']['router9_base_url'], lineage=descriptor, job_id=jid, jobs_root=self.jobs)

    def send(self, usage):
        usage.set_batch([0], 'fixture')
        usage.observe({'event':'before_send','attempt':1})

    def test_explicit_new_model_keeps_original_connection_allowance(self):
        original = self.prepare(self.p)
        old = self.usage(self.root,self.p,original)
        for _ in range(4): self.send(old)
        changed = copy.deepcopy(self.p)
        changed['params']['translation_model'] = 'oc/muse-spark-1.3-contributor-free'
        history = [self.row(self.root,self.p['params']['translation_model'])]
        descriptor = self.prepare(changed,history)
        validate_lineage(descriptor,changed)
        self.assertEqual(lineage_status(descriptor,jobs_root=self.jobs)['remaining_requests'],1)
        jid = uuid.uuid4().hex
        new = self.usage(jid,changed,descriptor)
        self.send(new)
        self.assertEqual(new.summary()['lineage_request_attempts'],5)
        self.assertFalse(lineage_status(descriptor,jobs_root=self.jobs)['can_send'])
        document = json.loads((self.jobs/jid/'api-usage.json').read_text())
        self.assertEqual(document['model'],changed['params']['translation_model'])
        # Reentry of that same new-model job retains its own immutable model.
        with self.assertRaises(providers.ProviderError): self.usage(jid,changed,descriptor)

    def test_legacy_json_import_uses_historical_model_not_current_model(self):
        old = TranslationUsage(self.jobs/self.root,1,20,'router9',self.p['params']['translation_model'],
                               endpoint=self.p['params']['router9_base_url'])
        self.send(old)
        changed = copy.deepcopy(self.p); changed['params']['translation_model']='oc/muse-spark'
        descriptor = self.prepare(changed,[self.row(self.root,self.p['params']['translation_model'])])
        self.assertEqual(lineage_status(descriptor,jobs_root=self.jobs)['lineage_request_attempts'],1)
        new = self.usage(uuid.uuid4().hex,changed,descriptor)
        self.assertEqual(new.summary()['lineage_request_attempts'],1)
        forged = self.row(self.root,'oc/muse-spark')
        with self.assertRaises(ValueError): self.prepare(changed,[forged])

    def test_model_edit_cannot_turn_unknown_history_into_zero(self):
        changed = copy.deepcopy(self.p); changed['params']['translation_model']='oc/muse-spark'
        descriptor = self.prepare(changed,[self.row(self.root,self.p['params']['translation_model'])])
        self.assertEqual(lineage_status(descriptor,jobs_root=self.jobs)['blocked_code'],'history_unknown')
        with self.assertRaises(providers.ProviderError): self.usage(uuid.uuid4().hex,changed,descriptor)

    def test_same_job_cannot_change_model_even_when_json_missing(self):
        self.send(self.usage(self.root,self.p,self.prepare(self.p)))
        (self.jobs/self.root/'api-usage.json').unlink()
        changed = copy.deepcopy(self.p); changed['params']['translation_model']='oc/muse-spark'
        with self.assertRaises(ValueError): self.usage(self.root,changed,self.prepare(changed))

    def test_saved_model_change_stops_before_vault(self):
        changed = copy.deepcopy(self.p); changed['params']['translation_model']='oc/muse-spark'
        job = {'id':self.root,'kind':'translate','project_revision':1}
        with patch('core.get',return_value=changed), patch('credentials.active_secret') as vault, \
                patch('providers.generate') as generate:
            with self.assertRaises(admission.AdmissionError) as caught:
                worker.translation(copy.deepcopy(self.p),job,self.jobs/self.root)
        self.assertEqual(caught.exception.report['blocked_code'],'execution_model_changed')
        vault.assert_not_called(); generate.assert_not_called()

    def test_endpoint_edit_stops_but_cosmetic_edit_does_not(self):
        changed = copy.deepcopy(self.p); changed['params']['font_size']=80
        admission.require_execution_contract(self.p,changed,'translate')
        changed['params']['router9_base_url']='http://127.0.0.1:20128/v1'
        with self.assertRaises(admission.AdmissionError):
            admission.require_execution_contract(self.p,changed,'translate')
        admission.require_execution_contract(self.p,changed,'tts')

    def test_missing_real_project_stops_before_vault(self):
        with patch('core.get',return_value=None), patch('credentials.active_secret') as vault:
            with self.assertRaises(admission.AdmissionError):
                worker.translation(copy.deepcopy(self.p),{'id':self.root,'kind':'translate','project_revision':1},self.jobs/self.root)
        vault.assert_not_called()

    def test_complete_translation_reuse_needs_no_provider_or_current_model(self):
        complete = copy.deepcopy(self.p); complete['segments'][0]['translated']='Bản dịch giữ nguyên'
        changed = copy.deepcopy(self.p); changed['params']['translation_model']='oc/muse-spark'
        with patch('core.get',return_value=changed), patch('credentials.active_secret') as vault, \
                patch('providers.generate') as generate, patch('worker.emit'):
            result = worker.translation(complete,{'id':self.root,'kind':'translate','project_revision':1},self.jobs/self.root)
        self.assertTrue(result['reused'])
        self.assertEqual(result['segments'][0]['translated'],'Bản dịch giữ nguyên')
        vault.assert_not_called(); generate.assert_not_called()

    def test_model_saved_after_response_blocks_retry_before_second_reservation(self):
        current = copy.deepcopy(self.p)
        descriptor = self.prepare(self.p)
        calls = []
        def handler(request):
            calls.append(request)
            current['params']['translation_model']='oc/muse-spark'
            return httpx.Response(503)
        original = providers.generate
        with patch('core.get',side_effect=lambda *args:current), patch('execution_admission.require_admission'), \
                patch('credentials.active_secret',return_value='TEST-ONLY') as vault, patch('worker.emit'), \
                patch('providers.time.sleep'), patch('providers.random.uniform',return_value=0), \
                httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.generate',side_effect=lambda *a,**kw:original(*a,client=client,**kw)):
            with self.assertRaises(admission.AdmissionError) as caught:
                worker.translation(copy.deepcopy(self.p),{'id':self.root,'kind':'translate',
                    'project_revision':1,'api_usage_lineage':descriptor},self.jobs/self.root)
        self.assertEqual(caught.exception.report['blocked_code'],'execution_model_changed')
        self.assertEqual(len(calls),1)
        self.assertEqual(vault.call_count,1)
        self.assertEqual(lineage_status(descriptor,jobs_root=self.jobs)['lineage_request_attempts'],1)
        self.assertTrue((self.jobs/self.root/'translation-checkpoint.json').is_file())
