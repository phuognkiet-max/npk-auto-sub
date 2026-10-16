"""Text preparation and a human export boundary across real worker control flow."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import core
import worker
from pipeline_resume import pre_render_signature, pre_render_review_status
from subtitle_quality import coverage_record


class PhaseOnePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='npk-phase1-')
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        source = self.folder/'source.mp4'
        source.write_bytes(b'private synthetic source, no decoder needed')
        self.project = {'id': uuid.uuid4().hex, 'updated': 1., 'source_revision': uuid.uuid4().hex,
            'media': {'path': str(source), 'duration': 12., 'fps': 30., 'width': 1280,
                      'height': 720, 'audio': True},
            'params': core.DEFAULTS | {'subtitle_text_cleanup': True, 'review_before_render': True,
                'pipeline_extract': 'existing', 'pipeline_translate': False, 'pipeline_tts': False,
                'pipeline_clean': False, 'use_clean': False, 'voice_gain': 0,
                'subtitle_automation': 'manual'},
            'segments': [{'id': 'a', 'start': 0., 'end': 6., 'text': 'A complete source sentence.',
                          'translated': 'Một câu nguồn hoàn chỉnh.', 'custom': {'keep': True}},
                         {'id': 'b', 'start': 6., 'end': 12., 'text': 'Another complete sentence.',
                          'translated': 'Một câu hoàn chỉnh khác.'}],
            'artifacts': {}, 'regions': [], 'extraction_regions': []}
        self.project['coverage'] = coverage_record(self.project, 'import', 0, 12)
        self.job = {'id': uuid.uuid4().hex, 'project_id': self.project['id'], 'kind': 'pipeline',
                    'source_revision': self.project['source_revision']}

    def test_selected_ocr_triplet_prepared_before_translation_without_losing_raw_evidence(self):
        rows = [{'start': i*2., 'end': (i+1)*2., 'text': text, 'translated': ''}
                for i, text in enumerate(['但底下却是另一个世界', '但底下却是另一个世界 @', '但底下却是另一个世界'])]
        core.save_srt(rows, self.folder/'source.srt', translated=False)
        raw = (self.folder/'source.srt').read_bytes()
        result = worker.prepare_recognized_text(self.project, self.job, self.folder,
            {'type': 'ocr', 'segments': rows, 'files': ['source.srt']})
        self.assertEqual(len(result['segments']), 1)
        self.assertEqual(result['segments'][0]['text'], '但底下却是另一个世界')
        self.assertEqual((result['segments'][0]['start'], result['segments'][0]['end']), (0., 6.))
        self.assertEqual((self.folder/'source.srt').read_bytes(), raw)
        self.assertEqual(len(rows), 3)
        report = json.loads((self.folder/'text-preparation-ocr.json').read_text(encoding='utf-8'))
        self.assertEqual(report['source_indices'], [[0, 1, 2]])
        self.assertTrue(report['changes'])
        again = worker.prepare_recognized_text(self.project, self.job, self.folder, result)
        self.assertEqual(again['segments'], result['segments'])

    def test_ambiguous_semantic_selection_requires_review_and_is_not_reinterpreted(self):
        result = {'type': 'auto_subtitles', 'segments': [{'start': 0, 'end': 2, 'text': 'Caption @'}],
                  'files': [], 'needs_review': True}
        self.assertEqual(worker.prepare_recognized_text(self.project, self.job, self.folder, result), result)
        self.assertFalse((self.folder/'text-preparation-auto_subtitles.json').exists())

    def test_real_pipeline_pauses_before_encoder_and_review_output_verifies_without_final(self):
        from job_progress import verify_outputs
        with patch.dict(worker.TASKS, {'render': unittest.mock.Mock(side_effect=AssertionError('no encoder'))}), \
             patch('providers.generate', side_effect=AssertionError('no provider')):
            result = worker.pipeline(self.project, self.job, self.folder)
        self.assertTrue(result['needs_review'])
        self.assertEqual(result['review_stage'], 'pre_render')
        self.assertEqual(result['remaining_steps'], ['render'])
        self.assertNotIn('primary_file', result)
        self.assertFalse((self.folder/'final.mp4').exists())
        self.assertEqual(result['segments'], self.project['segments'])
        saved = json.loads((self.folder/'pipeline-review.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['prepared_project']['segments'], self.project['segments'])
        self.assertEqual(saved['prepared_signature'], pre_render_signature(self.project))
        checked = verify_outputs(result, self.project, self.job, self.folder)
        self.assertEqual(checked['files_checked'], 1)
        self.assertFalse(pre_render_review_status(self.project)['approved'])

    def test_explicit_approved_export_only_consumes_reviewed_table(self):
        self.project['pre_render_review'] = {'schema_version': 1, 'approved': True,
            'signature': pre_render_signature(self.project), 'job_id': self.job['id']}
        before = copy.deepcopy(self.project['segments'])
        render = unittest.mock.Mock(return_value={'type': 'render', 'files': []})
        with patch.dict(worker.TASKS, {'render': render,
                'translate': unittest.mock.Mock(side_effect=AssertionError('no translation')),
                'tts': unittest.mock.Mock(side_effect=AssertionError('no synthesis'))}), \
             patch('job_progress.verify_outputs', return_value={'files_checked': 0}), \
             patch('stage_journal.record_stage', return_value='stage-journal.json'):
            result = worker.pipeline(self.project, self.job, self.folder)
        render.assert_called_once()
        self.assertEqual(render.call_args.args[0]['segments'], before)
        self.assertEqual(result['steps'], ['render'])
        self.assertEqual(result['primary_file'], 'final.mp4')
        self.assertNotIn('needs_review', result)

    def test_direct_render_and_changed_approved_contents_fail_before_encoder(self):
        with patch('worker.binary', side_effect=AssertionError('encoder must not be prepared')):
            with self.assertRaisesRegex(ValueError, 'duyệt'):
                worker.render(self.project, self.job, self.folder)
            self.project['pre_render_review'] = {'schema_version': 1, 'approved': True,
                'signature': pre_render_signature(self.project)}
            self.project['segments'][0]['translated'] = 'Nội dung đã sửa sau khi duyệt.'
            with self.assertRaisesRegex(ValueError, 'duyệt'):
                worker.render(self.project, self.job, self.folder)

    def test_recovered_voice_presents_derived_roles_before_review_without_recreating_audio(self):
        self.project['params']['pipeline_tts'] = True
        self.project['segments'] = [dict(start=i*2., end=(i+1)*2.,
            text='An identical source caption.', translated='Cùng một bản dịch đã lưu.') for i in range(3)]
        self.project['artifacts']['tts'] = {'job_id': uuid.uuid4().hex, 'speech_signature': 'anchored'}
        before = copy.deepcopy(self.project['segments'])
        with patch('subtitle_quality.speech_ready', return_value=True), \
             patch('stage_journal.record_reuse', return_value=None), \
             patch.dict(worker.TASKS, {'tts': unittest.mock.Mock(side_effect=AssertionError('no resynthesis'))}):
            result = worker.pipeline(self.project, self.job, self.folder)
        self.assertEqual(result['reused'], ['tts'])
        self.assertEqual(result['review_stage'], 'pre_render')
        self.assertEqual([r.get('speech_enabled', True) for r in result['segments']], [True, False, False])
        for old, new in zip(before, result['segments']):
            self.assertEqual([new[k] for k in ('start', 'end', 'text', 'translated')],
                             [old[k] for k in ('start', 'end', 'text', 'translated')])
        self.assertIn('text-preparation-reused-speech.json', result['files'])


if __name__ == '__main__':
    unittest.main()
