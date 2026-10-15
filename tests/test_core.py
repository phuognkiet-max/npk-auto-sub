import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tempfile
import unittest
import numpy as np
from cleaner import bounds, make_mask, Cleaner
from core import DEFAULTS, ROOT, MODELS, safe_owned, save_srt
from server import validate_params, validate_segments
from worker import tempo_filter

class CoreTests(unittest.TestCase):
    def test_fractional_crop(self):
        self.assertEqual(bounds(dict(x=3.3,y=4.2,w=5.2,h=6.2),100,100),(3,4,9,11))
    def test_crop_outside(self):
        with self.assertRaises(ValueError):
            bounds(dict(x=110,y=1,w=5,h=6),100,100)
    def test_nan_rejected(self):
        with self.assertRaises(ValueError): validate_params({'dilate':float('nan')})
    def test_negative_timestamp_rejected(self):
        with self.assertRaises(ValueError): validate_segments([dict(start=-1,end=2)],10)
    def test_zero_duration_rejected(self):
        with self.assertRaises(ValueError): validate_segments([dict(start=1,end=1)],10)
    def test_clipped_end_must_follow_start(self):
        with self.assertRaises(ValueError): validate_segments([dict(start=10,end=10.2)],10)
    def test_model_choice_rejected(self):
        with self.assertRaises(ValueError): validate_params({'method':'malicious'})
    def test_parent_traversal(self):
        with self.assertRaises(ValueError): safe_owned(ROOT/'data/../../escape')
    def test_root_not_owned_target(self):
        with self.assertRaises(ValueError): safe_owned(ROOT/'data')
    def test_mask_timing(self):
        frame=np.zeros((100,100,3),np.uint8)
        regions=[dict(x=20,y=20,w=10,h=10,start=2,end=5)]
        self.assertFalse(make_mask(frame,regions,DEFAULTS,1).any())
        self.assertTrue(make_mask(frame,regions,DEFAULTS,3).any())
    def test_telea_outside_pixels_unchanged(self):
        frame=np.random.default_rng(1).integers(0,255,(100,150,3),dtype=np.uint8)
        cleaner=Cleaner(MODELS/'lama/big-lama.pt',DEFAULTS|{'method':'telea'})
        result,mask=cleaner(frame,[dict(x=25.2,y=24.5,w=20.2,h=8.5)],0)
        np.testing.assert_array_equal(frame[mask==0],result[mask==0])
    def test_subtitle_unicode_roundtrip(self):
        import pysubs2
        with tempfile.TemporaryDirectory(dir=ROOT/'tests') as folder:
            path=Path(folder)/'test.srt'
            save_srt([dict(start=.12,end=1.8,text='你好',translated='Xin chào\nViệt Nam')],path)
            doc=pysubs2.load(str(path),encoding='utf-8')
            self.assertEqual(doc[0].plaintext,'Xin chào\nViệt Nam')
            self.assertEqual(doc[0].start,120)
    def test_atempo_extreme(self):
        self.assertEqual(tempo_filter(8),'atempo=2,atempo=2,atempo=2.000000')

if __name__=='__main__':unittest.main(verbosity=2)
