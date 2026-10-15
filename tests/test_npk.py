import json
from pathlib import Path
import unittest
from unittest.mock import patch
import numpy as np
from core import DATA, DEFAULTS, MODELS, signature
from cleaner import Cleaner, make_mask
from performance import FramePacer, profile_for, ffmpeg_limits, create_ocr
from process_control import matches_job
from core import ROOT
from tracking import MaskTimeline, cleanup_regions, tracking_signature
from credentials import credential_status, store_credential
from server import validate_params

class UpgradeTests(unittest.TestCase):
    def test_core_mask_is_fully_replaced_not_translucent(self):
        original = np.full((64, 100, 3), 255, np.uint8)
        cleaner = Cleaner(MODELS/'unused', DEFAULTS | dict(method='telea', temporal=False, dilate=0))
        cleaner.model = object()
        cleaner.predict = lambda roi, mask, side: np.zeros_like(roi)
        result, mask = cleaner(original, [dict(x=20,y=20,w=40,h=20)], 0)
        self.assertTrue((result[mask>0] == 0).all())
        np.testing.assert_array_equal(original[mask==0], result[mask==0])

    def test_empty_frame_skips_model(self):
        original = np.ones((64,100,3), np.uint8)
        cleaner = Cleaner(MODELS/'unused', DEFAULTS | dict(method='telea'))
        cleaner.model = object()
        cleaner.predict = lambda *args: self.fail('No model call on empty mask')
        result, mask = cleaner(original, [], 0)
        np.testing.assert_array_equal(original, result)
        self.assertFalse(mask.any())

    def test_half_open_region_end(self):
        frame=np.zeros((50,50,3),np.uint8)
        self.assertFalse(make_mask(frame,[dict(x=10,y=10,w=10,h=10,start=0,end=1)],DEFAULTS,1).any())

    def test_temporal_never_alters_outside_mask(self):
        frame=np.random.default_rng(10).integers(0,255,(180,320,3),dtype=np.uint8)
        cleaner=Cleaner(MODELS/'unused',DEFAULTS|dict(method='telea',temporal=True))
        regions=[dict(x=130,y=130,w=50,h=20)]
        for _ in range(3):
            result,mask=cleaner(frame,regions,0)
            np.testing.assert_array_equal(frame[mask==0],result[mask==0])

    def test_empty_timeline_intervals_and_boundaries(self):
        regions=[dict(x=10,y=20,w=30,h=10)]
        timeline=MaskTimeline([dict(start=0,end=.5,regions=regions),dict(start=.5,end=1,regions=[])])
        self.assertEqual(timeline.at(.2), regions)
        for time in (-.1,.5,.9,1): self.assertEqual(timeline.at(time),[])

    def test_bad_timeline_rejected(self):
        for samples in ([dict(start=1,end=0,regions=[])], [dict(start=float('nan'),end=2,regions=[])],
                        [dict(start=0,end=1,regions=[]),dict(start=.5,end=2,regions=[])]):
            with self.assertRaises(ValueError): MaskTimeline(samples)

    def test_workload_has_no_quality_reduction(self):
        self.assertEqual(profile_for({})['threads'],1)
        self.assertEqual(profile_for(dict(workload_mode='balanced'))['threads'],2)
        for key in ('quiet','balanced','fast'):
            self.assertNotIn('max_side',profile_for(dict(workload_mode=key)))
        slept=[]
        self.assertAlmostEqual(FramePacer(dict(workload_mode='quiet'),clock=lambda:10,sleeper=slept.append).rest(7),2)
        self.assertEqual(slept,[2.0])
        self.assertEqual(FramePacer(dict(workload_mode='fast'),clock=lambda:10,sleeper=slept.append).rest(7),0)
        self.assertIn('-filter_complex_threads', ffmpeg_limits({}))

    def test_new_enums_validation(self):
        self.assertEqual(validate_params(dict(mask_source='tracked',workload_mode='quiet'))['mask_source'],'tracked')
        with self.assertRaises(ValueError): validate_params(dict(mask_source='foo'))

    def test_exact_worker_identity(self):
        identifier='a'*32
        args=['python.exe',str(ROOT/'worker.py'),str(DATA/'jobs'/identifier/'input.json')]
        self.assertTrue(matches_job(args,identifier))
        self.assertFalse(matches_job(['python.exe','unrelated.py',args[2]],identifier))
        self.assertFalse(matches_job(args,'../escape'))
        self.assertFalse(matches_job(args,'b'*32))

    def test_credentials_readback_no_secret_leak(self):
        secret='unit-test-key-only-abcd'
        with patch('credentials.keyring.get_password',return_value=secret), patch('credentials.keyring.set_password') as write:
            status=store_credential('gemini',secret)
            self.assertTrue(status['read_back'])
            self.assertEqual(status['last4'],'abcd')
            self.assertNotIn(secret,json.dumps(status))
            self.assertFalse(status['provider_verified'])
            write.assert_called_once_with('VietDubStudio','gemini',secret)

    def test_credentials_failed_readback_not_success(self):
        with patch('credentials.keyring.get_password',return_value=None), patch('credentials.keyring.set_password'):
            with self.assertRaises(RuntimeError): store_credential('gemini','unit-test-key')

    def test_track_fingerprint_and_old_render_invalidated(self):
        path=DATA/'source.fixture'
        path.write_bytes(b'test video metadata')
        project=dict(media=dict(path=str(path)),regions=[dict(x=0,y=0,w=10,h=10)],params=DEFAULTS.copy(),artifacts={})
        original=signature(project)
        project['params']['workload_mode']='fast'
        self.assertEqual(signature(project),original)
        project['params']['mask_source']='tracked'
        with self.assertRaisesRegex(ValueError,'Chưa quét'): cleanup_regions(project)
        fingerprint=tracking_signature(project)
        project['params']['ocr_fps']=4
        self.assertNotEqual(tracking_signature(project),fingerprint)
        self.assertNotEqual(signature(project),original)

    def test_ocr_profile_threads_are_passed_to_constructor_without_loading_models(self):
        with patch('rapidocr_onnxruntime.RapidOCR') as constructor:
            for mode, threads in (('quiet', 1), ('balanced', 2), ('fast', 4)):
                result = create_ocr(dict(workload_mode=mode))
                self.assertIs(result, constructor.return_value)
                constructor.assert_called_with(intra_op_num_threads=threads, inter_op_num_threads=1,
                                               det_limit_type='max')
            create_ocr(dict(workload_mode='quiet', ocr_scale_mode='legacy'))
            constructor.assert_called_with(intra_op_num_threads=1, inter_op_num_threads=1,
                                           det_limit_type='min')

if __name__=='__main__': unittest.main(verbosity=2)
