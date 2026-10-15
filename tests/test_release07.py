import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from core import DATA, ROOT, DEFAULTS, create_project, put, save_srt
import server


class Release07Tests(unittest.TestCase):
    def setUp(self):
        self.p=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'Release checks')
        self.p['params'].update(use_clean=False,voice_gain=0,pipeline_tts=False,pipeline_translate=False,burn_subtitles=False)

    def test_editor_order_survives_saving(self):
        rows=[dict(start=4,end=5,text='B'),dict(start=1,end=2,text='A')]
        self.assertEqual([r['text'] for r in server.validate_segments(rows,8)],['B','A'])

    def test_reading_regions_do_not_change_clean_regions(self):
        r=dict(x=1,y=1,w=10,h=10,start=0,end=3)
        saved=server.update_project(self.p['id'],{'extraction_regions':[r]})
        self.assertEqual(saved['regions'],[])
        self.assertEqual(saved['extraction_regions'],[r])

    def test_reading_area_outside_clean_is_visible_warning_not_false_quality_failure(self):
        from subtitle_quality import clean_region_warnings
        self.p['params']['use_clean']=True
        self.p['extraction_regions']=[dict(x=10,y=10,w=100,h=40,start=0,end=8)]
        self.p['regions']=[dict(x=30,y=10,w=50,h=40,start=0,end=8)]
        self.assertTrue(clean_region_warnings(self.p))
        self.p['regions']=copy.deepcopy(self.p['extraction_regions'])
        self.assertFalse(clean_region_warnings(self.p))
        self.p['regions'][0]['end']=4
        self.assertTrue(clean_region_warnings(self.p))

    def test_portable_document_has_no_keys_paths_or_artifacts(self):
        from project_transfer import export_document,restore_document
        self.p['params']['api_key']='private-secret-sentinel'
        self.p['artifacts']={'clean':{'job_id':'not-portable'}}
        self.p['segments']=[dict(start=0,end=2,text='Hello',translated='Chào bạn')]
        doc=export_document(self.p)
        encoded=json.dumps(doc)
        self.assertNotIn('private-secret-sentinel',encoded)
        self.assertNotIn('not-portable',encoded)
        self.assertNotIn(self.p['media']['path'],encoded)
        restored=restore_document(doc,self.p['media']['path'])
        self.assertNotEqual(restored['id'],self.p['id'])
        self.assertEqual(restored['segments'],self.p['segments'])
        self.assertEqual(restored['artifacts'],{})

    def test_low_disk_guard_has_interval_and_stops(self):
        from resource_budget import DiskGuard
        guard=DiskGuard(DATA,100,clock=lambda:5)
        with patch('resource_budget.shutil.disk_usage',return_value=MagicMock(free=10)):
            with self.assertRaisesRegex(RuntimeError,'sắp đầy'):guard.check()

    def test_caption_wrap_never_discards_words(self):
        from subtitle_layout import wrap_caption,output_dimensions
        text='Xin chào bạn. Đây là bản phụ đề tiếng Việt để kiểm tra xuống dòng.'
        self.assertEqual(wrap_caption(text,{'subtitle_line_chars':20}).split(),text.split())
        self.assertEqual(output_dimensions({'width':1920,'height':1080},{'export_resolution':'720'}),(1280,720))
        self.assertEqual(output_dimensions({'width':1080,'height':1920},{'export_resolution':'720'}),(720,1280))
        self.assertEqual(output_dimensions({'width':640,'height':360},{'export_resolution':'1080'}),(640,360))

    def test_per_cue_style_and_voice_are_preserved(self):
        import pysubs2
        rows=server.validate_segments([dict(start=0,end=2,text='Xin chào',voice_id='piper:vais1000',subtitle_style={'font_size':72,'font_color':'#FF8800'})],8)
        with tempfile.TemporaryDirectory(dir=DATA) as folder:
            path=Path(folder)/'styled.ass'
            save_srt(rows,path,ass_style=(DEFAULTS,self.p['media']))
            doc=pysubs2.load(str(path));style=doc.styles[doc.events[0].style]
            self.assertEqual(style.fontsize,72);self.assertEqual(style.primarycolor.r,255)
        self.assertEqual(rows[0]['voice_id'],'piper:vais1000')

    def test_voice_signature_ignores_style_not_voice_override(self):
        from worker import speech_signature
        self.p['segments']=[dict(start=0,end=2,text='Hello',translated='Xin chào')]
        before=speech_signature(self.p)
        self.p['segments'][0]['subtitle_style']={'font_size':80}
        self.assertEqual(before,speech_signature(self.p))
        self.p['segments'][0]['voice_id']='piper:vais1000'
        self.assertNotEqual(before,speech_signature(self.p))

    def test_video_without_requested_text_is_exportable(self):
        from preflight import inspect
        self.assertTrue(inspect(self.p,'pipeline')['ok'])

    def test_publication_never_exposes_partial_or_overwrites(self):
        from exports import publish_verified
        with tempfile.TemporaryDirectory(dir=DATA) as folder:
            folder=Path(folder);job=folder/'job';job.mkdir();(job/'final.mp4').write_bytes(b'test-final')
            params={'export_directory':str(folder),'export_name':'result'}
            with patch('exports.shutil.copyfileobj',side_effect=OSError('simulated disk full')):
                with self.assertRaises(OSError):publish_verified({'params':params},job)
            self.assertEqual(list(folder.glob('*.mp4')),[])
            self.assertEqual(list(folder.glob('*.partial')),[])
            first=publish_verified({'params':params},job)
            second=publish_verified({'params':params},job)
            self.assertNotEqual(first['path'],second['path'])
            with self.assertRaises(ValueError):publish_verified({'params':params},job,unique=False)
            self.assertEqual(Path(first['path']).read_bytes(),b'test-final')

    def test_delivery_failure_preserves_verified_result(self):
        from exports import publish_or_keep_local
        result={'primary_file':'final.mp4','verification':{'files_checked':3}}
        with patch('exports.publish_verified',side_effect=OSError('disk disconnected')):
            saved=publish_or_keep_local(self.p,DATA,result)
        self.assertTrue(saved['delivery_pending'])
        self.assertEqual(saved['primary_file'],'final.mp4')
        self.assertEqual(saved['verification']['files_checked'],3)
        self.assertIn('Không cần xử lý',saved['warnings'][0])

    def ocr_mocks(self,folder,fail=False):
        import numpy as np
        from worker import ocr
        self.p['media'].update(duration=1.000952,width=12,height=12,fps=30)
        self.p['params'].update(ocr_fps=2,extraction_start=0,extraction_end=0)
        cap=MagicMock();frame=np.zeros((12,12,3),dtype='uint8')
        def samples(media,params,start,end,interval,log):
            from video_sampling import sample_count
            for index in range(sample_count(start,end,interval,media['fps'])):
                cap.read()
                if fail and index==1:raise RuntimeError('Không đọc được khung hình ở 0.50 giây')
                yield start+index*interval,frame
        engine=MagicMock(return_value=([([[0,0],[3,0],[3,3],[0,3]],'测试',.99)],None))
        with patch('worker.create_ocr',return_value=engine),patch('worker.sampled_frames',side_effect=samples),patch('worker.FramePacer'),patch('worker.emit'):
            result=ocr(self.p,{'kind':'ocr'},Path(folder))
        return result,cap,engine

    def test_ocr_does_not_sample_past_rounded_last_frame(self):
        with tempfile.TemporaryDirectory(dir=DATA) as folder:
            result,cap,engine=self.ocr_mocks(folder)
            self.assertEqual(cap.read.call_count,2)

    def test_reading_order_groups_baseline_before_x(self):
        from video_sampling import reading_order
        self.assertEqual(reading_order([(30,10,50,30,'B'),(0,11,25,31,'A'),(0,50,30,70,'C')]),'A B\nC')

    def test_checkpoint_invalidated_by_ocr_scale(self):
        with tempfile.TemporaryDirectory(dir=DATA) as folder:
            self.ocr_mocks(folder)
            self.p['params']['ocr_scale_mode']='legacy'
            result,cap,engine=self.ocr_mocks(folder)
            self.assertEqual(cap.read.call_count,2)
            self.assertEqual(result['coverage']['analyzed_end'],1.000952)

    def test_ocr_interruption_is_error_and_resumes_exact_checkpoint(self):
        with tempfile.TemporaryDirectory(dir=DATA) as folder:
            with self.assertRaisesRegex(RuntimeError,'0.50'):
                self.ocr_mocks(folder,fail=True)
            self.assertFalse((Path(folder)/'source.srt').exists())
            saved=json.loads((Path(folder)/'recognition-checkpoint.json').read_text(encoding='utf-8'))
            self.assertEqual(saved['next_time'],.5)
            result,cap,engine=self.ocr_mocks(folder)
            self.assertEqual(cap.read.call_count,1)
            self.assertEqual(len(result['segments']),1)
            self.assertEqual(result['segments'][0]['start'],0)

    def test_checkpoint_does_not_resume_after_region_change(self):
        with tempfile.TemporaryDirectory(dir=DATA) as folder:
            self.ocr_mocks(folder)
            self.p['extraction_regions']=[dict(x=0,y=0,w=10,h=10,start=0,end=1)]
            result,cap,engine=self.ocr_mocks(folder)
            self.assertEqual(cap.read.call_count,2)
