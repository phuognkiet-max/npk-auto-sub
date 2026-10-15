from pathlib import Path
import unittest
from unittest.mock import patch
from core import ROOT, DATA, create_project, put
from server import enqueue
from voice_samples import sample_signature


class UsabilityTests(unittest.TestCase):
    def test_preview_requests_reuse_queued_work(self):
        body={'kind':'voice_preview','sample_text':'Mẫu chỉ để kiểm tra trùng lặp.',
              'params':{'voice_id':'piper:vais1000','voice_speed':1}}
        first=enqueue(body)
        self.assertEqual(enqueue(body)['id'],first['id'])
        first['state']='cancelled';put('job',first['id'],first)
        self.assertNotEqual(enqueue(body)['id'],first['id'])

    def test_preview_cache_requires_existing_file(self):
        body={'kind':'voice_preview','sample_text':'Mẫu kiểm tra cache.',
              'params':{'voice_id':'piper:vais1000','voice_speed':1}}
        first=enqueue(body);first['state']='done';put('job',first['id'],first)
        sample=DATA/'jobs'/first['id']/'sample.wav';sample.write_bytes(b'test placeholder')
        self.assertEqual(enqueue(body)['id'],first['id'])
        sample.unlink()
        self.assertNotEqual(enqueue(body)['id'],first['id'])

    def test_preview_cache_separates_text_and_speed(self):
        params={'voice_id':'piper:vais1000','voice_speed':1}
        self.assertNotEqual(sample_signature(params,'A'),sample_signature(params,'B'))
        self.assertNotEqual(sample_signature(params,'A'),sample_signature(params|{'voice_speed':1.2},'A'))

    def test_export_rejects_missing_requested_voice(self):
        from preflight import inspect
        project=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'No voice test')
        project['params'].update(use_clean=False,pipeline_tts=False,voice_gain=1)
        check=inspect(project,'render')
        self.assertFalse(check['ok'])
        self.assertTrue(any('chưa có giọng' in e for e in check['errors']))
        project['params']['voice_gain']=0
        # Explicitly request a source-only export: empty subtitles may no longer pass silently.
        project['params']['burn_subtitles']=False
        self.assertTrue(inspect(project,'render')['ok'])

    def test_import_revision_conflict_does_not_overwrite(self):
        from fastapi import HTTPException
        from core import get
        from server import import_subtitles, update_project
        project=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'Import revision test')
        update_project(project['id'],{'name':'Newer project'})
        before=get('project',project['id'])
        with self.assertRaises(HTTPException) as failure:
            import_subtitles(project['id'],{'text':'1\n00:00:00,000 --> 00:00:01,000\nHello\n',
                                           'expected_revision':project['updated']})
        self.assertEqual(failure.exception.status_code,409)
        self.assertEqual(get('project',project['id']),before)
