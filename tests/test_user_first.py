import copy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from core import ROOT, DATA, DEFAULTS, create_project, put, get, signature, save_srt
import server
from fixture_hardware import CPU_HARDWARE
from subtitle_quality import inspect_subtitles, coverage_record, review_signature, clean_ready, export_plan


class UserFirstTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('hardware_admission.hardware_info', return_value=CPU_HARDWARE))
        self.p = create_project(str(ROOT/'tests/fixtures/demo.mp4'),'User first tests')
        self.p['params'].update(use_clean=False,voice_gain=0,pipeline_tts=False,pipeline_translate=False)
        self.p['segments']=[{'start':0,'end':2,'text':'Xin chào','translated':'Xin chào'}]
        put('project',self.p['id'],self.p)
        self.client=TestClient(server.app,headers={'x-studio-token':server.TOKEN})

    def test_seven_cues_over_two_minutes_needs_review_not_guessed_speech(self):
        p=copy.deepcopy(self.p);p['media']['duration']=120
        p['segments']=[{'start':i*2,'end':i*2+1,'text':'a','translated':'a'} for i in range(7)]
        qa=inspect_subtitles(p)
        self.assertTrue(qa['requires_review']);self.assertFalse(qa['errors'])
        self.assertIn('trailing_gap',[i['code'] for i in qa['issues']])
        self.assertEqual(len(p['segments']),7)

    def test_review_is_bound_to_source_content_and_timing_not_font(self):
        p=self.p;p['subtitle_review']={'signature':review_signature(p),'reason':'Đã xem video.'}
        self.assertFalse(inspect_subtitles(p)['requires_review'])
        p['params']['font_size']=80
        self.assertFalse(inspect_subtitles(p)['requires_review'])
        p['segments'][0]['end']=3
        self.assertTrue(inspect_subtitles(p)['requires_review'])

    def test_full_scan_is_not_inferred_from_last_caption(self):
        p=self.p;p['media']['duration']=120
        p['segments'][0]['end']=119
        p['coverage']=coverage_record(p,'asr',0,14)
        self.assertTrue(inspect_subtitles(p)['requires_review'])
        p['coverage']=coverage_record(p,'asr',0,120)
        self.assertFalse(inspect_subtitles(p)['requires_review'])

    def test_review_requires_reason_and_current_signature(self):
        url=f'/api/studio/projects/{self.p["id"]}/review'
        self.assertEqual(self.client.post(url,json={'signature':'wrong','reason':'Đã xem'}).status_code,409)
        self.assertEqual(self.client.post(url,json={'signature':review_signature(self.p),'reason':'x'}).status_code,400)
        r=self.client.post(url,json={'signature':review_signature(self.p),'reason':'Tôi đã xem toàn bộ video.'})
        self.assertEqual(r.status_code,200)
        self.assertFalse(inspect_subtitles(r.json())['requires_review'])

    def test_empty_requested_subtitle_cannot_pass(self):
        p=self.p;p['segments']=[]
        from preflight import inspect
        self.assertFalse(inspect(p,'render')['ok'])
        p['params']['burn_subtitles']=False
        self.assertTrue(inspect(p,'render')['ok'])

    def test_completed_clean_survives_translation_or_voice_change(self):
        p=self.p;folder=DATA/'jobs'/uuid.uuid4().hex;folder.mkdir()
        import shutil
        from job_progress import verify_outputs
        shutil.copyfile(p['media']['path'], folder/'clean.mp4')
        artifact={'job_id':folder.name,'signature':signature(p),'files':['clean.mp4']}
        artifact['verification']=verify_outputs(artifact,p,{'kind':'clean'},folder)
        p['artifacts']['clean']=artifact
        self.assertTrue(clean_ready(p))
        p['segments'][0]['translated']='Lời mới';p['params']['voice_id']='changed'
        self.assertTrue(clean_ready(p))
        p['params']['method']='box'
        self.assertFalse(clean_ready(p))

    def test_summary_does_not_return_segments_or_credentials(self):
        response=self.client.get('/api/studio/state')
        self.assertEqual(response.status_code,200)
        item=next(p for p in response.json()['projects'] if p['id']==self.p['id'])
        self.assertNotIn('segments',item);self.assertNotIn('params',item)

    def test_project_revision_restore_preserves_current(self):
        old=self.p['updated'];new=server.update_project(self.p['id'],{'expected_revision':old,'name':'New name'})
        url=f'/api/studio/projects/{self.p["id"]}/restore'
        self.assertEqual(self.client.post(url,json={'expected_revision':old,'revision':old}).status_code,409)
        restored=self.client.post(url,json={'expected_revision':new['updated'],'revision':old})
        self.assertEqual(restored.status_code,200);self.assertEqual(restored.json()['name'],self.p['name'])

    def test_styled_ass_roundtrip(self):
        import pysubs2
        with tempfile.TemporaryDirectory(dir=DATA) as directory:
            path=Path(directory)/'styled.ass'
            p=DEFAULTS|{'subtitle_alignment':'top','font_color':'#F0A020','outline_color':'#112233','subtitle_background':True,'subtitle_margin_x':60}
            save_srt(self.p['segments'],path,ass_style=(p,self.p['media']))
            style=pysubs2.load(str(path)).styles['Default']
            self.assertEqual(int(style.alignment),8);self.assertEqual(style.borderstyle,3)
            self.assertEqual(style.primarycolor.r,240);self.assertEqual(style.marginl,60)

    def test_export_plan_does_not_reject_missing_translation_that_will_be_created(self):
        p=self.p;p['params'].update(pipeline_translate=True,voice_gain=1)
        p['segments'][0]['translated']=''
        self.assertFalse(export_plan(p)['subtitles']['errors'])

    def test_job_retry_idempotency_and_revision(self):
        self.p['params'].update(burn_subtitles=False)
        put('project',self.p['id'],self.p)
        body={'project_id':self.p['id'],'kind':'render','expected_revision':self.p['updated'],'request_id':uuid.uuid4().hex}
        with patch('preflight.inspect',return_value={'ok':True}):
            first=server.enqueue(body);second=server.enqueue(body)
        self.assertEqual(first['id'],second['id'])

    def test_bad_style_values_rejected(self):
        for params in ({'font_color':'red;evil'},{'subtitle_alignment':'nowhere'},{'voice_offset':float('nan')},{'export_name':'CON'}):
            with self.assertRaises(ValueError):server.validate_params(params)
