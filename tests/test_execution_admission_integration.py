"""Production API and queue boundaries using only fixture DATA and ASGI HTTP."""
import asyncio
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import httpx
import core
import server
import studio_api
from execution_admission import AdmissionError
from job_admission import project_lease


PROJECT = 'a' * 32
JOB = 'b' * 32


class ExecutionAdmissionIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ['VIETDUB_DATA_DIR'])
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        for module in (core, server, studio_api):
            self.start_patch(patch.object(module, 'DATA', self.data))
        self.start_patch(patch.dict(os.environ, {'VIETDUB_DATA_DIR':str(self.data)}))
        self.start_patch(patch('storage_manager._ensure_no_pending'))
        source = self.data/'source.mp4'
        source.write_bytes(b'fixture-source-identity-only')
        self.project = {'id':PROJECT, 'name':'Admission fixture', 'created':10, 'updated':20,
            'source_revision':'fixture-source', 'artifacts':{}, 'regions':[], 'extraction_regions':[],
            'segments':[{'start':0, 'end':5, 'text':'Hello', 'translated':'Xin chào'}],
            'media':{'path':str(source), 'duration':10, 'width':1280, 'height':720, 'fps':25, 'audio':True},
            'params':core.DEFAULTS | {'use_clean':False, 'pipeline_extract':'existing',
                'pipeline_translate':True, 'pipeline_tts':False, 'voice_gain':0, 'burn_subtitles':False}}
        self.row = {'id':JOB, 'project_id':PROJECT, 'project_name':self.project['name'],
            'kind':'pipeline', 'state':'error', 'created':time.time()-10000, 'started':time.time()-9999,
            'ended':time.time()-9900, 'source_revision':'fixture-source', 'project_revision':20,
            'configuration':copy.deepcopy(self.project['params'])}
        core.put('project', PROJECT, self.project)
        core.put('job', JOB, self.row)
        self.monitor = self.data/'npk-pipeline-monitor-state.json'
        self.monitor.write_text(json.dumps({'project_id':PROJECT, 'audit_hold':True,
                                           'quota_scope':'unknown'}), encoding='utf-8')
        self.folder = self.data/'jobs'/JOB
        self.folder.mkdir(parents=True)
        (self.folder/'translation-checkpoint.json').write_text('fixture checkpoint must remain untouched')

    def start_patch(self, patcher):
        value = patcher.start()
        self.addCleanup(patcher.stop)
        return value

    def request(self, method, path, body=None):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=server.app),
                    base_url='http://127.0.0.1', headers={'x-studio-token':server.TOKEN}) as client:
                return await client.request(method, path, json=body)
        return asyncio.run(send())

    def mock_content_plan(self):
        plan = {'job_id':JOB, 'project_id':PROJECT, 'expected_revision':20,
                'can_continue':True, 'completed_steps':['ocr'], 'remaining_steps':['translate','render'],
                'requires_review':False, 'errors':[], 'warnings':[], 'reason':'Content ready',
                'next_action':'continue_pipeline'}
        self.start_patch(patch('pipeline_resume.continuation_status', return_value=plan))
        self.start_patch(patch('pipeline_resume.prepare_continuation',
                              return_value=(copy.deepcopy(self.project), JOB, JOB, None)))
        self.start_patch(patch('provider_usage.prepare_lineage', return_value={'fixture':True}))
        self.start_patch(patch('provider_usage.lineage_status', return_value={'execution_allowed':True}))

    def test_create_job_hold_rejects_before_preflight_or_credentials(self):
        before = (self.folder/'translation-checkpoint.json').read_bytes()
        with patch('server.active_secret', side_effect=AssertionError('vault access')), \
             patch('keyring.get_password', side_effect=AssertionError('native vault access')), \
             patch('preflight.inspect', side_effect=AssertionError('preflight should not run')):
            with self.assertRaises(AdmissionError) as caught:
                server.create_job({'kind':'translate', 'project_id':PROJECT,
                                   'expected_revision':20, 'replace_segments':True})
        self.assertEqual(caught.exception.report['blocked_code'], 'audit_hold')
        self.assertEqual([row['id'] for row in core.all_items('job')], [JOB])
        self.assertEqual(core.all_items('admission_lease'), [])
        self.assertEqual((self.folder/'translation-checkpoint.json').read_bytes(), before)

    def test_http_enqueue_hold_has_no_provider_or_queue_side_effect(self):
        with patch('server.active_secret', side_effect=AssertionError('vault access')), \
             patch('preflight.inspect', side_effect=AssertionError('preflight should not run')):
            response = self.request('POST', '/api/jobs', {'kind':'pipeline', 'project_id':PROJECT,
                'expected_revision':20, 'replace_segments':True, 'request_id':'fixture-request'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('quota', response.json()['detail'])
        self.assertEqual(response.json()['execution_admission']['blocked_code'], 'audit_hold')
        self.assertEqual(len(core.all_items('job')), 1)
        self.assertEqual(core.all_items('job_request'), [])

    def test_spoken_optimization_cannot_bypass_provider_hold(self):
        with patch('server.active_secret', side_effect=AssertionError('vault access')), \
             patch('preflight.inspect', side_effect=AssertionError('preflight should not run')):
            with self.assertRaises(AdmissionError) as caught:
                server.create_job({'kind':'optimize_spoken', 'project_id':PROJECT,
                                   'expected_revision':20, 'replace_segments':True})
        self.assertEqual(caught.exception.report['blocked_code'], 'audit_hold')

    def test_continuation_get_separates_content_ready_from_execution_hold(self):
        self.mock_content_plan()
        with patch('server.active_secret', side_effect=AssertionError('vault access')):
            response = self.request('GET', f'/api/studio/jobs/{JOB}/continuation')
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result['content_ready'])
        self.assertFalse(result['execution_allowed'])
        self.assertFalse(result['can_continue'])
        self.assertEqual(result['blocked_code'], 'audit_hold')
        self.assertEqual(result['remaining_steps'], ['translate','render'])
        self.assertEqual(len(core.all_items('job')), 1)

    def test_post_continue_cannot_use_stale_owner_or_reach_planner(self):
        self.start_patch(patch.object(server, 'owner_record', {'instance_id':'current-owner'}))
        with patch('pipeline_resume.continuation_status', side_effect=AssertionError('planner reached')):
            response = self.request('POST', f'/api/studio/jobs/{JOB}/continue-pipeline',
                {'owner_instance_id':'stale-owner', 'expected_revision':20})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('sở hữu', response.json()['detail'])
        self.assertEqual(len(core.all_items('job')), 1)

    def test_post_continue_rechecks_hold_after_content_ready(self):
        self.mock_content_plan()
        self.start_patch(patch.object(server, 'owner_record', {'instance_id':'current-owner'}))
        with patch('server.active_secret', side_effect=AssertionError('vault access')):
            response = self.request('POST', f'/api/studio/jobs/{JOB}/continue-pipeline',
                {'owner_instance_id':'current-owner', 'expected_revision':20,
                 'request_id':'fixture-continue-request', 'automatic':True})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertIn('quota', response.json()['detail'])
        self.assertEqual(len(core.all_items('job')), 1)
        self.assertEqual(core.all_items('job_request'), [])

    def test_queued_provider_job_is_checked_before_process_or_vault(self):
        queued = self.row | {'state':'queued', 'stage':'queued', 'started':None, 'ended':None,
                            'created':time.time(), 'automatic':True}
        core.put('job', JOB, queued)
        spec = {'project':self.project, 'job':queued}
        (self.folder/'input.json').write_text(json.dumps(spec), encoding='utf-8')
        stop = MagicMock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        with patch.object(server, 'stop_event', stop), patch.object(server, 'owner_record', None), \
             patch('server._recover_abandoned_jobs'), \
             patch('server.subprocess.Popen', side_effect=AssertionError('process started')), \
             patch('server.active_secret', side_effect=AssertionError('vault access')), \
             patch('keyring.get_password', side_effect=AssertionError('native vault access')):
            server._worker_loop_run()
        stopped = core.get('job', JOB)
        self.assertEqual(stopped['state'], 'error')
        self.assertEqual(stopped['failure_stage'], 'admission')
        self.assertEqual(stopped['failure_code'], 'audit_hold')
        self.assertEqual(stopped['execution_admission']['blocked_code'], 'audit_hold')
        self.assertFalse((self.folder/'worker.log').exists())
        self.assertEqual((self.folder/'translation-checkpoint.json').read_text(), 'fixture checkpoint must remain untouched')

    def test_queued_snapshot_cannot_masquerade_as_unprotected_project(self):
        queued = self.row | {'state':'queued', 'stage':'queued', 'started':None, 'ended':None,
                            'created':time.time()}
        core.put('job', JOB, queued)
        tampered = copy.deepcopy(self.project)
        tampered['id'] = 'unprotected-project'
        (self.folder/'input.json').write_text(json.dumps({'project':tampered, 'job':queued}), encoding='utf-8')
        stop = MagicMock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        with patch.object(server, 'stop_event', stop), patch.object(server, 'owner_record', None), \
             patch('server._recover_abandoned_jobs'), \
             patch('server.subprocess.Popen', side_effect=AssertionError('process started')), \
             patch('server.active_secret', side_effect=AssertionError('vault access')):
            server._worker_loop_run()
        stopped = core.get('job', JOB)
        self.assertEqual(stopped['failure_code'], 'invalid_snapshot')
        self.assertFalse((self.folder/'worker.log').exists())

    def test_queued_snapshot_cannot_hide_provider_step_by_changing_params(self):
        queued = self.row | {'state':'queued', 'stage':'queued', 'started':None, 'ended':None,
                            'created':time.time()}
        core.put('job', JOB, queued)
        tampered = copy.deepcopy(self.project)
        tampered['params']['pipeline_translate'] = False
        (self.folder/'input.json').write_text(json.dumps({'project':tampered, 'job':queued}), encoding='utf-8')
        stop = MagicMock()
        stop.wait.side_effect = [False, True]
        stop.is_set.return_value = False
        with patch.object(server, 'stop_event', stop), patch.object(server, 'owner_record', None), \
             patch('server._recover_abandoned_jobs'), \
             patch('server.subprocess.Popen', side_effect=AssertionError('process started')), \
             patch('server.active_secret', side_effect=AssertionError('vault access')):
            server._worker_loop_run()
        self.assertEqual(core.get('job', JOB)['failure_code'], 'invalid_snapshot')
        self.assertFalse((self.folder/'worker.log').exists())

    def test_cross_connection_lease_collision_does_not_deadlock_store(self):
        with project_lease(self.data, PROJECT):
            # The lease transaction is already committed; independent reads
            # and writes continue, while another admission of this project fails.
            with closing(sqlite3.connect(self.data/'studio.sqlite3', timeout=.2)) as db, db:
                self.assertEqual(db.execute("select count(*) from objects where kind='admission_lease'").fetchone()[0], 1)
                db.execute('insert into objects values(?,?,?)', ('fixture', 'separate-connection', '{}'))
            with self.assertRaisesRegex(ValueError, 'yêu cầu khác'):
                with project_lease(self.data, PROJECT):
                    self.fail('duplicate lease acquired')
        self.assertEqual(core.all_items('admission_lease'), [])
        self.assertEqual(core.get('fixture', 'separate-connection'), {})

    def test_old_lease_release_never_deletes_replacement_owner(self):
        lease_id = hashlib.sha256(PROJECT.encode()).hexdigest()
        replacement = {'pid':os.getpid(), 'process_started':1, 'nonce':'replacement'}
        with project_lease(self.data, PROJECT):
            with closing(sqlite3.connect(self.data/'studio.sqlite3')) as db, db:
                db.execute("update objects set value=? where kind='admission_lease' and id=?",
                           (json.dumps(replacement), lease_id))
        self.assertEqual(core.get('admission_lease', lease_id), replacement)

    def test_dead_owner_lease_can_be_reclaimed_but_ambiguous_owner_cannot(self):
        lease_id = hashlib.sha256(PROJECT.encode()).hexdigest()
        core.put('admission_lease', lease_id, {'pid':2147483647, 'process_started':1, 'nonce':'dead'})
        with patch('job_admission._alive', return_value=False), project_lease(self.data, PROJECT):
            self.assertNotEqual(core.get('admission_lease', lease_id)['nonce'], 'dead')
        self.assertEqual(core.all_items('admission_lease'), [])
        core.put('admission_lease', lease_id, {'pid':1, 'process_started':1, 'nonce':'ambiguous'})
        with patch('job_admission._alive', return_value=True), self.assertRaises(ValueError):
            with project_lease(self.data, PROJECT):
                self.fail('ambiguous lease replaced')
        self.assertEqual(core.get('admission_lease', lease_id)['nonce'], 'ambiguous')

    def test_nonfinite_owner_start_is_ambiguous_and_never_reclaimed(self):
        lease_id = hashlib.sha256(PROJECT.encode()).hexdigest()
        owner = {'pid':os.getpid(), 'process_started':float('nan'), 'nonce':'ambiguous-start'}
        core.put('admission_lease', lease_id, owner)
        with self.assertRaises(ValueError):
            with project_lease(self.data, PROJECT):
                self.fail('nonfinite lease owner replaced')
        self.assertEqual(core.get('admission_lease', lease_id)['nonce'], 'ambiguous-start')


if __name__ == '__main__':
    unittest.main()
