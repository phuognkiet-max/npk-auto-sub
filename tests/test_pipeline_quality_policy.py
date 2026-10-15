"""Full setup continues past advisory OCR hints while real gaps stay resumable."""
import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import wave

from core import DATA, DEFAULTS, ROOT, probe, save_srt
from job_progress import verify_outputs
from subtitle_quality import coverage_record, inspect_subtitles, review_signature
import worker


class PipelineQualityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='pipeline-policy-', dir=DATA)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.project = {
            'id': 'a' * 32, 'name': 'Private pipeline quality fixture', 'updated': 1,
            'media': probe(ROOT / 'tests/fixtures/demo.mp4'), 'artifacts': {}, 'regions': [],
            'params': DEFAULTS | {'pipeline_extract': 'ocr', 'subtitle_automation': 'manual', 'pipeline_translate': True,
                'pipeline_tts': True, 'pipeline_clean': True, 'burn_subtitles': False,
                'use_clean': False, 'original_gain': 0, 'voice_gain': 1},
            'segments': [],
        }
        self.recognized = [
            {'start': 0., 'end': .6, 'text': '啊', 'translated': ''},
            {'start': .6, 'end': 4., 'text': '你好，请看这段视频', 'translated': ''},
            {'start': 4., 'end': self.project['media']['duration'], 'text': '谢谢，再见', 'translated': ''},
        ]
        self.calls = []
        self.job = {'id': 'b' * 32, 'kind': 'pipeline'}

    def recognized_project(self, **details):
        project = copy.deepcopy(self.project)
        project['segments'] = copy.deepcopy(self.recognized)
        project['coverage'] = coverage_record(project, 'ocr', 0, project['media']['duration'],
                                             clipped_samples=[.3, 1.5], **details)
        return project

    def engines(self, coverage=None):
        def recognize(project, job, folder):
            self.calls.append('ocr')
            save_srt(self.recognized, folder / 'source.srt', translated=False)
            record = coverage_record(project, 'ocr', 0, project['media']['duration'],
                                     clipped_samples=[.3, 1.5])
            if coverage is not None:
                record.update(coverage)
            return {'type': 'ocr', 'files': ['source.srt'], 'segments': copy.deepcopy(self.recognized),
                    'coverage': record, 'warnings': ['OCR retained every recognized word.']}

        def translate(project, job, folder):
            self.calls.append('translate')
            self.assertEqual([row['text'] for row in project['segments']],
                             [row['text'] for row in self.recognized])
            rows = [row | {'translated': f'Lời dịch đầy đủ câu {index + 1}.'}
                    for index, row in enumerate(project['segments'])]
            save_srt(rows, folder / 'translated.srt')
            return {'type': 'translate', 'files': ['translated.srt'], 'segments': rows,
                    'warnings': ['Translation warning retained.']}

        def synthesize(project, job, folder):
            self.calls.append('tts')
            self.assertTrue(all(row['translated'] for row in project['segments']))
            with wave.open(str(folder / 'voice.wav'), 'wb') as audio:
                audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b'\x10\x00' * round(project['media']['duration'] * 16000))
            return {'type': 'tts', 'files': ['voice.wav'], 'sample_rate': 16000,
                    'warnings': ['Timing warning retained.']}

        def clean(project, job, folder):
            self.calls.append('clean')
            shutil.copyfile(project['media']['path'], folder / 'clean.mp4')
            return {'type': 'clean', 'files': ['clean.mp4']}

        def render(project, job, folder):
            self.calls.append('render')
            self.assertTrue(project['params']['use_clean'])
            self.assertEqual(set(project['artifacts']), {'ocr', 'translate', 'tts', 'clean'})
            shutil.copyfile(project['media']['path'], folder / 'final.mp4')
            return {'type': 'render', 'files': ['final.mp4'], 'primary_file': 'final.mp4',
                    'warnings': ['Translation warning retained.']}

        return {'ocr': recognize, 'translate': translate, 'tts': synthesize,
                'clean': clean, 'render': render}

    def run_pipeline(self, coverage=None):
        private = copy.deepcopy(self.project)
        with patch.dict(worker.TASKS, self.engines(coverage)), patch('worker.emit'):
            result = worker.pipeline(private, self.job, self.folder)
        # Verify real WAV bytes and fully decode copied fixture video. The
        # stage engines stay offline; no provider, OCR or TTS model is loaded.
        checked = verify_outputs(result, private, self.job, self.folder)
        return result, checked, private

    def test_short_character_and_rectangle_edge_are_advisory_without_deleting_words(self):
        project = self.recognized_project()
        original = copy.deepcopy(project)
        report = inspect_subtitles(project)
        hints = {issue['code']: issue for issue in report['issues']}
        self.assertEqual(hints['brief_fragment']['severity'], 'warning')
        self.assertEqual(hints['cropped_text']['severity'], 'warning')
        self.assertFalse(report['requires_review'])
        self.assertFalse(report['reviewed'])
        self.assertEqual(project, original)

    def test_full_setup_runs_all_stages_and_keeps_advisories_without_faking_review(self):
        original = copy.deepcopy(self.project)
        result, checked, private = self.run_pipeline()
        self.assertEqual(self.calls, ['ocr', 'translate', 'tts', 'clean', 'render'])
        self.assertEqual(result['primary_file'], 'final.mp4')
        self.assertFalse(result.get('needs_review'))
        self.assertTrue(any('chạm mép' in message for message in result['warnings']))
        self.assertTrue(any('một ký tự' in message for message in result['warnings']))
        for message in ('OCR retained every recognized word.', 'Translation warning retained.',
                        'Timing warning retained.'):
            self.assertEqual(result['warnings'].count(message), 1)
        self.assertEqual([row['text'] for row in result['segments']],
                         [row['text'] for row in self.recognized])
        self.assertNotIn('subtitle_review', private)
        self.assertEqual(self.project, original)
        self.assertGreater(checked['video_frames_decoded']['final.mp4'], 0)
        self.assertEqual(checked['audio_frames_verified']['voice.wav'],
                         round(private['media']['duration'] * 16000))
        self.assertFalse((self.folder / 'pipeline-review.json').exists())

    def assert_paused(self, coverage=None, code=None):
        result, checked, private = self.run_pipeline(coverage)
        self.assertEqual(self.calls, ['ocr'])
        self.assertTrue(result['needs_review'])
        self.assertTrue(result['output_pending'])
        self.assertTrue(result['resume_available'])
        self.assertEqual(result['stage_completed'], 'ocr')
        self.assertEqual(result['completed_steps'], ['ocr'])
        self.assertEqual(result['remaining_steps'], ['translate', 'tts', 'clean', 'render'])
        self.assertEqual(result['next_action'], 'review_subtitles_then_resume')
        self.assertNotIn('primary_file', result)
        self.assertIn(code, [issue['code'] for issue in result['review_report']['issues']])
        self.assertEqual(result['segments'], self.recognized)
        self.assertNotIn('subtitle_review', private)
        self.assertIn('OCR retained every recognized word.', result['warnings'])
        self.assertIn(result['review_checkpoint'], result['files'])
        checkpoint = self.folder / result['review_checkpoint']
        self.assertTrue(checkpoint.is_file())
        self.assertIsInstance(json.loads(checkpoint.read_text(encoding='utf-8')), dict)
        self.assertFalse((self.folder / 'final.mp4').exists())
        self.assertGreaterEqual(checked['files_checked'], 2)
        return result

    def test_partial_recognition_still_pauses_before_translation_and_keeps_saved_ocr(self):
        self.assert_paused({'analyzed_end': 4.}, 'coverage_partial')

    def test_unknown_source_coverage_still_pauses(self):
        self.assert_paused({'source_signature': 'not-the-current-source'}, 'coverage_unknown')

    def test_long_unrecognized_trailing_gap_still_pauses(self):
        self.project['media']['duration'] = 120.
        self.recognized = [{'start': 0., 'end': 2., 'text': '你好', 'translated': ''}]
        self.assert_paused(code='trailing_gap')

    def test_long_internal_gap_still_pauses(self):
        self.project['media']['duration'] = 120.
        self.recognized = [{'start': 0., 'end': 2., 'text': '你好', 'translated': ''},
                           {'start': 119., 'end': 120., 'text': '谢谢', 'translated': ''}]
        self.assert_paused(code='internal_gap')

    def test_invalid_source_timing_still_pauses(self):
        self.recognized[1]['end'] = self.project['media']['duration'] + 2
        result = self.assert_paused(code='timing')
        self.assertTrue(result['review_report']['errors'])

    def test_human_review_only_waives_matching_coverage_not_structural_errors(self):
        project = self.recognized_project()
        project['coverage']['analyzed_end'] = 4.
        project['subtitle_review'] = {'signature': review_signature(project), 'reason': 'I checked the source.'}
        self.assertFalse(inspect_subtitles(project)['requires_review'])
        project['segments'][0]['end'] = project['media']['duration'] + 2
        project['subtitle_review']['signature'] = review_signature(project)
        self.assertTrue(inspect_subtitles(project)['errors'])


if __name__ == '__main__':
    unittest.main()
