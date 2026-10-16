"""Real text-preparation/translation boundary with a synthetic provider only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import DATA, DEFAULTS, prepare_project_text
from translation_checkpoint import read_checkpoint, validate_resume
from subtitle_quality import coverage_record
import worker


def triple_project():
    sentence = '但底下却是另一个世界'
    return {'params': DEFAULTS | {'subtitle_text_cleanup': True, 'provider': 'router9',
                'translation_model': 'synthetic/text-only-model', 'target_language': 'English',
                'translation_batch': 20, 'translation_scope': 'all'},
            'media': {'path': 'synthetic-source-no-media-decoding.mp4', 'duration': 8.},
            'artifacts': {}, 'regions': [],
            'segments': [{'start': i * 2., 'end': i * 2. + 2., 'text': sentence + (' @' if i == 1 else ''),
                          'translated': ''} for i in range(3)]}


class TextPreparationTranslationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('credentials.active_secret', return_value='SYNTHETIC-NOT-A-KEY'))
        self.enterContext(patch('worker.emit'))
        self.calls = []

    def folder(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))

    def provider(self, provider, secret, model, prompt, **kwargs):
        inputs = json.loads(prompt.split('Input:\n', 1)[1])
        self.calls.append({'provider': provider, 'model': model, 'prompt': prompt, 'inputs': inputs})
        self.assertTrue(all(item['text'].strip() and '@' not in item['text'] for item in inputs))
        return json.dumps({'lines': [{'id': item['id'], 'text': 'But another world lies beneath.'}
                                    for item in inputs]})

    def test_required_triple_reaches_provider_as_one_clean_row_and_one_checkpoint(self):
        original = triple_project()
        before = copy.deepcopy(original)
        prepared, report = prepare_project_text(original, stage='source')
        self.assertEqual(original, before)
        self.assertEqual(report['source_indices'], [[0, 1, 2]])
        self.assertEqual(report['output_count'], 1)
        snapshot = copy.deepcopy(prepared)
        folder = self.folder()
        with patch('providers.generate', side_effect=self.provider) as generate:
            result = worker.translation(prepared, {}, folder)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(len(self.calls[0]['inputs']), 1)
        self.assertEqual(self.calls[0]['inputs'][0]['text'], '但底下却是另一个世界')
        self.assertEqual(self.calls[0]['inputs'][0]['seconds'], 6.)
        self.assertIn('to English', self.calls[0]['prompt'])
        self.assertEqual(len(result['segments']), 1)
        checkpoint = read_checkpoint(folder / 'translation-checkpoint.json')
        self.assertEqual(validate_resume(checkpoint, snapshot), [0])
        self.assertEqual(len(checkpoint['rows']), 1)
        self.assertEqual(checkpoint['rows'][0]['start'], 0.)
        self.assertEqual(checkpoint['rows'][0]['end'], 6.)
        self.assertEqual(checkpoint['rows'][0]['translated'], 'But another world lies beneath.')

    def test_completed_prepared_checkpoint_resumes_without_retranslation_or_remerging(self):
        prepared, _ = prepare_project_text(triple_project())
        snapshot = copy.deepcopy(prepared)
        folder = self.folder()
        with patch('providers.generate', side_effect=self.provider):
            worker.translation(prepared, {}, folder)
        with patch('providers.generate', side_effect=AssertionError('Finished cue was sent again')):
            result = worker.translation(copy.deepcopy(snapshot), {}, folder)
        self.assertTrue(result['reused'])
        self.assertEqual(result['resumed_count'], 1)
        self.assertEqual(len(result['segments']), 1)
        self.assertEqual(len(self.calls), 1)

    def test_scope_remap_cannot_send_or_rewrite_unselected_caption(self):
        original = triple_project()
        original['segments'].insert(0, {'start': 0., 'end': .5, 'text': 'Unselected @do_not_change',
                                        'translated': 'User reviewed translation.'})
        prepared, report = prepare_project_text(original, selected_indices=[1, 2, 3])
        selected = [index for index, origins in enumerate(report['source_indices'])
                    if origins and all(origin in [1, 2, 3] for origin in origins)]
        self.assertEqual(selected, [1])
        before = copy.deepcopy(prepared['segments'][0])
        with patch('providers.generate', side_effect=self.provider):
            result = worker.translation(prepared, {'segment_indices': selected}, self.folder())
        self.assertEqual([item['id'] for item in self.calls[0]['inputs']], [1])
        self.assertEqual(result['segments'][0], before)
        self.assertEqual(result['segments'][1]['text'], '但底下却是另一个世界')

    def test_credit_source_is_clean_before_api_and_remains_display_only_after_translation(self):
        original = triple_project()
        original['segments'] = [{'start': 0., 'end': 2.,
            'text': '视频来源: 当地市政府社交媒体账号 @GobiernoMunicipal', 'translated': ''}]
        prepared, report = prepare_project_text(original)
        self.assertFalse(report['needs_review'])
        self.assertIs(prepared['segments'][0]['speech_enabled'], False)
        with patch('providers.generate', side_effect=self.provider):
            result = worker.translation(prepared, {}, self.folder())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]['inputs'][0]['text'], '视频来源: 当地市政府社交媒体账号')
        self.assertIs(result['segments'][0]['speech_enabled'], False)

    def test_watermark_only_row_is_not_sent_as_empty_provider_input(self):
        original = triple_project()
        original['segments'].append({'start': 6., 'end': 7., 'text': '@onlyhandle', 'translated': ''})
        prepared, report = prepare_project_text(original)
        self.assertEqual(report['removed_indices'], [3])
        self.assertEqual(len(prepared['segments']), 1)
        with patch('providers.generate', side_effect=self.provider):
            result = worker.translation(prepared, {}, self.folder())
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0]['inputs']), 1)
        self.assertEqual(len(result['segments']), 1)

    def test_speech_project_wrapper_compares_the_explicitly_selected_source(self):
        original = triple_project()
        original['params']['tts_text_source'] = 'source'
        original['segments'] = [
            {'start': 0., 'end': 2., 'text': 'The elephant walked across the grassy plain.',
             'translated': 'Same translated caption.'},
            {'start': 2., 'end': 4., 'text': 'Buy a discounted ticket for tomorrow concert.',
             'translated': 'Same translated caption.'}]
        prepared, _ = prepare_project_text(original, stage='speech')
        self.assertEqual(prepared['segments'], original['segments'])

    def test_speech_project_wrapper_does_not_fallback_from_missing_translation(self):
        original = triple_project()
        original['params']['tts_text_source'] = 'translated'
        prepared, _ = prepare_project_text(original, stage='speech')
        self.assertEqual(prepared['segments'], original['segments'])

    def test_pipeline_prepares_recognized_triple_before_translation_and_checkpoint(self):
        original = triple_project()
        recognized = copy.deepcopy(original['segments'])
        original.update(id='synthetic-text-pipeline', name='Synthetic text pipeline',
                        source_revision='synthetic-source', updated=1.)
        original['media'].update(duration=6., width=1280, height=720, fps=25., audio=True)
        original['params'].update(pipeline_extract='ocr', subtitle_automation='manual',
            pipeline_translate=True, pipeline_tts=False, pipeline_clean=False,
            use_clean=False, voice_gain=0, burn_subtitles=False, review_before_render=True)
        original['segments'] = []
        folder = self.folder()
        def recognize(project, job, target):
            raw = {'type': 'ocr', 'files': [], 'segments': copy.deepcopy(recognized),
                   'coverage': coverage_record(project, 'ocr', 0., 6.)}
            return worker.prepare_recognized_text(project, job, target, raw)
        with patch.dict(worker.TASKS, {'ocr': recognize}), \
                patch('providers.generate', side_effect=self.provider), \
                patch('job_progress.verify_outputs', return_value={'synthetic': True}), \
                patch('stage_journal.record_stage', return_value='stage-journal.json'):
            result = worker.pipeline(copy.deepcopy(original), {'id': 'synthetic-job', 'kind': 'pipeline'}, folder)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(len(self.calls[0]['inputs']), 1)
        self.assertEqual(self.calls[0]['inputs'][0]['text'], '但底下却是另一个世界')
        self.assertEqual(result['review_stage'], 'pre_render')
        self.assertEqual(result['steps'], ['ocr', 'translate'])
        self.assertEqual(result['remaining_steps'], ['render'])
        checkpoint = read_checkpoint(folder / 'translation-checkpoint.json')
        snapshot = checkpoint['pipeline_state']['recognition_snapshot']['segments']
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]['text'], '但底下却是另一个世界')
        audit = json.loads((folder / 'text-preparation-ocr.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['source_indices'], [[0, 1, 2]])
        self.assertEqual(audit['input_count'], 3)
        self.assertEqual(audit['output_count'], 1)

    def test_protected_empty_source_fails_before_credential_or_provider_access(self):
        original = triple_project()
        original['segments'] = [{'start': 0., 'end': 2., 'text': '@onlyhandle',
                                  'translated': 'My reviewed translation.'}]
        prepared, report = prepare_project_text(original)
        self.assertTrue(report['needs_review'])
        self.assertEqual(prepared['segments'][0]['translated'], 'My reviewed translation.')
        with patch('credentials.active_secret') as credential, patch('providers.generate') as provider:
            with self.assertRaisesRegex(ValueError, 'không còn lời gốc.*'):
                worker.translation(prepared, {}, self.folder())
        credential.assert_not_called()
        provider.assert_not_called()

    def test_unselected_protected_empty_source_does_not_block_valid_selected_scope(self):
        original = triple_project()
        original['segments'] = [
            {'start': 0., 'end': 2., 'text': '@onlyhandle', 'translated': 'Reviewed target.'},
            {'start': 2., 'end': 4., 'text': '但底下却是另一个世界', 'translated': ''}]
        prepared, _ = prepare_project_text(original)
        before = copy.deepcopy(prepared['segments'][0])
        with patch('providers.generate', side_effect=self.provider):
            result = worker.translation(prepared, {'segment_indices': [1]}, self.folder())
        self.assertEqual([item['id'] for item in self.calls[0]['inputs']], [1])
        self.assertEqual(result['segments'][0], before)


if __name__ == '__main__':
    unittest.main()
