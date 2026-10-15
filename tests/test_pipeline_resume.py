"""A paused full workflow must resume its remaining work, preserving edits."""
from contextlib import ExitStack
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

from fastapi.testclient import TestClient
import core
import server
import studio_api
from pipeline_resume import continuation_status, continuation_snapshot, write_review_checkpoint
from subtitle_quality import coverage_record
from translation_checkpoint import write_checkpoint


class PipelineContinuationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='pipeline-resume-')))
        (self.data/'jobs').mkdir()
        for module in (core, server, studio_api):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        self.stack.enter_context(patch('preflight.inspect', return_value={'ok':True, 'errors':[], 'warnings':[]}))
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token':server.TOKEN}))
        source = self.data/'source.mp4'
        source.write_bytes(b'fixture source identity; no decoder or model needed')
        self.project = {'id':uuid.uuid4().hex, 'name':'Full pipeline fixture', 'updated':20., 'created':1.,
                        'source_revision':uuid.uuid4().hex,
                        'media':{'path':str(source), 'duration':10., 'width':1280, 'height':720, 'audio':True},
                        'params':core.DEFAULTS | {'pipeline_extract':'ocr', 'pipeline_translate':True,
                            'pipeline_tts':False, 'pipeline_clean':False, 'use_clean':False, 'voice_gain':0},
                        'segments':[{'start':0., 'end':5., 'text':'Original one', 'translated':''},
                                    {'start':5., 'end':10., 'text':'Original two', 'translated':''}],
                        'artifacts':{}, 'regions':[], 'extraction_regions':[]}
        self.project['coverage'] = coverage_record(self.project, 'ocr', 0, 10)
        self.row = {'id':uuid.uuid4().hex, 'project_id':self.project['id'], 'project_revision':10.,
                    'source_revision':self.project['source_revision'], 'kind':'pipeline', 'state':'review',
                    'created':3., 'project_applied':True,
                    'configuration':{'pipeline_extract':'ocr'},
                    'result':{'needs_review':True, 'steps':['ocr'], 'files':['source.srt'],
                        'segments':copy.deepcopy(self.project['segments']),
                        'coverage':copy.deepcopy(self.project['coverage'])}}
        self.folder = self.data/'jobs'/self.row['id']
        self.folder.mkdir()
        original = copy.deepcopy(self.project)
        original.update(updated=10., segments=[])
        self.original = original
        self.save_input()
        core.put('project', self.project['id'], self.project)
        core.put('job', self.row['id'], self.row)

    def save_input(self):
        (self.folder/'input.json').write_text(json.dumps({'project':self.original, 'job':self.row}), encoding='utf-8')

    def plan(self):
        return continuation_status(self.row, self.project, [self.row], self.folder)

    def test_legacy_review_can_continue_without_repeating_recognition(self):
        plan = self.plan()
        self.assertTrue(plan['can_continue'], plan)
        self.assertEqual(plan['completed_steps'], ['ocr'])
        self.assertEqual(plan['remaining_steps'], ['translate', 'render'])
        self.assertFalse(plan['requires_review'])

    def test_asr_snapshot_continues_without_repeating_asr(self):
        self.original['params']['pipeline_extract'] = 'asr'
        self.row['configuration']['pipeline_extract'] = 'asr'
        self.row['result']['steps'] = ['asr']
        self.project['params']['pipeline_extract'] = 'asr'
        self.project['coverage']['method'] = 'asr'
        self.row['result']['coverage']['method'] = 'asr'
        self.save_input()
        plan = self.plan()
        self.assertTrue(plan['can_continue'], plan)
        self.assertEqual(plan['completed_steps'], ['asr'])

    def test_changed_source_revision_and_same_path_replacement_are_rejected(self):
        self.project['source_revision'] = 'changed-source'
        self.assertFalse(self.plan()['can_continue'])
        self.project['source_revision'] = self.row['source_revision']
        Path(self.project['media']['path']).write_bytes(b'different source bytes now')
        plan = self.plan()
        self.assertFalse(plan['can_continue'])
        self.assertIn('Tệp video đã đổi', plan['reason'])

    def test_current_manual_translation_and_timing_survive_job_local_overrides(self):
        self.project['segments'][0].update(text='Source corrected', translated='Bản dịch tự sửa', end=4.8)
        self.project['params'].update(font_size=33, subtitle_flow='single', export_name='User export', voice_speed=1.2)
        original = copy.deepcopy(self.project)
        self.assertTrue(self.plan()['can_continue'])
        snapshot = continuation_snapshot(self.project)
        self.assertEqual(snapshot['segments'], self.project['segments'])
        self.assertEqual(snapshot['params']['pipeline_extract'], 'existing')
        self.assertEqual(snapshot['params']['translation_scope'], 'missing')
        for key in ('font_size', 'subtitle_flow', 'export_name', 'voice_speed'):
            self.assertEqual(snapshot['params'][key], self.project['params'][key])
        snapshot['segments'][0]['translated'] = 'isolated worker edit'
        self.assertEqual(self.project, original)

    def test_active_job_or_newer_recognition_blocks_old_continuation(self):
        for other in ({'id':'busy', 'project_id':self.project['id'], 'kind':'tts', 'state':'running'},
                      {'id':'newer', 'project_id':self.project['id'], 'kind':'ocr', 'state':'done',
                       'source_revision':self.project['source_revision'], 'created':4.}):
            with self.subTest(other=other):
                plan = continuation_status(self.row, self.project, [self.row, other], self.folder)
                self.assertFalse(plan['can_continue'])

    def test_unapplied_result_missing_rows_or_changed_snapshot_block(self):
        self.row['project_applied'] = False
        self.project['segments'][0]['text'] = 'other table'
        self.assertIn('chưa được áp dụng', self.plan()['reason'])
        self.row['project_applied'] = True
        self.row['result']['segments'] = []
        self.assertFalse(self.plan()['can_continue'])
        self.row['result']['segments'] = copy.deepcopy(self.project['segments'])
        self.original['media']['width'] = 1920
        self.save_input()
        self.assertFalse(self.plan()['can_continue'])

    def test_real_structural_error_and_partial_coverage_require_correction(self):
        self.project['segments'][0]['end'] = 0
        plan = self.plan()
        self.assertFalse(plan['can_continue'])
        self.assertTrue(plan['errors'])
        self.project['segments'][0]['end'] = 5
        self.project['coverage']['analyzed_end'] = 8
        plan = self.plan()
        self.assertFalse(plan['can_continue'])
        self.assertTrue(plan['requires_review'])
        self.assertEqual(plan['next_action'], 'review_subtitles_then_resume')

    def test_status_endpoints_are_readonly_and_detail_exposes_plan(self):
        original = copy.deepcopy(core.get('project', self.project['id']))
        for path in (f"/api/studio/projects/{self.project['id']}/pipeline-continuation",
                     f"/api/studio/jobs/{self.row['id']}/continuation"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue(response.json()['can_continue'])
        detail = self.client.get(f"/api/studio/jobs/{self.row['id']}").json()
        self.assertTrue(detail['continuation']['can_continue'])
        self.assertEqual(core.get('project', self.project['id']), original)
        self.assertEqual(len(core.all_items('job')), 1)

    def test_continue_queues_only_once_and_editor_config_stays_full_workflow(self):
        self.project['segments'][0]['translated'] = 'Manual preserved translation'
        core.put('project', self.project['id'], self.project)
        route = f"/api/studio/jobs/{self.row['id']}/continue-pipeline"
        with patch('preflight.inspect', return_value={'ok':True}), \
                patch.object(server, 'active_secret', return_value='fixture-not-real-secret'), \
                patch.object(server, 'active_key_id', return_value='fixture-key'):
            response = self.client.post(route, json={'expected_revision':20.})
            self.assertEqual(response.status_code, 200, response.text)
            queued = response.json()
            second = self.client.post(route, json={'expected_revision':20.}).json()
        self.assertEqual(queued['id'], second['id'])
        self.assertEqual(queued['continuation_from'], self.row['id'])
        self.assertEqual(queued['configuration']['pipeline_extract'], 'existing')
        spec = json.loads((self.data/'jobs'/queued['id']/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(spec['project']['segments'][0]['translated'], 'Manual preserved translation')
        self.assertEqual(spec['project']['params']['translation_scope'], 'missing')
        self.assertEqual(core.get('project', self.project['id']), self.project)
        self.assertEqual(len(core.all_items('job')), 2)

    def test_expected_revision_and_review_block_without_any_new_job(self):
        route = f"/api/studio/jobs/{self.row['id']}/continue-pipeline"
        self.assertEqual(self.client.post(route, json={'expected_revision':19.}).status_code, 409)
        self.project['coverage']['analyzed_end'] = 8
        core.put('project', self.project['id'], self.project)
        self.assertEqual(self.client.post(route, json={'expected_revision':20.}).status_code, 400)
        self.assertEqual(len(core.all_items('job')), 1)
        self.assertNotIn('subtitle_review', core.get('project', self.project['id']))

    def test_preflight_error_is_reported_without_queue_or_project_mutation(self):
        route = f"/api/studio/jobs/{self.row['id']}/continue-pipeline"
        with patch('preflight.inspect', return_value={'ok':False, 'errors':['Fixture: missing voice']}):
            response = self.client.post(route, json={'expected_revision':20.})
        self.assertEqual(response.status_code, 400)
        self.assertIn('missing voice', response.json()['detail'])
        self.assertEqual(len(core.all_items('job')), 1)
        self.assertEqual(core.get('project', self.project['id']), self.project)

    def test_request_id_bound_to_other_pipeline_never_returns_wrong_job(self):
        existing = {'id':'other-pipeline', 'kind':'pipeline', 'project_id':self.project['id'], 'state':'error', 'created':1.}
        core.put('job', existing['id'], existing)
        core.put('job_request', 'already-used-request', {'job_id':existing['id']})
        response = self.client.post(f"/api/studio/jobs/{self.row['id']}/continue-pipeline",
                                    json={'expected_revision':20., 'request_id':'already-used-request'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(core.all_items('job')), 2)

    def test_readonly_plan_checks_remaining_engines_with_existing_extraction(self):
        def inspect(snapshot, kind, *, check_credentials=True):
            self.assertFalse(check_credentials, 'Inspecting continuation must not read Windows credentials.')
            self.assertEqual(kind, 'pipeline')
            self.assertEqual(snapshot['params']['pipeline_extract'], 'existing')
            self.assertEqual(snapshot['params']['translation_scope'], 'missing')
            return {'ok':False, 'errors':['Fixture: missing key'], 'warnings':['Voice advisory']}
        with patch('preflight.inspect', side_effect=inspect):
            plan = self.plan()
        self.assertFalse(plan['can_continue'])
        self.assertEqual(plan['next_action'], 'fix_configuration')
        self.assertIn('Fixture: missing key', plan['errors'])
        self.assertIn('Voice advisory', plan['warnings'])
        self.assertEqual(self.project['params']['pipeline_extract'], 'ocr')

    def test_completed_successor_removes_paused_card_and_never_queues_again(self):
        successor = {'id':'completed-successor', 'project_id':self.project['id'], 'kind':'pipeline', 'state':'done',
                     'source_revision':self.project['source_revision'], 'continuation_from':self.row['id'],
                     'created':30., 'result':{'primary_file':'final.mp4', 'files':['final.mp4']}}
        core.put('job', successor['id'], successor)
        response = self.client.get(f"/api/studio/projects/{self.project['id']}/pipeline-continuation")
        self.assertIsNone(response.json()['job_id'])
        response = self.client.post(f"/api/studio/jobs/{self.row['id']}/continue-pipeline", json={'expected_revision':20.})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(core.all_items('job')), 2)

    def failed_child(self, completed=(0,), selected=None):
        child = {'id':uuid.uuid4().hex, 'kind':'pipeline', 'project_id':self.project['id'],
                 'project_revision':20., 'source_revision':self.project['source_revision'],
                 'continuation_from':self.row['id'], 'state':'error', 'created':30.,
                 'message':'HTTP 429 fixture; provider quota', 'configuration':{'pipeline_extract':'existing'}}
        private = continuation_snapshot(self.project)
        if selected is not None:
            child['segment_indices'] = list(selected)
        folder = self.data/'jobs'/child['id'];folder.mkdir()
        (folder/'input.json').write_text(json.dumps({'project':private, 'job':child}), encoding='utf-8')
        translated = copy.deepcopy(private['segments'])
        for index in completed:
            translated[index]['translated'] = 'Checkpoint translation ' + str(index)
        write_checkpoint(folder/'translation-checkpoint.json', private, translated, set(completed), selected)
        # This semantic-recovery fixture has a measured request history. The
        # separate admission tests cover legacy history which is unknown.
        from provider_usage import TranslationUsage
        usage = TranslationUsage(folder, len(private['segments']), 20,
                                 private['params']['provider'], private['params']['translation_model'])
        usage.set_batch(list(completed) or [0], 'fixture only')
        usage.observe({'event':'before_send', 'attempt':1})
        usage.observe({'event':'response', 'http_status':200 if completed else 503})
        usage.checkpoint(len(completed))
        core.put('job', child['id'], child)
        return child, folder

    def test_partial_failed_successor_plan_recovers_batches_without_writing_editor(self):
        child, folder = self.failed_child()
        original = copy.deepcopy(self.project)
        for path in (f"/api/studio/projects/{self.project['id']}/pipeline-continuation",
                     f"/api/studio/jobs/{self.row['id']}/continuation",
                     f"/api/studio/jobs/{child['id']}/continuation"):
            plan = self.client.get(path).json()
            self.assertTrue(plan['can_continue'], plan)
            self.assertEqual(plan['recovery_job_id'], child['id'])
            self.assertEqual(plan['reused_translation_count'], 1)
            self.assertEqual(plan['missing_translation_count'], 1)
            self.assertIn('429', plan['last_error'])
        self.assertTrue(self.client.get(f"/api/studio/jobs/{child['id']}").json()['can_resume'])
        self.assertEqual(core.get('project', self.project['id']), original)

    def test_original_continue_uses_failed_child_completed_translation_once(self):
        child, folder = self.failed_child()
        with patch.object(server, 'active_secret', return_value='test-not-real'), \
                patch.object(server, 'active_key_id', return_value='fixture'):
            response = self.client.post(f"/api/studio/jobs/{self.row['id']}/continue-pipeline", json={'expected_revision':20.})
        self.assertEqual(response.status_code, 200, response.text)
        queued = response.json()
        self.assertEqual(queued['continuation_from'], self.row['id'])
        self.assertEqual(queued['recovery_from'], child['id'])
        snapshot = json.loads((self.data/'jobs'/queued['id']/'input.json').read_text(encoding='utf-8'))['project']
        self.assertEqual(snapshot['params']['pipeline_extract'], 'existing')
        self.assertEqual(snapshot['params']['translation_scope'], 'missing')
        self.assertEqual(snapshot['segments'][0]['translated'], 'Checkpoint translation 0')
        self.assertEqual(snapshot['segments'][1]['translated'], '')
        self.assertEqual(core.get('project', self.project['id']), self.project)

    def test_already_applied_360_translations_are_reused_not_zero_or_double_counted(self):
        self.project['segments'] = [
            {'start':index * 10 / 422, 'end':(index + 1) * 10 / 422,
             'text':f'Original cue {index}', 'translated':''} for index in range(422)]
        self.row['result']['segments'] = copy.deepcopy(self.project['segments'])
        core.put('job', self.row['id'], self.row)
        first, _ = self.failed_child(completed=range(360))
        for index in range(360):
            self.project['segments'][index]['translated'] = f'Checkpoint translation {index}'
        self.project['segments'][12]['translated'] = 'A newer manual correction must survive'
        core.put('project', self.project['id'], self.project)
        original = copy.deepcopy(self.project)
        first_plan = self.client.get(f"/api/studio/jobs/{first['id']}/continuation").json()
        self.assertTrue(first_plan['can_continue'], first_plan)
        self.assertEqual(first_plan['reused_translation_count'], 360)
        self.assertEqual(first_plan['restored_translation_count'], 0)
        self.assertEqual(first_plan['missing_translation_count'], 62)
        self.assertIn('Dùng lại 360 câu', first_plan['reason'])

        # Retry two's input and its empty checkpoint both contain the 360
        # retained rows; neither is a new batch or a second copy of each cue.
        second, _ = self.failed_child(completed=())
        second['created'] = 40.
        core.put('job', second['id'], second)
        second_plan = self.client.get(f"/api/studio/jobs/{second['id']}/continuation").json()
        self.assertTrue(second_plan['can_continue'], second_plan)
        self.assertEqual(second_plan['reused_translation_count'], 360)
        self.assertEqual(second_plan['restored_translation_count'], 0)
        self.assertEqual(second_plan['missing_translation_count'], 62)
        self.assertEqual(core.get('project', self.project['id']), original)

    def test_reused_count_is_scoped_and_preserves_manual_translation_over_checkpoint(self):
        self.project['segments'] = [
            {'start':index * 2.5, 'end':(index + 1) * 2.5,
             'text':f'Original cue {index}', 'translated':''} for index in range(4)]
        self.project['segments'][0]['translated'] = 'Translated outside selection'
        self.project['segments'][3]['translated'] = 'Another translation outside selection'
        self.row['result']['segments'] = copy.deepcopy(self.project['segments'])
        core.put('job', self.row['id'], self.row)
        child, _ = self.failed_child(completed=(1,), selected=(1, 2))
        self.project['segments'][1]['translated'] = 'Manual replacement of checkpoint translation'
        core.put('project', self.project['id'], self.project)
        original = copy.deepcopy(self.project)
        plan = self.client.get(f"/api/studio/jobs/{child['id']}/continuation").json()
        self.assertTrue(plan['can_continue'], plan)
        self.assertEqual(plan['reused_translation_count'], 1)
        self.assertEqual(plan['restored_translation_count'], 0)
        self.assertEqual(plan['missing_translation_count'], 1)
        self.assertIn('Dùng lại 1 câu', plan['reason'])
        self.assertEqual(core.get('project', self.project['id']), original)

    def test_generic_resume_from_existing_child_avoids_editor_ocr_validator(self):
        child, folder = self.failed_child(completed=(0, 1))
        self.project['segments'][0]['translated'] = 'Manual newer translation'
        core.put('project', self.project['id'], self.project)
        with patch('server.active_secret', side_effect=AssertionError('Complete translations must not need key')):
            response = self.client.post('/api/jobs', json={'kind':'pipeline', 'project_id':self.project['id'],
                                                          'expected_revision':20., 'replace_segments':True,
                                                          'resume_from':child['id']})
        self.assertEqual(response.status_code, 200, response.text)
        queued = response.json()
        snapshot = json.loads((self.data/'jobs'/queued['id']/'input.json').read_text(encoding='utf-8'))['project']
        self.assertEqual(snapshot['params']['pipeline_extract'], 'existing')
        self.assertFalse(snapshot['params']['pipeline_translate'])
        self.assertEqual(snapshot['segments'][0]['translated'], 'Manual newer translation')
        self.assertEqual(snapshot['segments'][1]['translated'], 'Checkpoint translation 1')
        self.assertEqual(queued['recovery_from'], child['id'])
        self.assertNotIn('resume_from', queued)
        self.assertFalse((self.data/'jobs'/queued['id']/'translation-checkpoint.json').exists())
        self.assertEqual(core.get('project', self.project['id']), self.project)

    def test_second_interruption_keeps_prior_recovered_batches_and_latest_child(self):
        first, _ = self.failed_child()
        with patch.object(server, 'active_secret', return_value='test-not-real'), \
                patch.object(server, 'active_key_id', return_value='fixture'):
            queued = self.client.post(f"/api/studio/jobs/{first['id']}/continue-pipeline", json={'expected_revision':20.}).json()
        folder = self.data/'jobs'/queued['id']
        spec = json.loads((folder/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(spec['project']['segments'][0]['translated'], 'Checkpoint translation 0')
        translated = copy.deepcopy(spec['project']['segments'])
        translated[1]['translated'] = 'Second batch translation'
        write_checkpoint(folder/'translation-checkpoint.json', spec['project'], translated, {1})
        queued.update(state='error', message='Later step interrupted')
        core.put('job', queued['id'], queued)
        for identifier in (first['id'], queued['id'], self.row['id']):
            plan = self.client.get(f"/api/studio/jobs/{identifier}/continuation").json()
            self.assertTrue(plan['can_continue'], plan)
            self.assertEqual(plan['recovery_job_id'], queued['id'])
            self.assertEqual(plan['reused_translation_count'], 2)
            self.assertEqual(plan['missing_translation_count'], 0)
        with patch('server.active_secret', side_effect=AssertionError('No completed batch may be redone')):
            response = self.client.post('/api/jobs', json={'kind':'pipeline', 'project_id':self.project['id'],
                                                          'expected_revision':20., 'replace_segments':True,
                                                          'resume_from':first['id']})
        self.assertEqual(response.status_code, 200, response.text)
        new = response.json()
        self.assertEqual(new['recovery_from'], queued['id'])
        private = json.loads((self.data/'jobs'/new['id']/'input.json').read_text(encoding='utf-8'))['project']
        self.assertEqual([r['translated'] for r in private['segments']], ['Checkpoint translation 0', 'Second batch translation'])
        self.assertFalse(private['params']['pipeline_translate'])
        self.assertEqual(core.get('project', self.project['id']), self.project)

    def test_selected_recovery_does_not_expand_or_remap_after_source_edits(self):
        child, _ = self.failed_child(completed=(0,), selected=(0,))
        self.project['segments'].reverse()
        core.put('project', self.project['id'], self.project)
        plan = self.client.get(f"/api/studio/jobs/{child['id']}/continuation").json()
        self.assertFalse(plan['can_continue'])
        self.assertIn('Phạm vi dịch', plan['reason'])

    def test_corrupt_or_changed_target_checkpoint_never_creates_successor(self):
        child, folder = self.failed_child()
        self.project['params']['target_language'] = 'Another language'
        core.put('project', self.project['id'], self.project)
        plan = self.client.get(f"/api/studio/jobs/{child['id']}/continuation").json()
        self.assertFalse(plan['can_continue'])
        self.assertIn('ngôn ngữ', plan['reason'])
        self.project['params']['target_language'] = core.DEFAULTS['target_language']
        core.put('project', self.project['id'], self.project)
        document = json.loads((folder/'translation-checkpoint.json').read_text(encoding='utf-8'))
        document['signature'] = 'tampered'
        (folder/'translation-checkpoint.json').write_text(json.dumps(document), encoding='utf-8')
        response = self.client.post(f"/api/studio/jobs/{child['id']}/continue-pipeline", json={'expected_revision':20.})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(len(core.all_items('job')), 2)

    def test_newer_independent_final_hides_old_paused_workflow(self):
        final = {'id':'independent-final', 'project_id':self.project['id'], 'kind':'pipeline', 'state':'done',
                 'source_revision':self.project['source_revision'], 'created':30.,
                 'result':{'primary_file':'final.mp4', 'files':['final.mp4']}}
        core.put('job', final['id'], final)
        plan = self.client.get(f"/api/studio/projects/{self.project['id']}/pipeline-continuation").json()
        self.assertIsNone(plan['job_id'])

    def test_review_checkpoint_preserves_invalid_cue_for_manual_fix(self):
        self.project['segments'][0]['end'] = 0
        filename = write_review_checkpoint(self.original, self.project, self.row, self.folder,
                                           self.row['result'] | {'completed_steps':['ocr'], 'remaining_steps':['translate','render']})
        document = json.loads((self.folder/filename).read_text(encoding='utf-8'))
        self.assertEqual(document['rows'][0]['end'], 0)
        self.assertEqual(document['completed_steps'], ['ocr'])
        self.assertEqual(document['source_revision'], self.project['source_revision'])

    def test_final_folder_opens_published_video_parent_and_review_has_no_final(self):
        route = f"/api/studio/jobs/{self.row['id']}/open-result-folder"
        with patch.object(studio_api.os, 'startfile') as opened:
            self.assertEqual(self.client.post(route).status_code, 400)
            opened.assert_not_called()
            (self.folder/'final.mp4').write_bytes(b'verified fixture final')
            exports = self.data/'human-exports'; exports.mkdir()
            published = exports/'final.mp4'; published.write_bytes(b'published fixture final')
            self.row.update(state='done', result={'primary_file':'final.mp4', 'files':['final.mp4'],
                                                 'published':{'path':str(published)}})
            core.put('job', self.row['id'], self.row)
            # A published copy remains usable after the technical copy is moved.
            (self.folder/'final.mp4').unlink()
            response = self.client.post(route)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(Path(response.json()['directory']), exports.resolve())
            self.assertEqual(Path(response.json()['path']), published.resolve())
            opened.assert_called_once_with(str(exports.resolve()))


if __name__ == '__main__':
    unittest.main()
