"""Caption/object OCR contracts; fixture engines only, no model/provider calls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

import numpy as np

from core import DEFAULTS,ROOT
from ocr_tracks import OCRTrackAssembler,selection_quality_issues
from translation_quality import ocr_same_content
from video_sampling import reading_order
import worker


def observation(text,bbox,region='fallback',confidence=.99,index=0):
    return {'id':str(index),'text':text,'bbox':bbox,'region_id':region,'confidence':confidence}


def detection(text,bbox,confidence=.99):
    x,y,r,b=bbox
    return ([[x,y],[r,y],[r,b],[x,b]],text,confidence)


class OCRTrackContractTests(unittest.TestCase):
    def test_selection_messages_do_not_promote_background_warning_to_review(self):
        selection={'requires_review':True,'issues':[
            {'code':'competing_caption_band','severity':'review','track_ids':['caption','credits']},
            {'code':'multiline_text_block','severity':'warning','track_id':'background'}]}
        issues=selection_quality_issues(selection)
        self.assertEqual([issue['severity'] for issue in issues],['review','warning'])
        self.assertIn('Chọn lớp',issues[0]['message'])
        self.assertIn('không được đưa vào lời đọc',issues[1]['message'])
        self.assertNotIn('track_ids',issues[0]['message'])

    def test_background_document_warning_is_visible_without_required_review(self):
        issues=selection_quality_issues({'requires_review':False,'issues':[
            {'code':'multiline_text_block','severity':'warning'}]})
        self.assertEqual(len(issues),1)
        self.assertEqual(issues[0]['severity'],'warning')

    def test_missing_review_details_have_a_safe_user_facing_fallback(self):
        issues=selection_quality_issues({'requires_review':True,'issues':[]})
        self.assertEqual(issues[0]['severity'],'review')
        self.assertIn('Chọn lớp',issues[0]['message'])

    def test_small_caption_motion_keeps_a_single_band_and_full_text(self):
        tracker=OCRTrackAssembler(1280,720,1.)
        for index,top in enumerate([648,659,654,664]):
            tracker.observe(float(index),[observation(f'Câu {index}',[310,top,970,top+33])],4.)
        self.assertEqual(len(tracker.state['tracks']),1)
        self.assertEqual([row['text'] for row in tracker.rows()],[f'Câu {index}' for index in range(4)])
        self.assertFalse(tracker.selection()['requires_review'])

    def test_nonconcurrent_relocated_caption_is_retained_and_needs_explicit_choice(self):
        tracker=OCRTrackAssembler(1280,720,1.)
        tracker.observe(0.,[observation('Phụ đề đáy',[310,655,970,690])],2.)
        tracker.observe(1.,[observation('Phụ đề chuyển lên',[310,500,970,535])],2.)
        self.assertEqual(len(tracker.state['tracks']),2)
        selection=tracker.selection()
        self.assertTrue(selection['requires_review'])
        self.assertIn('unselected_temporal_band',[issue['code'] for issue in selection['issues']])
        self.assertEqual({run['text'] for track in tracker.state['tracks'] for run in track['runs']},
                         {'Phụ đề đáy','Phụ đề chuyển lên'})

    def test_geometry_deduplicates_jitter_from_overlapping_regions(self):
        boxes=[(20,10,160,30,'Xin chào'),(21,11,161,31,'Xin chào')]
        self.assertEqual(reading_order(boxes),'Xin chào')

    def test_repeated_words_at_independent_positions_are_preserved(self):
        self.assertEqual(reading_order([(0,10,25,30,'hi'),(30,10,55,30,'hi')]),'hi hi')

    def test_different_text_in_same_box_never_discarded_as_duplicate(self):
        text=reading_order([(20,10,160,30,'小王'),(21,11,161,31,'小李')])
        self.assertIn('小王',text)
        self.assertIn('小李',text)

    def test_fuzzy_name_and_meaning_changes_are_distinct(self):
        for first,second in [('今天买牛肉100元','今天买羊肉100元'),
                             ('小王今天在学校参加比赛','小李今天在学校参加比赛'),
                             ('Tôi sẽ làm việc này','Tôi sẽ không làm việc này'),
                             ('Xin chào các bạn hôm nay','Xin chào cac bạn hôm nay')]:
            with self.subTest(first=first):
                self.assertFalse(ocr_same_content(first,second))

    def test_caption_continuity_does_not_depend_on_headline_or_watermark(self):
        tracker=OCRTrackAssembler(1280,720,.5)
        for index in range(6):
            tracker.observe(index*.5,[
                observation('Watermark'+str(index),[10,670,160,687],index=0),
                observation('Headline'+str(index),[480,440,790,459],index=1),
                observation('Đây là lời cần đọc',[280,640,1000,675],index=2)],3)
        self.assertEqual(tracker.rows(),[{'start':0.,'end':3.,'text':'Đây là lời cần đọc','translated':''}])
        self.assertEqual(len(tracker.state['tracks']),3)

    def test_changed_caption_name_gets_new_run_on_same_track(self):
        tracker=OCRTrackAssembler(1280,720,.5)
        for index,text in enumerate(['小王今天在学校参加比赛','小李今天在学校参加比赛']):
            tracker.observe(index*.5,[observation(text,[320,640,960,680])],1)
        self.assertEqual(len(tracker.state['tracks']),1)
        self.assertEqual([row['text'] for row in tracker.rows()],
                         ['小王今天在学校参加比赛','小李今天在学校参加比赛'])

    def test_document_background_retained_without_blocking_clear_caption(self):
        tracker=OCRTrackAssembler(1280,720,.5)
        document=[observation(f'Đoạn tài liệu {index}',[220,440+index*26,1100,462+index*26],index=index)
                  for index in range(7)]
        tracker.observe(0,document+[observation('Phụ đề chính',[300,655,980,688],index=8)],.5)
        self.assertEqual([row['text'] for row in tracker.rows()],['Phụ đề chính'])
        selection=tracker.selection()
        self.assertFalse(selection['requires_review'])
        self.assertIn('multiline_text_block',[issue['code'] for issue in selection['issues']])
        self.assertIn('Đoạn tài liệu 6',tracker.manifest()['tracks'][0]['runs'][0]['text'])

    def test_only_document_needs_selection_review_and_is_not_spoken(self):
        tracker=OCRTrackAssembler(1280,720,.5)
        tracker.observe(0,[observation(f'Tài liệu {index}',[220,440+index*26,1100,462+index*26],index=index)
                           for index in range(7)],.5)
        self.assertEqual(tracker.rows(),[])
        self.assertTrue(tracker.selection()['requires_review'])

    def test_partial_region_conflicting_text_is_preserved_for_review(self):
        tracker=OCRTrackAssembler(1280,720,.5,explicit_regions=True)
        tracker.observe(0,[observation('Xin chào bạn',[300,650,980,685],region='0',index=0),
                           observation('chào bạn',[400,650,980,685],region='1',index=1)],.5)
        self.assertTrue(tracker.selection()['requires_review'])
        self.assertIn('overlapping_text_variants',[issue['code'] for issue in tracker.selection()['issues']])

    def test_track_state_roundtrips_and_invalid_evidence_is_rejected(self):
        tracker=OCRTrackAssembler(1280,720,.5)
        tracker.observe(0,[observation('Xin chào',[300,650,980,685])],1)
        tracker.observe(.5,[observation('Hẹn gặp lại',[300,650,980,685])],1)
        restored=OCRTrackAssembler(1280,720,.5,state=tracker.state)
        self.assertEqual(restored.rows(),tracker.rows())
        for mutate in [lambda state:state.update(evidence_bytes=-1),
                       lambda state:state.update(samples_processed=True),
                       lambda state:state['tracks'][0]['runs'][1].update(start=-1),
                       lambda state:state['tracks'][0]['runs'][1].update(first_sample=0),
                       lambda state:state['tracks'][0].update(bbox=[0,0,1281,10])]:
            state=copy.deepcopy(tracker.state)
            mutate(state)
            with self.subTest(state=str(state)[:120]),self.assertRaisesRegex(ValueError,'không hợp lệ'):
                OCRTrackAssembler(1280,720,.5,state=state)

    def test_three_line_caption_is_preserved(self):
        tracker=OCRTrackAssembler(1280,720,.5,explicit_regions=True)
        tracker.observe(0,[observation(f'Dòng {index}',[350,595+index*30,930,620+index*30],region='0',index=index)
                           for index in range(3)],.5)
        self.assertEqual([row['text'] for row in tracker.rows()],['Dòng 0\nDòng 1\nDòng 2'])
        self.assertFalse(tracker.selection()['requires_review'])

    def test_competing_centred_caption_bands_require_review(self):
        tracker=OCRTrackAssembler(1280,720,.5)
        tracker.observe(0,[observation('Dòng trên',[250,560,1030,595],index=0),
                           observation('Dòng dưới',[250,650,1030,685],index=1)],.5)
        self.assertTrue(tracker.selection()['requires_review'])
        self.assertEqual(len(tracker.selection()['selected_tracks']),1)

    def test_explicit_overlapping_regions_read_same_object_once(self):
        tracker=OCRTrackAssembler(1280,720,.5,explicit_regions=True)
        tracker.observe(0,[observation('Xin chào',[300,650,980,685],region='0',index=0),
                           observation('Xin chào',[301,651,981,686],region='1',index=1)],.5)
        self.assertEqual([row['text'] for row in tracker.rows()],['Xin chào'])
        self.assertEqual(len(tracker.state['tracks']),1)
        self.assertEqual(tracker.state['tracks'][0]['regions'],['0','1'])


class OCRWorkerEvidenceTests(unittest.TestCase):
    def project(self):
        return {'params':DEFAULTS|{'ocr_fps':2,'workload_mode':'fast'},
                'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':1.,
                         'fps':30.,'width':1280,'height':720},'regions':[], 'extraction_regions':[]}

    def test_worker_publishes_raw_boxes_and_only_selected_caption(self):
        # Default OCR inspects the entire frame, so detections above the old
        # lower-40% crop are eligible caption candidates too.
        engine=Mock(side_effect=[([
            detection('Watermark'+str(index),[10,650,160,667]),
            detection('Headline'+str(index),[480,8,790,27]),
            detection('Phụ đề chính',[280,260,1000,295]),
            detection('Low confidence raw evidence',[200,55,900,75],.2)],None) for index in range(2)])
        frame=np.zeros((720,1280,3),dtype='uint8')
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with patch('worker.create_ocr',return_value=engine),patch('worker.FramePacer'),patch('worker.emit'), \
                    patch('worker.sampled_frames',return_value=iter([(0.,frame),(.5,frame)])):
                result=worker.ocr(self.project(),{},folder)
            self.assertEqual(result['segments'],[{'start':0.,'end':1.,'text':'Phụ đề chính','translated':''}])
            records=[json.loads(line) for line in (folder/'ocr-observations.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(records),2)
            self.assertEqual(len(records[0]['observations']),4)
            self.assertEqual(records[0]['observations'][2]['bbox'],[280,260,1000,295])
            self.assertEqual(engine.call_args_list[0].args[0].shape,frame.shape)
            self.assertIn('ocr-observations.jsonl',result['files'])
            self.assertIn('ocr-tracks.json',result['files'])
            checkpoint=json.loads((folder/'recognition-checkpoint.json').read_text(encoding='utf-8'))
            self.assertEqual(checkpoint['ocr_tracks']['evidence_bytes'],(folder/'ocr-observations.jsonl').stat().st_size)
            self.assertTrue(checkpoint['decoded_complete'])

    def test_resume_truncates_uncommitted_evidence_sample(self):
        p=self.project()
        frame=np.zeros((720,1280,3),dtype='uint8')
        def failed_samples(*_args):
            yield 0.,frame
            raise RuntimeError('fixture decoder interruption')
        engine=Mock(return_value=([detection('Đã lưu',[320,208,960,243])],None))
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with patch('worker.create_ocr',return_value=engine),patch('worker.FramePacer'),patch('worker.emit'), \
                    patch('worker.sampled_frames',side_effect=failed_samples):
                with self.assertRaisesRegex(RuntimeError,'interruption'):
                    worker.ocr(p,{},folder)
            evidence=folder/'ocr-observations.jsonl'
            with evidence.open('ab') as stream:
                stream.write(b'uncommitted crash bytes\n')
            with patch('worker.create_ocr',return_value=engine),patch('worker.FramePacer'),patch('worker.emit'), \
                    patch('worker.sampled_frames',return_value=iter([(.5,frame)])):
                result=worker.ocr(p,{'resume_from':'fixture'},folder)
            self.assertEqual(result['segments'],[{'start':0.,'end':1.,'text':'Đã lưu','translated':''}])
            self.assertNotIn(b'uncommitted',evidence.read_bytes())
            self.assertEqual(len(evidence.read_text(encoding='utf-8').splitlines()),2)

    def test_resume_missing_durable_sidecar_fails_without_redecoding(self):
        p=self.project()
        frame=np.zeros((720,1280,3),dtype='uint8')
        engine=Mock(return_value=([detection('Đã lưu',[320,208,960,243])],None))
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            with patch('worker.create_ocr',return_value=engine),patch('worker.FramePacer'),patch('worker.emit'), \
                    patch('worker.sampled_frames',return_value=iter([(0.,frame),(.5,frame)])):
                worker.ocr(p,{},folder)
            checkpoint=(folder/'recognition-checkpoint.json').read_bytes()
            (folder/'ocr-observations.jsonl').unlink()
            with patch('worker.create_ocr'),patch('worker.sampled_frames') as samples:
                with self.assertRaisesRegex(ValueError,'nhật ký bằng chứng'):
                    worker.ocr(p,{'resume_from':'fixture'},folder)
                samples.assert_not_called()
            self.assertEqual((folder/'recognition-checkpoint.json').read_bytes(),checkpoint)


if __name__=='__main__':
    unittest.main()
