import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import cv2
import numpy as np

from core import DEFAULTS, ROOT, MODELS, probe, signature
from cleaner import Cleaner, blend_effect, stretch_source
from fast_cover import preview_fast, clean_fast
from server import validate_params
from worker import preview


class CleanEffectTests(unittest.TestCase):
    def setUp(self):
        cv2.setNumThreads(1)
        self.tmp = tempfile.TemporaryDirectory(prefix='npk-clean-effects-', dir=ROOT/'tests')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.project = dict(media=probe(ROOT/'tests/fixtures/demo.mp4'),
            params=DEFAULTS | dict(method='blur', temporal=False, video_encoder='cpu', dilate=0),
            regions=[dict(x=100,y=265,w=420,h=75,start=0,end=8)], artifacts={})

    def image(self, method, **params):
        self.project['params'].update(method=method, **params)
        preview_fast(self.project, 0, self.folder)
        return cv2.imread(str(self.folder/'after.jpg'))

    def test_opacity_and_feather_apply_to_real_pixels(self):
        zero = self.image('box', cover_opacity=0, edge_feather=0)
        before = cv2.imread(str(self.folder/'before.jpg'))
        solid = self.image('box', cover_opacity=1)
        half = self.image('box', cover_opacity=.5)
        soft = self.image('box', cover_opacity=1, edge_feather=20)
        roi = np.s_[285:315,180:440]
        self.assertLess(np.abs(zero[roi].astype(float)-before[roi]).mean(), 5)
        self.assertLess(solid[roi].mean(), 5)
        self.assertGreater(half[roi].mean(), solid[roi].mean()+20)
        self.assertLess(half[roi].mean(), zero[roi].mean()-20)
        self.assertGreater(soft[267:272,180:440].mean(), solid[267:272,180:440].mean()+20)

    def test_blur_strength_and_stretch_controls_change_pixels(self):
        low = self.image('blur', blur_radius=2)
        high = self.image('blur', blur_radius=32)
        self.assertGreater(cv2.absdiff(low[270:338,110:510], high[270:338,110:510]).mean(), 5)
        soft = self.image('blur', cover_opacity=.65, edge_feather=12)
        self.assertGreater(cv2.absdiff(high[270:338,110:510],soft[270:338,110:510]).mean(), 2)
        a = self.image('stretch', cover_opacity=1, edge_feather=0, stretch_offset=0, stretch_height=4)
        b = self.image('stretch', stretch_offset=80, stretch_height=40, edge_feather=8)
        self.assertGreater(cv2.absdiff(a[270:338,110:510], b[270:338,110:510]).mean(), 2)

    def test_preview_filter_matches_encoded_video(self):
        for method in ('blur','stretch','box'):
            with self.subTest(method=method):
                still = self.image(method,cover_opacity=.8,edge_feather=10)
                events = []
                result = clean_fast(self.project, {'kind':'clip','time':0}, self.folder, lambda **v:events.append(v))
                cap = cv2.VideoCapture(str(self.folder/'clean.mp4'))
                ok, frame = cap.read(); cap.release()
                self.assertTrue(ok)
                self.assertLess(cv2.absdiff(still[275:330,110:510],frame[275:330,110:510]).mean(), 8)
                self.assertEqual(result['signature'],signature(self.project))
                self.assertFalse(result['ai_inference'])

    def test_inpaint_radius_is_used_and_blend_preserves_outside(self):
        frame = np.random.default_rng(10).integers(0,255,(80,120,3),dtype=np.uint8)
        p = DEFAULTS | dict(method='telea', temporal=False,dilate=0,inpaint_radius=2,cover_opacity=.6,edge_feather=4)
        original_inpaint = cv2.inpaint
        with patch('cleaner.cv2.inpaint', wraps=original_inpaint) as call:
            output,mask = Cleaner(MODELS/'unused',p)(frame,[dict(x=20,y=30,w=60,h=20)],0)
        self.assertEqual(call.call_args.args[2], 2)
        np.testing.assert_array_equal(frame[mask==0],output[mask==0])
        self.assertGreater(cv2.absdiff(frame,output).max(),0)
        np.testing.assert_array_equal(frame,blend_effect(frame,output,mask,dict(cover_opacity=0)))

    def test_ranges_and_signature_prevent_reusing_stale_results(self):
        for key,value in dict(cover_opacity=1.1,edge_feather=33,stretch_offset=-1,inpaint_radius=10).items():
            with self.assertRaises(ValueError): validate_params({key:value})
        for key,value in dict(cover_opacity=.6,edge_feather=8,blur_radius=32).items():
            p=copy.deepcopy(self.project);p['params'][key]=value
            self.assertNotEqual(signature(self.project), signature(p))
        self.project['params']['method']='telea'
        p=copy.deepcopy(self.project);p['params']['inpaint_radius']=2
        self.assertNotEqual(signature(self.project),signature(p))

    def test_preview_rejects_inactive_region_and_stretch_bounds(self):
        self.project['regions'][0]['start']=2
        with self.assertRaisesRegex(ValueError,'không có vùng'):
            preview(self.project,dict(time=0),self.folder)
        self.assertEqual(stretch_source(0,20,100,5,'above',10),(30,5))
        with self.assertRaises(ValueError): stretch_source(0,99,100,12,'above',0)
