"""Durable admission across new job folders; no real credential/network access."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import patch
import uuid

import httpx
import providers
import worker
from core import DATA, DEFAULTS
from provider_usage import TranslationUsage, prepare_lineage, lineage_status, validate_lineage, read_summary, LINEAGE_NAME


class TranslationLineageBudgetTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))
        self.jobs = self.base/'jobs'
        self.jobs.mkdir()
        self.source = self.base/'source.mp4'
        self.source.write_bytes(b'private source identity fixture')
        self.p = {'id': uuid.uuid4().hex, 'source_revision': uuid.uuid4().hex,
                  'media': {'path': str(self.source), 'duration': 750},
                  'params': DEFAULTS | {'translation_scope':'missing', 'translation_batch':20},
                  'segments': [{'start': i*2., 'end': i*2.+1, 'text':'Source '+str(i), 'translated':''}
                               for i in range(40)], 'artifacts':{}, 'regions':[]}
        self.root_id = uuid.uuid4().hex
        self.root = self.folder(self.root_id)
        self.enterContext(patch('worker.emit'))
        self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))
        # Provider/ledger behavior is isolated from the independent policy gate.
        self.enterContext(patch('worker.require_execution_admission'))

    def folder(self, jid=None):
        path = self.jobs/(jid or uuid.uuid4().hex)
        path.mkdir()
        return path

    def descriptor(self, p=None, history=()):
        return prepare_lineage(p or self.p, 'pipeline', self.root_id, history, jobs_root=self.jobs)

    def row(self, folder, *, stage='1/4:translate'):
        return {'id':folder.name, 'kind':'pipeline', 'state':'error', 'created':1.,
                'project_id':self.p['id'], 'source_revision':self.p['source_revision'],
                'continuation_from': None if folder==self.root else self.root_id,
                'configuration':{'pipeline_translate':True}, 'stage':stage}

    def usage(self, folder, descriptor=None, pending=40, batch=20):
        return TranslationUsage(folder, pending, batch, self.p['params']['provider'],
                                self.p['params']['translation_model'], lineage=descriptor or self.descriptor(),
                                job_id=folder.name, jobs_root=self.jobs)

    def send(self, usage):
        usage.set_batch([0], 'SOURCE AND SECRET MUST NOT ENTER THE LEDGER')
        usage.observe({'event':'before_send', 'attempt':1})

    def translate(self, p, folder, descriptor, handler, job_fields=None):
        real_generate = providers.generate
        job = {'id':folder.name, 'kind':'pipeline', 'api_usage_lineage':descriptor}
        if job_fields:
            job.update({k:v for k,v in job_fields.items() if k != 'credential_ref'})
        with patch('credentials.active_secret', return_value='TEST-ONLY'), \
                httpx.Client(transport=httpx.MockTransport(handler)) as client, \
                patch('providers.generate', side_effect=lambda *a, **kw: real_generate(*a, client=client, **kw)):
            return worker.translation(p, job, folder)

    def standalone_api_fixture(self):
        import core
        import server
        import studio_api
        from fastapi.testclient import TestClient
        for module in (core, server, studio_api): self.enterContext(patch.object(module, 'DATA', self.base))
        self.enterContext(patch('preflight.inspect', return_value={'ok':True, 'errors':[], 'warnings':[]}))
        vault = self.enterContext(patch.object(server, 'active_secret', return_value='TEST-ONLY'))
        self.enterContext(patch.object(server, 'active_key_id', return_value='fixture'))
        p = copy.deepcopy(self.p)
        p.update(name='Private standalone translation', created=1., updated=20., extraction_regions=[])
        p['media'].update(width=1280, height=720, audio=False, fps=30.)
        p['params'].update(pipeline_extract='existing', pipeline_translate=True, subtitle_automation='manual')
        row = self.row(self.root)
        row.update(kind='translate', project_name=p['name'], project_revision=p['updated'], stage='translate')
        (self.root/'input.json').write_text(json.dumps({'project':p, 'job':row}), encoding='utf-8')
        core.put('project', p['id'], p); core.put('job', row['id'], row)
        client = self.enterContext(TestClient(server.app, headers={'x-studio-token':server.TOKEN}))
        return p, row, client, vault

    @staticmethod
    def requested_rows(request):
        prompt = json.loads(request.content)['contents'][0]['parts'][0]['text']
        return json.loads(prompt.split('Input:\n')[1])

    @staticmethod
    def success(rows):
        output = {'lines':[{'id':r['id'], 'text':'Dịch đủ '+str(r['id'])} for r in rows]}
        return httpx.Response(200, json={'candidates':[{'content':{'parts':[{'text':json.dumps(output)}]}}],
                             'usageMetadata':{'promptTokenCount':10, 'candidatesTokenCount':3, 'totalTokenCount':13}})

    def test_empty_recognition_defers_allowance_until_375_rows_are_known(self):
        empty = copy.deepcopy(self.p)
        empty['segments'] = []
        empty['params']['pipeline_extract'] = 'ocr'
        descriptor = self.descriptor(empty)
        self.assertIsNone(descriptor['request_limit'])
        before = lineage_status(descriptor, jobs_root=self.jobs)
        self.assertTrue(before['execution_allowed'])
        self.assertIsNone(before['lineage_request_limit'])
        self.assertFalse((self.root/LINEAGE_NAME).exists(), 'Read-only preparation must not create state.')
        usage = self.usage(self.root, descriptor, pending=375)
        self.assertEqual((usage.baseline, usage.limit), (19, 24))
        self.assertEqual(lineage_status(descriptor, jobs_root=self.jobs)['request_limit'], 24)

    def test_budget_cannot_reset_across_distinct_successor_folders(self):
        self.p['segments'] = self.p['segments'][:1]
        descriptor = self.descriptor()
        usage = self.usage(self.root, descriptor, pending=1)
        for _ in range(4): self.send(usage)
        successor = self.folder()
        inherited = self.descriptor(history=[self.row(self.root)])
        next_usage = self.usage(successor, inherited, pending=1)
        self.assertEqual(next_usage.summary()['request_attempts'], 0)
        self.assertEqual(next_usage.summary()['lineage_request_attempts'], 4)
        self.assertEqual(next_usage.limit, 5)
        self.send(next_usage)
        status = lineage_status(inherited, jobs_root=self.jobs)
        self.assertEqual((status['lineage_request_attempts'], status['remaining_requests']), (5, 0))
        self.assertFalse(status['execution_allowed'])
        with self.assertRaises(providers.ProviderError) as caught:
            self.usage(self.folder(), inherited, pending=1)
        self.assertEqual(caught.exception.status, 'budget')
        self.assertEqual(lineage_status(inherited, jobs_root=self.jobs)['lineage_request_attempts'], 5)

    def test_continuation_reuses_five_cues_and_adds_sends_to_the_same_budget(self):
        self.p['segments'] = self.p['segments'][:25]
        for i in range(5): self.p['segments'][i]['translated'] = 'Existing '+str(i)
        first = copy.deepcopy(self.p)
        descriptor = self.descriptor(first)
        first_ids = []
        def unavailable(request):
            first_ids.append([r['id'] for r in self.requested_rows(request)])
            return httpx.Response(503)
        with self.assertRaises(providers.ProviderError):
            self.translate(first, self.root, descriptor, unavailable)
        self.assertEqual(first_ids, [list(range(5,25)), list(range(5,25))])
        measured = read_summary(self.root)
        self.assertEqual((measured['reused_cues'], measured['newly_completed_cues'], measured['remaining_cues']), (5,0,20))
        child = self.folder()
        child_descriptor = self.descriptor(history=[self.row(self.root)])
        second_ids = []
        def available(request):
            rows = self.requested_rows(request); second_ids.append([r['id'] for r in rows]); return self.success(rows)
        result = self.translate(copy.deepcopy(self.p), child, child_descriptor, available)
        measured = result['api_usage']
        self.assertEqual(second_ids, [list(range(5,25))])
        self.assertEqual((measured['request_attempts'], measured['lineage_request_attempts']), (1,3))
        self.assertEqual((measured['reused_cues'], measured['newly_completed_cues'], measured['remaining_cues']), (5,20,0))
        self.assertEqual([r['translated'] for r in result['segments'][:5]], [r['translated'] for r in self.p['segments'][:5]])

    def test_legacy_known_ledger_is_imported_once_and_keeps_original_allowance(self):
        old = TranslationUsage(self.root, 40, 20, self.p['params']['provider'], self.p['params']['translation_model'])
        self.send(old); self.send(old)
        history = [self.row(self.root)]
        descriptor = self.descriptor(history=history)
        child = self.folder()
        usage = self.usage(child, descriptor, pending=1)
        self.assertEqual((usage.limit, usage.summary()['lineage_request_attempts']), (6,2))
        self.send(usage)
        again = self.descriptor(history=history + [self.row(child)])
        next_usage = self.usage(self.folder(), again, pending=1)
        self.assertEqual(next_usage.summary()['lineage_request_attempts'], 3, 'Old ledgers must not double count on replay.')
        self.assertEqual(next_usage.limit, 6)

    def test_missing_translation_history_blocks_before_credentials_or_transport(self):
        history = [self.row(self.root)]
        descriptor = self.descriptor(history=history)
        self.assertEqual(descriptor['history_status'], 'unknown')
        status = lineage_status(descriptor, jobs_root=self.jobs)
        self.assertTrue(status['history_unknown'])
        self.assertEqual(status['blocked_code'], 'history_unknown')
        child = self.folder()
        with patch('credentials.active_secret') as credentials, patch('providers.generate') as generate:
            with self.assertRaises(providers.ProviderError):
                worker.translation(copy.deepcopy(self.p), {'id':child.name, 'kind':'pipeline',
                    'api_usage_lineage':descriptor}, child)
            credentials.assert_not_called(); generate.assert_not_called()

    def test_ocr_director_failure_before_translation_is_not_unknown_translation_usage(self):
        descriptor = self.descriptor(history=[self.row(self.root, stage='1/5:ocr')])
        self.assertEqual(descriptor['history_status'], 'known')
        self.assertEqual(descriptor['unknown_job_ids'], [])

    def test_queued_or_cancelled_before_start_is_not_unknown_but_observed_ledger_still_counts(self):
        queued = self.row(self.root, stage='queued'); queued['state'] = 'queued'
        self.assertEqual(self.descriptor(history=[queued])['history_status'], 'known')
        queued['state'] = 'cancelled'
        self.assertEqual(self.descriptor(history=[queued])['history_status'], 'known')
        usage = TranslationUsage(self.root, 40, 20, self.p['params']['provider'], self.p['params']['translation_model'])
        self.send(usage)
        measured = self.descriptor(history=[queued])
        self.assertEqual(lineage_status(measured, jobs_root=self.jobs)['lineage_request_attempts'], 1)

    def test_standalone_retry_new_uuid_reuses_root_budget_and_exhaustion_blocks_resume_before_key(self):
        import core
        self.p['segments'] = self.p['segments'][:2]
        p, row, client, vault = self.standalone_api_fixture()
        old = TranslationUsage(self.root, 40, 20, p['params']['provider'], p['params']['translation_model'])
        for _ in range(4): self.send(old)
        response = client.post('/api/jobs', json={'project_id':p['id'], 'kind':'translate',
            'retry_from':row['id'], 'replace_segments':True, 'expected_revision':p['updated']})
        self.assertEqual(response.status_code, 200, response.text)
        child = response.json(); self.assertNotEqual(child['id'], row['id'])
        self.assertEqual(child['api_usage_lineage']['root_id'], row['id'])
        self.assertEqual(child['api_usage_lineage']['request_limit'], 6)
        folder = self.jobs/child['id']
        packet = json.loads((folder/'input.json').read_text(encoding='utf-8'))
        calls = []
        with self.assertRaises(providers.ProviderError):
            self.translate(packet['project'], folder, packet['job']['api_usage_lineage'],
                           lambda request:calls.append(request) or httpx.Response(503), packet['job'])
        self.assertEqual(len(calls), 2)
        child.update(state='error', stage='translate')
        core.put('job', child['id'], child)
        vault.reset_mock()
        denied = client.post('/api/jobs', json={'project_id':p['id'], 'kind':'translate',
            'resume_from':child['id'], 'replace_segments':True, 'expected_revision':p['updated']})
        self.assertEqual(denied.status_code, 409, denied.text)
        vault.assert_not_called()
        self.assertEqual(len(core.all_items('job')), 2)
        self.assertEqual(lineage_status(child['api_usage_lineage'], jobs_root=self.jobs)['lineage_request_attempts'], 6)

    def test_standalone_resume_new_uuid_keeps_allowance_and_restored_cue(self):
        from translation_checkpoint import write_checkpoint
        self.p['segments'] = self.p['segments'][:2]
        p, row, client, vault = self.standalone_api_fixture()
        restored = copy.deepcopy(p['segments']); restored[0]['translated'] = 'Existing first cue'
        write_checkpoint(self.root/'translation-checkpoint.json', p, restored, {0}, None, [])
        old = TranslationUsage(self.root, 40, 20, p['params']['provider'], p['params']['translation_model'])
        self.send(old); self.send(old)
        response = client.post('/api/jobs', json={'project_id':p['id'], 'kind':'translate',
            'resume_from':row['id'], 'replace_segments':True, 'expected_revision':p['updated']})
        self.assertEqual(response.status_code, 200, response.text)
        child = response.json(); self.assertNotEqual(child['id'], row['id'])
        self.assertEqual(child['api_usage_lineage']['root_id'], row['id'])
        self.assertEqual(child['api_usage_lineage']['request_limit'], 6)
        folder = self.jobs/child['id']
        packet = json.loads((folder/'input.json').read_text(encoding='utf-8'))
        calls = []
        def handler(request):
            rows = self.requested_rows(request); calls.append([r['id'] for r in rows]); return self.success(rows)
        result = self.translate(packet['project'], folder, packet['job']['api_usage_lineage'], handler, packet['job'])
        self.assertEqual(calls, [[1]])
        self.assertEqual(result['segments'][0]['translated'], 'Existing first cue')
        self.assertEqual(result['api_usage']['lineage_request_attempts'], 3)
        self.assertEqual((result['api_usage']['reused_cues'], result['api_usage']['newly_completed_cues']), (1,1))

    def test_source_or_budget_tampering_fails_closed(self):
        descriptor = self.descriptor()
        self.usage(self.root, descriptor)
        tampered = dict(descriptor, request_limit=descriptor['request_limit']+1)
        with self.assertRaises(ValueError): self.usage(self.folder(), tampered)
        changed = copy.deepcopy(self.p)
        changed['params']['translation_model'] = 'different-model'
        with self.assertRaises(ValueError): validate_lineage(descriptor, changed)
        self.source.write_bytes(b'changed source identity')
        with self.assertRaises(ValueError): self.descriptor()

    def test_invalid_historical_ledger_is_not_treated_as_zero(self):
        old = TranslationUsage(self.root, 40, 20, self.p['params']['provider'], self.p['params']['translation_model'])
        self.send(old)
        document = json.loads((self.root/'api-usage.json').read_text(encoding='utf-8'))
        document['requests'][0]['prompt'] = 'UNSAFE VALUE'
        (self.root/'api-usage.json').write_text(json.dumps(document), encoding='utf-8')
        with self.assertRaises(ValueError): self.descriptor(history=[self.row(self.root)])

    def test_committed_send_survives_json_persist_crash(self):
        descriptor = self.descriptor()
        usage = self.usage(self.root, descriptor)
        usage.set_batch([0], 'DO NOT LOG')
        with patch.object(usage, 'persist', side_effect=OSError('simulated local JSON write failure')):
            with self.assertRaises(OSError): usage.observe({'event':'before_send', 'attempt':1})
        recovered = self.usage(self.root, descriptor)
        self.assertEqual(recovered.summary()['request_attempts'], 1)
        self.assertEqual(recovered.summary()['lineage_request_attempts'], 1)
        self.assertNotIn('DO NOT LOG', (self.root/'api-usage.json').read_text(encoding='utf-8'))

    def test_simultaneous_successors_cannot_overreserve_shared_allowance(self):
        self.p['segments'] = self.p['segments'][:1]
        descriptor = self.descriptor()
        folders = [self.root] + [self.folder() for _ in range(15)]
        def reserve(folder):
            try:
                self.send(self.usage(folder, descriptor, pending=1))
                return True
            except providers.ProviderError as error:
                self.assertEqual(error.status, 'budget')
                return False
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, folders))
        self.assertEqual(sum(results), 5)
        self.assertEqual(lineage_status(descriptor, jobs_root=self.jobs)['lineage_request_attempts'], 5)

    def test_same_job_competing_instances_cannot_send_the_same_local_ordinal(self):
        descriptor = self.descriptor()
        first = self.usage(self.root, descriptor)
        second = self.usage(self.root, descriptor)
        self.send(first)
        with self.assertRaises(ValueError): self.send(second)
        self.assertEqual(lineage_status(descriptor, jobs_root=self.jobs)['lineage_request_attempts'], 1)

    def test_deleted_shared_request_record_is_detected_instead_of_restoring_allowance(self):
        descriptor = self.descriptor()
        usage = self.usage(self.root, descriptor)
        self.send(usage)
        db = sqlite3.connect(self.root/LINEAGE_NAME)
        try:
            db.execute('DELETE FROM requests'); db.commit()
        finally: db.close()
        with self.assertRaises(ValueError): lineage_status(descriptor, jobs_root=self.jobs)
        with self.assertRaises(ValueError): self.usage(self.folder(), descriptor)

    def test_committed_registry_preserves_known_history_when_json_ledger_is_missing(self):
        descriptor = self.descriptor()
        usage = self.usage(self.root, descriptor)
        self.send(usage)
        (self.root/'api-usage.json').unlink()
        inherited = self.descriptor(history=[self.row(self.root)])
        self.assertEqual(inherited['history_status'], 'known')
        successor = self.usage(self.folder(), inherited)
        self.assertEqual(successor.summary()['lineage_request_attempts'], 1)

    def test_real_legacy_packet_without_lineage_cannot_read_credentials(self):
        child = self.folder()
        with patch('credentials.active_secret') as credentials, patch('providers.generate') as generate:
            with self.assertRaises(providers.ProviderError):
                worker.translation(copy.deepcopy(self.p), {'id':child.name, 'kind':'pipeline'}, child)
            credentials.assert_not_called(); generate.assert_not_called()

    def test_real_continuation_endpoint_attaches_shared_budget_and_preserves_inherited_five(self):
        from fastapi.testclient import TestClient
        import core
        import server
        import studio_api
        from subtitle_quality import coverage_record
        from translation_checkpoint import write_checkpoint
        for module in (core, server, studio_api): self.enterContext(patch.object(module, 'DATA', self.base))
        self.enterContext(patch('preflight.inspect', return_value={'ok':True, 'errors':[], 'warnings':[]}))
        self.enterContext(patch('subtitle_quality.inspect_subtitles', return_value={
            'requires_review':False, 'errors':[], 'issues':[]}))
        self.enterContext(patch.object(server, 'active_secret', return_value='TEST-ONLY'))
        self.enterContext(patch.object(server, 'active_key_id', return_value='fixture'))
        p = copy.deepcopy(self.p)
        p.update(name='Private full pipeline', created=1., updated=20., extraction_regions=[])
        p['media'].update(width=1280, height=720, audio=False, fps=30.)
        p['params'].update(pipeline_extract='existing', pipeline_translate=True, pipeline_tts=False,
                           pipeline_clean=False, use_clean=False, subtitle_automation='manual', voice_gain=0)
        p['segments'] = p['segments'][:25]
        p['coverage'] = coverage_record(p, 'ocr', 0, p['media']['duration'])
        row = self.row(self.root)
        row.update(project_revision=p['updated'], project_name=p['name'],
                   configuration={'pipeline_extract':'existing', 'pipeline_translate':True})
        (self.root/'input.json').write_text(json.dumps({'project':p, 'job':row}), encoding='utf-8')
        rows = copy.deepcopy(p['segments'])
        for i in range(5): rows[i]['translated'] = 'Existing '+str(i)
        write_checkpoint(self.root/'translation-checkpoint.json', p, rows, {0,1,2,3,4}, None, [])
        old = TranslationUsage(self.root, 25, 20, p['params']['provider'], p['params']['translation_model'])
        self.send(old)
        core.put('project', p['id'], p); core.put('job', row['id'], row)
        with TestClient(server.app, headers={'x-studio-token':server.TOKEN}) as client:
            response = client.post(f'/api/studio/jobs/{row["id"]}/continue-pipeline',
                                   json={'expected_revision':p['updated']})
            self.assertEqual(response.status_code, 200, response.text)
            child = response.json()
            duplicate = client.post(f'/api/studio/jobs/{row["id"]}/continue-pipeline',
                                    json={'expected_revision':p['updated']}).json()
            self.assertEqual(child['id'], duplicate['id'])
        folder = self.jobs/child['id']
        packet = json.loads((folder/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(packet['job']['api_usage_lineage']['root_id'], self.root_id)
        self.assertEqual(packet['job']['api_usage_lineage']['request_limit'], 6)
        self.assertEqual(sum(bool(r['translated']) for r in packet['project']['segments']), 5)
        self.assertEqual(packet['project']['params']['pipeline_extract'], 'existing')
        requested = []
        def available(request):
            rows = self.requested_rows(request); requested.append([r['id'] for r in rows]); return self.success(rows)
        result = self.translate(packet['project'], folder, packet['job']['api_usage_lineage'], available)
        self.assertEqual(requested, [list(range(5,25))])
        self.assertEqual(result['api_usage']['lineage_request_attempts'], 2)
        self.assertEqual((result['api_usage']['reused_cues'], result['api_usage']['newly_completed_cues']), (5,20))
        self.assertEqual(core.get('project', p['id'])['segments'], p['segments'], 'Endpoint/worker must not rewrite the editor.')
