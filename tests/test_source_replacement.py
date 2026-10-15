import copy
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from core import ROOT, create_project, get, put
import server


class SourceReplacementTests(unittest.TestCase):
    def setUp(self):
        self.p = create_project(str(ROOT/'tests/fixtures/demo.mp4'), 'Giữ tên dự án')
        self.p['segments'] = [dict(start=0, end=2, text='Old', translated='Cũ')]
        self.p['regions'] = self.p['extraction_regions'] = [dict(x=0, y=0, w=30, h=20, start=0, end=8)]
        self.p['artifacts'] = {'render': {'job_id':'old'}, 'tts': {'job_id':'old'}}
        self.p['coverage'] = {'old':True}
        self.p['subtitle_review'] = {'old':True}
        self.p['params'].update(voice_path='old.wav', bgm_path='music.wav', extraction_start=5, extraction_end=8)
        put('project', self.p['id'], self.p)
        self.client = TestClient(server.app, headers={'x-studio-token':server.TOKEN})
        self.url = '/api/studio/projects/' + self.p['id'] + '/replace-source'

    def replace(self, **options):
        return self.client.post(self.url, json=dict(path=self.p['media']['path'],
            expected_revision=self.p['updated'], **options))

    def test_new_source_clears_stale_content_not_settings_or_history(self):
        media = {**self.p['media'], 'path':'D:\\test-only-new.mp4', 'duration':30, 'width':1080, 'height':1920}
        with patch('studio_api.probe', return_value=media):
            response = self.replace(mode='reset')
        self.assertEqual(response.status_code, 200, response.text)
        p = response.json()
        self.assertEqual(p['media'], media)
        for field in ('id','name','created'):
            self.assertEqual(p[field], self.p[field])
        for field in ('segments','regions','extraction_regions'):
            self.assertEqual(p[field], [])
        self.assertEqual(p['artifacts'], {})
        self.assertNotIn('coverage', p)
        self.assertNotIn('subtitle_review', p)
        self.assertTrue(p['source_revision'])
        for field in ('font','font_size','voice_id','provider','workload_mode'):
            self.assertEqual(p['params'][field], self.p['params'][field])
        for field in ('voice_path','bgm_path'):
            self.assertEqual(p['params'][field], '')
        self.assertEqual(p['params']['extraction_start'], 0)
        self.assertFalse(p['params']['use_clean'])
        self.assertEqual(get('project_backup', p['id']), self.p)
        restored = self.client.post('/api/studio/projects/' + p['id'] + '/restore', json={
            'expected_revision':p['updated'], 'revision':self.p['updated']})
        self.assertEqual(restored.status_code, 200)
        self.assertEqual(restored.json()['media'], self.p['media'])
        self.assertEqual(restored.json()['segments'], self.p['segments'])

    def test_keep_preserves_edits_but_invalidates_processed_results(self):
        response = self.replace(mode='keep')
        self.assertEqual(response.status_code, 200, response.text)
        p = response.json()
        for field in ('segments','regions','extraction_regions','params'):
            self.assertEqual(p[field], self.p[field])
        self.assertEqual(p['artifacts'], {})
        self.assertNotIn('subtitle_review', p)

    def test_keep_rejects_incompatible_media_without_changes(self):
        for delta in ({'duration':30}, {'width':1080,'height':1920}):
            with patch('studio_api.probe', return_value={**self.p['media'], **delta}):
                self.assertEqual(self.replace(mode='keep').status_code, 400)
            self.assertEqual(get('project',self.p['id']), self.p)

    def test_stale_revision_and_invalid_mode_rejected(self):
        p = copy.deepcopy(self.p); p['updated'] += 1; put('project', p['id'], p)
        self.assertEqual(self.replace().status_code, 409)
        self.assertEqual(self.replace(mode='unknown').status_code, 400)
        self.assertEqual(get('project',p['id']), p)

    def test_missing_file_and_auth_fail_without_changes(self):
        response = self.client.post(self.url, json={'path':str(ROOT/'tests/no-such-video.mp4'),
            'expected_revision':self.p['updated']})
        self.assertGreaterEqual(response.status_code, 400)
        response = self.client.post(self.url, headers={'x-studio-token':'wrong'}, json={})
        self.assertIn(response.status_code, (401,403))
        self.assertEqual(get('project',self.p['id']), self.p)

    def test_running_or_queued_jobs_block_source_change(self):
        for state in ('running', 'queued'):
            put('job', 'active-source-test', dict(id='active-source-test', project_id=self.p['id'], state=state))
            self.assertEqual(self.replace().status_code, 409)
            self.assertEqual(get('project',self.p['id']), self.p)
        put('job', 'active-source-test', dict(id='active-source-test', project_id=self.p['id'], state='done'))

    def test_old_results_cannot_be_applied_to_replacement(self):
        put('job', 'old-source-test', dict(id='old-source-test', project_id=self.p['id'], state='done', kind='ocr',
            result={'segments':self.p['segments']}))
        p = self.replace().json()
        result = self.client.post('/api/studio/jobs/old-source-test/apply', json={'expected_revision':p['updated']})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(get('project',p['id'])['segments'], [])

    def test_new_jobs_record_source_version(self):
        p = self.replace().json()
        with patch('preflight.inspect', return_value={'ok':True}):
            job = server.enqueue(dict(project_id=p['id'], kind='ocr', expected_revision=p['updated']))
        self.assertEqual(job['source_revision'], p['source_revision'])
