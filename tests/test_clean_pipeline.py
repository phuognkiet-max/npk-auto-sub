import copy
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import cv2
import numpy as np

from core import ROOT, DATA, DEFAULTS, probe, binary, run, create_project, put
from clean_timing import clip_window, frame_time
from worker import clean, preview, read_frame
from job_progress import verify_outputs


class CleanPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='clean-pipeline-',dir=DATA)
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.p = dict(media=probe(ROOT/'tests/fixtures/demo.mp4'), artifacts={},
            params=DEFAULTS | dict(method='telea',temporal=False,workload_mode='fast',dilate=0,video_encoder='cpu'),
            regions=[dict(x=100,y=265,w=45,h=25,start=0,end=8)])

    def test_window_boundaries_and_legacy_container_tail(self):
        media = dict(duration=72.678005,video_duration=72.666667,fps=30)
        for moment in (72.649616,72.678005,71):
            start,length = clip_window(media,moment)
            self.assertAlmostEqual(start,69+2/3)
            self.assertEqual(length,3)
        self.assertEqual(clip_window(media,0),(0,3))
        self.assertEqual(clip_window(media,17.01),(17,3))
        del media['video_duration']
        self.assertAlmostEqual(clip_window(media,72.678005)[0],69+2/3)
        self.assertAlmostEqual(frame_time(media,72.678005),2179/30)
        self.assertAlmostEqual(frame_time(dict(duration=8,fps=30),8),239/30)
        self.assertEqual(clip_window(dict(duration=1.2,fps=30),1.2),(0,1.2))
        for moment in (-1,80,float('nan'),float('inf')):
            with self.assertRaises(ValueError): clip_window(media,moment)

    def test_real_clip_start_middle_tail_and_exact_end(self):
        original = copy.deepcopy(self.p)
        for method, moment in [('telea',0),('telea',2.25),('telea',7.999),('blur',8)]:
            with self.subTest(method=method,time=moment):
                self.p['params']['method'] = method
                job = dict(kind='clip',time=moment)
                result = clean(self.p,job,self.folder)
                verified = verify_outputs(result,self.p,job,self.folder)
                fps = self.p['media']['fps']
                self.assertEqual(verified['video_frames_decoded']['clean.mp4'],round(3*fps))
                self.assertAlmostEqual(result['duration'],3)
                self.assertAlmostEqual(result['start'],min(int(moment*fps)/fps,5))
        self.assertEqual(self.p['media'],original['media'])
        self.assertEqual(self.p['regions'],original['regions'])

    def make_source(self, name, duration=1.2, audio_tail=False, vfr=False):
        path = self.folder/name
        command = [binary('ffmpeg'),'-v','error','-y','-f','lavfi','-i',f'testsrc2=s=160x120:r=30:d={duration}']
        if audio_tail:
            command += ['-f','lavfi','-i','sine=frequency=500:duration=3','-c:a','aac']
        if vfr:
            command += ['-vf',"select='if(lt(n,60),not(mod(n,2)),1)'",'-fps_mode','vfr']
        command += ['-c:v','libx264','-threads','1',str(path)]
        run(command,timeout=30)
        self.p['media'] = probe(path)
        self.p['regions'] = [dict(x=20,y=80,w=60,h=20,start=0,end=self.p['media']['duration'])]
        return self.p['media']

    def test_short_no_audio_full_render_and_audio_longer_than_video(self):
        for name, audio_tail in [('short.mp4',False),('audio-tail.mp4',True)]:
            self.make_source(name,audio_tail=audio_tail)
            job = dict(kind='clip',time=self.p['media']['duration'])
            result = clean(self.p,job,self.folder)
            self.assertAlmostEqual(result['duration'],1.2)
            self.assertEqual(verify_outputs(result,self.p,job,self.folder)['video_frames_decoded']['clean.mp4'],36)
            self.assertEqual(probe(self.folder/'clean.mp4')['audio'],audio_tail)
        self.make_source('full.mp4')
        result = clean(self.p,dict(kind='clean'),self.folder)
        verify_outputs(result,self.p,dict(kind='clean'),self.folder)

    def test_variable_frame_rate_tail_normalizes_without_missing_frames(self):
        self.make_source('vfr.mp4',duration=4,vfr=True)
        job = dict(kind='clip',time=self.p['media']['duration'])
        result = clean(self.p,job,self.folder)
        self.assertLessEqual(abs(result['duration']-3),1/self.p['media']['fps'])
        verify_outputs(result,self.p,job,self.folder)

    def test_still_and_live_at_exact_end_have_a_readable_picture(self):
        self.assertIsNotNone(read_frame(self.p['media']['path'],self.p['media']['duration']))
        for method in ('blur','telea'):
            self.p['params']['method'] = method
            result = preview(self.p,dict(time=8),self.folder)
            self.assertLess(result['time'],8)
            self.assertTrue((self.folder/'after.jpg').stat().st_size>100)

    def decoder(self, pixels=b'', code=0):
        proc = Mock()
        proc.stdout = io.BytesIO(pixels)
        proc.wait.return_value = code
        proc.poll.return_value = code
        return proc

    def test_empty_decoder_and_failure_do_not_load_model_or_publish(self):
        for pixels,code,pattern in [(b'',0,'ngắt sớm'),(b'',1,'giải mã'),(b'incomplete',0,'thiếu dữ liệu')]:
            decoder = self.decoder(pixels,code)
            with patch('core.probe',return_value=self.p['media']),patch('worker.subprocess.Popen',return_value=decoder),patch('cleaner.Cleaner') as model:
                with self.assertRaisesRegex(RuntimeError,pattern):
                    clean(self.p,dict(kind='clip',time=0),self.folder)
                model.assert_not_called()
                self.assertTrue(decoder.stdout.closed)
                self.assertFalse((self.folder/'clean.mp4').exists())

    def test_model_load_or_encoder_start_failure_releases_decoder(self):
        pixels = np.zeros((360,640,3),np.uint8).tobytes()
        for step in ('model','encoder'):
            decoder = self.decoder(pixels)
            decoder.poll.return_value = None
            factory = Mock(side_effect=RuntimeError('model failed')) if step=='model' else Mock()
            with patch('core.probe',return_value=self.p['media']),patch('worker.subprocess.Popen',side_effect=[decoder,OSError('encoder failed')]),patch('cleaner.Cleaner',factory):
                with self.assertRaises((RuntimeError,OSError)):
                    clean(self.p,dict(kind='clip',time=0),self.folder)
            decoder.kill.assert_called_once()
            decoder.wait.assert_called_once()
            self.assertTrue(decoder.stdout.closed)

    def test_inactive_region_rejected_before_model_loading(self):
        self.p['regions'][0].update(start=0,end=1)
        with patch('cleaner.Cleaner') as model:
            with self.assertRaisesRegex(ValueError,'không có vùng'):
                clean(self.p,dict(kind='clip',time=7.9),self.folder)
            model.assert_not_called()

    def test_output_metadata_cannot_hide_missing_video_frames(self):
        (self.folder/'clean.mp4').write_bytes(b'fake')
        info = dict(duration=8,video_duration=8,fps=30,audio=True)
        with patch('core.probe',return_value=info),patch('core.run') as decode:
            decode.return_value.stdout = b'frame=200\nprogress=end\n'
            with self.assertRaisesRegex(ValueError,'số khung'):
                verify_outputs(dict(files=['clean.mp4']),self.p,dict(kind='clean'),self.folder)
            info['video_duration'] = 7
            with self.assertRaisesRegex(ValueError,'Luồng hình'):
                verify_outputs(dict(files=['clean.mp4']),self.p,dict(kind='clean'),self.folder)

    def test_api_accepts_exact_end_but_not_out_of_range(self):
        import server
        from fastapi.testclient import TestClient
        p = create_project(str(ROOT/'tests/fixtures/demo.mp4'),'Clip boundary unit')
        p.update(regions=self.p['regions'])
        p['params'].update(method='telea')
        put('project',p['id'],p)
        client = TestClient(server.app,headers={'x-studio-token':server.TOKEN})
        response = client.post('/api/jobs',json=dict(project_id=p['id'],kind='clip',time=8))
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['time'],8)
        response = client.post('/api/jobs',json=dict(project_id=p['id'],kind='clip',time=8.1))
        self.assertEqual(response.status_code,400)

    def test_region_transitions_do_not_carry_old_mask_or_change_outside(self):
        from cleaner import Cleaner
        self.make_source('transitions.mp4')
        self.p['params']['temporal'] = True
        self.p['regions'] = [dict(x=20,y=80,w=30,h=20,start=0,end=.5),
                             dict(x=100,y=80,w=30,h=20,start=.5,end=1.2)]
        calls = []
        class RecordingCleaner(Cleaner):
            def __call__(self, frame, regions, moment):
                restored, mask = super().__call__(frame,regions,moment)
                np.testing.assert_array_equal(restored[mask==0],frame[mask==0])
                calls.append((moment,bool(mask[85,25]),bool(mask[85,105])))
                return restored,mask
        with patch('cleaner.Cleaner',RecordingCleaner):
            result = clean(self.p,dict(kind='clip',time=0),self.folder)
        self.assertEqual(len(calls),36)
        self.assertTrue(all(left and not right for t,left,right in calls if t < .5))
        self.assertTrue(all(right and not left for t,left,right in calls if t >= .5))
        verify_outputs(result,self.p,dict(kind='clip',time=0),self.folder)

    def test_invalid_ai_output_and_out_of_memory_do_not_fallback_silently(self):
        import torch
        from cleaner import Cleaner
        model = Cleaner.__new__(Cleaner)
        model.torch,model.device = torch,'cpu'
        frame = np.zeros((32,32,3),np.uint8)
        mask = np.ones((32,32),np.uint8)*255
        for bad in (torch.zeros((1,1,32,32)),torch.full((1,3,32,32),float('nan'))):
            model.model = lambda x,m:bad
            with self.assertRaisesRegex(RuntimeError,'không hợp lệ'): model.predict(frame,mask,32)
        model.params = DEFAULTS | dict(method='lama',dilate=0)
        model.previous = None
        model.model = object()
        model.predict = Mock(side_effect=torch.cuda.OutOfMemoryError('simulated'))
        with patch.object(torch.cuda,'empty_cache') as clear:
            with self.assertRaisesRegex(RuntimeError,'Không tự giảm chất lượng'):
                model(frame,[dict(x=5,y=5,w=20,h=20)],0)
            clear.assert_called_once()
