import unittest
import time
from unittest.mock import patch
from fastapi.testclient import TestClient
from core import ROOT, create_project, put
import server
from fixture_hardware import CPU_HARDWARE


class RecognitionStateTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('hardware_admission.hardware_info', return_value=CPU_HARDWARE))
        self.p = create_project(str(ROOT/'tests/fixtures/demo.mp4'), 'Recognition UI state')
        self.p['source_revision'] = 'new-source'
        put('project', self.p['id'], self.p)
        self.client = TestClient(server.app, headers={'x-studio-token':server.TOKEN})

    def job(self, suffix, kind='asr', state='done', created=1, **extra):
        row = dict(id=self.p['id']+suffix, project_id=self.p['id'], source_revision='new-source',
                   kind=kind, state=state, created=created, progress=1, **extra)
        return put('job', row['id'], row)

    def state(self):
        return self.client.get('/api/studio/state',params={'project_id':self.p['id']}).json()

    def test_last_method_survives_global_history_limit(self):
        job = self.job('asr')
        for i in range(65):
            self.job(str(i),kind='voice_preview',created=2+i)
        result = self.state()
        self.assertNotIn(job['id'], [row['id'] for row in result['jobs']])
        self.assertEqual(result['recognition']['id'], job['id'])
        self.assertEqual(result['recognition']['extraction_method'], 'asr')
        self.assertNotIn('configuration', result['recognition'])

    def test_current_source_only_and_running_precedes_queued(self):
        old = self.job('old', kind='ocr', created=100)
        old['source_revision'] = None; put('job', old['id'], old)
        self.assertIsNone(self.state()['recognition'])
        self.job('queued', state='queued', created=3)
        active = self.job('running', state='running', created=2)
        self.assertEqual(self.state()['recognition']['id'], active['id'])

    def test_pipeline_method_and_terminal_states_are_explicit(self):
        for i, state in enumerate(('done','error','cancelled','review')):
            self.job(str(i),kind='pipeline',state=state,created=i,
                     configuration={'pipeline_extract':'ocr', 'private_field':'must-not-leak'})
            result = self.state()['recognition']
            self.assertEqual(result['state'], state)
            self.assertEqual(result['extraction_method'], 'ocr')
            self.assertNotIn('private_field', str(result))

    def test_pipeline_failure_keeps_verified_recognition_separate_from_remote_failure(self):
        job=self.job('semantic',kind='pipeline',state='error',created=time.time(),configuration={'pipeline_extract':'ocr'},
            stage_journal={'completed_steps':['ocr']},failure_stage='auto_subtitles',
            failure_code='provider_service',http_status=503,next_action='continue_auto_subtitles')
        state=self.state()
        for row in (state['recognition'],next(row for row in state['jobs'] if row['id']==job['id'])):
            self.assertEqual(row['stage_journal']['completed_steps'],['ocr'])
            self.assertEqual(row['failure_stage'],'auto_subtitles')
            self.assertEqual(row['http_status'],503)
            self.assertEqual(row['next_action'],'continue_auto_subtitles')

    def test_duplicate_recognition_rejected_but_completed_can_retry(self):
        row = self.job('busy', state='running')
        for kind in ('asr','ocr'):
            response = self.client.post('/api/jobs',json={'project_id':self.p['id'], 'kind':kind})
            self.assertEqual(response.status_code,409)
        row['state']='error';put('job',row['id'],row)
        with patch('preflight.inspect',return_value={'ok':True}):
            response=self.client.post('/api/jobs',json={'project_id':self.p['id'],'kind':'asr'})
        self.assertEqual(response.status_code,200,response.text)
