import copy
import os
import subprocess
import sys
import unittest
from unittest.mock import patch, Mock
import cv2
import numpy as np
from fastapi import HTTPException
from fastapi.testclient import TestClient
from core import ROOT, DATA, DEFAULTS, create_project, all_items, get
import server
from live_clean_preview import snapshot, render_frame, render_in_child, gate, KEYS


class LivePreviewTests(unittest.TestCase):
    def setUp(self):
        self.p = create_project(str(ROOT/'tests/fixtures/demo.mp4'), 'Live preview test')
        self.regions = [dict(x=100,y=265,w=420,h=75,start=0,end=8)]
        self.body = dict(source_revision=self.p.get('source_revision'),time=0,regions=self.regions,
                         params=dict(method='telea',cover_opacity=1,edge_feather=0,inpaint_radius=2,mask_mode='rectangle'))
        self.client = TestClient(server.app)
        self.headers = {'x-studio-token':server.TOKEN}
        self.url = '/api/studio/projects/'+self.p['id']+'/live-clean-frame'

    def spec(self, body=None):
        return snapshot(self.p,body or self.body,server.validate_params,server.validate_regions,server.number)

    def test_transient_overrides_do_not_write_project_or_jobs(self):
        before = copy.deepcopy(get('project',self.p['id']))
        jobs = all_items('job')
        with patch('live_clean_preview.render_frame', return_value=b'\xff\xd8test\xff\xd9') as render:
            response = self.client.post(self.url,headers=self.headers,json=self.body)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.headers['content-type'],'image/jpeg')
        self.assertEqual(response.headers['cache-control'],'no-store')
        self.assertEqual(render.call_args.args[0]['regions'], self.regions)
        self.assertEqual(get('project',self.p['id']),before)
        self.assertEqual(all_items('job'),jobs)

    def test_source_paths_auth_and_ranges_are_guarded(self):
        self.assertEqual(self.client.post(self.url,json=self.body).status_code,403)
        for override in [dict(source_revision='old-source'),dict(media={'path':'C:/foreign.mp4'}),
                         dict(params={'method':'telea','voice_path':'C:/foreign.wav'}),
                         dict(time=9),dict(time=float('nan')),dict(regions=[]),
                         dict(regions=[{}]),dict(regions=self.regions*101),
                         dict(params={'method':'telea','edge_feather':500}),
                         dict(params={'method':'telea','mask_source':'tracked'})]:
            with self.subTest(override=override), self.assertRaises((ValueError,HTTPException)):
                self.spec(self.body | override)
        spec = self.spec()
        self.assertEqual(set(spec),{'media','params','regions','time'})
        self.assertEqual(set(spec['params']),KEYS)

    def test_busy_worker_and_frame_gate_reject_without_queueing(self):
        with patch.object(server,'active_process') as active, patch('live_clean_preview.render_frame') as render:
            active.poll.return_value = None
            response = self.client.post(self.url,headers=self.headers,json=self.body)
            self.assertEqual(response.status_code,409)
            render.assert_not_called()
        gate.acquire()
        try:
            with self.assertRaises(HTTPException) as result: render_frame(self.spec())
            self.assertEqual(result.exception.status_code,429)
        finally: gate.release()

    def test_actual_child_returns_effective_jpeg_and_releases_gate(self):
        from worker import read_frame
        before = read_frame(self.p['media']['path'],0)
        # Exercise the production parent and rendering function in a real
        # child, explicitly constrained to CPU Telea / owned fixture media.
        original_popen = subprocess.Popen
        def fixture_child(command, **options):
            self.assertEqual(command[1:], ['-X', 'utf8', str(ROOT/'live_clean_preview.py'), '--frame'])
            options['env'] = dict(options.get('env') or os.environ,
                                  NPK_TEST_PROFILE='media', VIETDUB_DATA_DIR=str(DATA))
            return original_popen([sys.executable, '-X', 'utf8', str(ROOT/'tools/media_fixture_child.py')], **options)
        with patch('live_clean_preview.subprocess.Popen', side_effect=fixture_child):
            jpeg = render_frame(self.spec())
        after = cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
        self.assertEqual(after.shape,before.shape)
        self.assertGreater(cv2.absdiff(after[270:338,110:510],before[270:338,110:510]).mean(),10)
        self.assertFalse(gate.locked())
        spec = self.spec(); spec['params']['cover_opacity'] = 0
        zero = cv2.imdecode(np.frombuffer(render_in_child(spec),np.uint8),cv2.IMREAD_COLOR)
        self.assertLess(cv2.absdiff(zero,before).mean(),6)

    def test_production_preempts_only_the_owned_preview_process(self):
        from live_clean_preview import cancel_active
        proc = Mock(pid=34567)
        proc.poll.return_value = None
        child = Mock()
        with patch('live_clean_preview.live_process',proc), patch('psutil.Process') as owner:
            owner.return_value.children.return_value = [child]
            cancel_active()
            owner.assert_called_once_with(proc.pid)
            child.kill.assert_called_once()
            proc.kill.assert_called_once()
            self.assertTrue(proc.preview_cancelled)
