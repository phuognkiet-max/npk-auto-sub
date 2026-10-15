"""Resume the final valid video tick without discarding OCR checkpoint data."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np
from core import DEFAULTS, ROOT
from subtitle_quality import digest, source_signature
import worker


class OCRTailResumeTests(unittest.TestCase):
    def test_legacy_signature_resumes_only_final_sample_with_video_coverage(self):
        project={'params':DEFAULTS|{'ocr_fps':2,'extraction_end':3.112},
                 'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':3.112,
                          'video_duration':3.1,'fps':30,'width':16,'height':16},
                 'regions':[],'extraction_regions':[]}
        previous=[{'start':.5,'end':1.5,'text':'已保存','translated':''}]
        # Preserve the original requested container range. A shorter stream
        # tail must not force re-OCR from zero.
        fingerprint=digest({'source':source_signature(project),'regions':[],
            'range':[0,3.112],'interval':.5,'confidence':.55,'similarity':.84,
            'scale':'native','search_area':'automatic_full_frame_v1','version':5})
        engine=Mock(return_value=([],None))
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            checkpoint=folder/'recognition-checkpoint.json'
            checkpoint.write_text(json.dumps({'signature':fingerprint,'rows':previous,
                'current':None,'next_time':3.,'clipped_samples':[]}),encoding='utf-8')
            with patch('worker.create_ocr',return_value=engine),patch('worker.emit'), \
                    patch('worker.FramePacer'),patch('worker.sampled_frames',return_value=iter([
                        (3.,np.zeros((16,16,3),dtype='uint8'))])) as samples:
                result=worker.ocr(project,{'kind':'ocr','resume_from':'previous'},folder)
            self.assertEqual(samples.call_args.args[2:5],(3.,3.1,.5))
            engine.assert_called_once()
            self.assertEqual(result['segments'],previous)
            self.assertEqual(result['coverage']['analyzed_end'],3.1)
            saved=json.loads(checkpoint.read_text(encoding='utf-8'))
            self.assertTrue(saved['decoded_complete'])
            self.assertEqual(saved['signature'],fingerprint)
            self.assertEqual(saved['next_time'],3.5)
            self.assertTrue((folder/'source.srt').is_file())

    def test_audio_only_extraction_window_is_rejected(self):
        project={'params':DEFAULTS|{'extraction_start':3.2,'extraction_end':4.},
                 'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':4.,
                          'video_duration':3.1,'fps':30,'width':16,'height':16},
                 'regions':[],'extraction_regions':[]}
        with tempfile.TemporaryDirectory() as folder,patch('worker.create_ocr'), \
                patch('worker.sampled_frames') as samples:
            with self.assertRaisesRegex(ValueError,'không có hình'):
                worker.ocr(project,{},Path(folder))
            samples.assert_not_called()

    def test_decoder_failure_after_final_sample_keeps_cues_and_does_not_restart(self):
        project={'params':DEFAULTS|{'ocr_fps':2,'extraction_end':.1},
                 'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':.112,
                          'video_duration':.1,'fps':30,'width':16,'height':16},
                 'regions':[],'extraction_regions':[]}
        engine=Mock(return_value=([([[3,3],[12,3],[12,12],[3,12]],'已保存',.99)],None))
        def failed_decoder(*args):
            yield 0.,np.zeros((16,16,3),dtype='uint8')
            raise RuntimeError('decoder exited with error')
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with patch('worker.create_ocr',return_value=engine),patch('worker.emit'), \
                    patch('worker.FramePacer'),patch('worker.sampled_frames',side_effect=failed_decoder):
                with self.assertRaisesRegex(RuntimeError,'decoder exited'):
                    worker.ocr(project,{},folder)
            checkpoint=folder/'recognition-checkpoint.json'
            original=checkpoint.read_bytes()
            saved=json.loads(original)
            self.assertEqual(saved['current']['text'],'已保存')
            self.assertFalse(saved.get('decoded_complete',False))
            self.assertFalse((folder/'source.srt').exists())
            with patch('worker.create_ocr'),patch('worker.sampled_frames') as samples:
                with self.assertRaisesRegex(ValueError,'không tự chạy lại'):
                    worker.ocr(project,{},folder)
                samples.assert_not_called()
            self.assertEqual(checkpoint.read_bytes(),original)

    def test_confirmed_complete_checkpoint_can_republish_without_decoding_again(self):
        from ocr_checkpoint import signature
        project={'params':DEFAULTS|{'extraction_end':.1},
                 'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':.112,
                          'video_duration':.1,'fps':30,'width':16,'height':16},
                 'regions':[],'extraction_regions':[]}
        rows=[{'start':0.,'end':.1,'text':'已保存','translated':''}]
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            (folder/'recognition-checkpoint.json').write_text(json.dumps({
                'signature':signature(project),'rows':rows,'current':None,
                'next_time':.5,'decoded_complete':True}),encoding='utf-8')
            with patch('worker.create_ocr'),patch('worker.emit'), \
                    patch('worker.sampled_frames') as samples:
                result=worker.ocr(project,{},folder)
                samples.assert_not_called()
            self.assertEqual(result['segments'],rows)
            self.assertEqual(result['coverage']['analyzed_end'],.1)


if __name__=='__main__':
    unittest.main()
