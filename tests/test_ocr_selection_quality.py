"""Selection uncertainty must not promote an unrelated document warning."""
import unittest

from core import DEFAULTS,ROOT
from subtitle_quality import coverage_record,inspect_subtitles


class OCRSelectionQualityTests(unittest.TestCase):
    def project(self,selection):
        project={'params':DEFAULTS|{'burn_subtitles':False},
                 'media':{'path':str(ROOT/'tests/fixtures/demo.mp4'),'duration':2.,'width':1280,'height':720},
                 'segments':[{'start':0.,'end':2.,'text':'Xin chào','translated':''}]}
        project['coverage']=coverage_record(project,'ocr',0.,2.,ocr_selection=selection)
        return project

    def test_unrelated_document_warning_stays_warning_during_caption_review(self):
        selection={'requires_review':True,'issues':[
            {'code':'competing_caption_band','severity':'review','track_ids':['caption','credits']},
            {'code':'multiline_text_block','severity':'warning','track_id':'background'}]}
        quality=inspect_subtitles(self.project(selection))
        selection_issues=[issue for issue in quality['issues'] if issue['code'].startswith('ocr_')]
        self.assertTrue(quality['requires_review'])
        self.assertEqual([issue['severity'] for issue in selection_issues],['review','warning'])
        self.assertFalse(any('track_ids' in issue['message'] for issue in selection_issues))

    def test_clear_caption_keeps_background_warning_visible_without_pause(self):
        selection={'requires_review':False,'issues':[{'code':'multiline_text_block','severity':'warning'}]}
        quality=inspect_subtitles(self.project(selection))
        selection_issues=[issue for issue in quality['issues'] if issue['code'].startswith('ocr_')]
        self.assertFalse(quality['requires_review'])
        self.assertEqual(len(selection_issues),1)
        self.assertEqual(selection_issues[0]['severity'],'warning')


if __name__=='__main__':
    unittest.main()
