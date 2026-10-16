"""The review warning uses the same language-aware budget as spoken rewriting."""
import copy
import unittest
from core import DEFAULTS
from subtitle_quality import inspect_subtitles


class ReviewBudgetTests(unittest.TestCase):
    def test_vietnamese_nine_syllables_in_two_seconds_are_not_flagged_dense(self):
        project = {'media': {'path': 'synthetic-missing.mp4', 'duration': 2.,
                            'width': 1280, 'height': 720},
                   'params': DEFAULTS | {'target_language': 'vi', 'burn_subtitles': False},
                   'segments': [{'start': 0., 'end': 2., 'text': 'Original dialogue',
                       'translated': 'Nhưng bên dưới lại là một thế giới khác.'}]}
        before = copy.deepcopy(project)
        self.assertNotIn('reading', [item['code'] for item in inspect_subtitles(project)['issues']])
        self.assertEqual(project, before)
        project['segments'][0]['end'] = 1.
        self.assertIn('reading', [item['code'] for item in inspect_subtitles(project)['issues']])
        project['params']['words_per_second'] = 10.
        self.assertNotIn('reading', [item['code'] for item in inspect_subtitles(project)['issues']])
        project['params'].update(target_language='English', words_per_second=3.6)
        project['segments'][0]['end'] = 2.
        self.assertIn('reading', [item['code'] for item in inspect_subtitles(project)['issues']])


if __name__ == '__main__':
    unittest.main()
