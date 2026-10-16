"""A saved human review is a content-bound prerequisite for final rendering.

These tests use private synthetic projects; no decoder, provider, model or
credential vault is needed for reviewing or queuing an offline export.
"""
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
from pipeline_resume import (pre_render_signature, pre_render_review_status,
    require_pre_render_approval, write_review_checkpoint, continuation_status,
    pre_render_continuation_snapshot, prepare_continuation)
from subtitle_quality import coverage_record


class PreRenderReviewTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='npk-pre-render-')))
        (self.data / 'jobs').mkdir()
        for module in (core, server, studio_api):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        self.stack.enter_context(patch('preflight.inspect', return_value={'ok': True, 'errors': [], 'warnings': []}))
        self.provider = self.stack.enter_context(patch('providers.generate', side_effect=AssertionError('no provider')))
        self.vault = self.stack.enter_context(patch('credentials.active_secret', side_effect=AssertionError('no vault')))
        self.load = self.stack.enter_context(patch('voices.load_voice', side_effect=AssertionError('no inference')))
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token': server.TOKEN}))
        source = self.data / 'source.mp4'
        source.write_bytes(b'synthetic source identity, deliberately not a media file')
        self.project = {'id': uuid.uuid4().hex, 'name': 'Generic multilingual review fixture',
            'updated': 20., 'created': 1., 'source_revision': uuid.uuid4().hex,
            'media': {'path': str(source), 'duration': 10., 'width': 1280, 'height': 720,
                      'fps': 30., 'audio': True},
            'params': core.DEFAULTS | {'review_before_render': True, 'pipeline_extract': 'existing',
                'pipeline_translate': False, 'pipeline_tts': False, 'pipeline_clean': False,
                'use_clean': False, 'voice_gain': 0, 'subtitle_automation': 'manual'},
            'segments': [{'start': 0., 'end': 5., 'text': 'A complete source sentence.',
                          'translated': 'Một câu đã dịch hoàn chỉnh.'},
                         {'start': 5., 'end': 10., 'text': 'Another complete source sentence.',
                          'translated': 'Một câu khác đã dịch hoàn chỉnh.'}],
            'artifacts': {}, 'regions': [], 'extraction_regions': []}
        self.project['coverage'] = coverage_record(self.project, 'import', 0, 10)
        self.row = {'id': uuid.uuid4().hex, 'project_id': self.project['id'], 'project_revision': 10.,
            'source_revision': self.project['source_revision'], 'kind': 'pipeline', 'state': 'review',
            'created': 5., 'project_applied': True, 'configuration': {'pipeline_extract': 'existing'},
            'result': {'type': 'pipeline', 'needs_review': True, 'review_stage': 'pre_render',
                'files': ['pipeline-review.json'], 'completed_steps': ['translate'],
                'remaining_steps': ['render'], 'segments': copy.deepcopy(self.project['segments']),
                'pre_render_signature': pre_render_signature(self.project)}}
        self.folder = self.data / 'jobs' / self.row['id']
        self.folder.mkdir()
        (self.folder / 'input.json').write_text(json.dumps({'project': self.project, 'job': self.row}), encoding='utf-8')
        write_review_checkpoint(self.project, self.project, self.row, self.folder, self.row['result'])
        core.put('project', self.project['id'], self.project)
        core.put('job', self.row['id'], self.row)
        self.endpoint = '/api/studio/projects/' + self.project['id'] + '/pre-render-review'

    def plan(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def approve(self, **overrides):
        plan = self.plan()
        body = {key: plan[key] for key in ('job_id', 'signature', 'expected_revision')}
        body['approved'] = True
        body.update(overrides)
        return self.client.post(self.endpoint, json=body)

    def test_plan_is_read_only_and_explicit_approval_is_required(self):
        before = core.get('project', self.project['id'])
        plan = self.plan()
        self.assertTrue(plan['can_approve'], plan)
        self.assertFalse(plan['can_continue'])
        self.assertTrue(plan['requires_review'])
        self.assertEqual(plan['job_id'], self.row['id'])
        self.assertEqual(plan['remaining_steps'], ['render'])
        self.assertEqual(core.get('project', self.project['id']), before)
        self.assertEqual(len(core.all_items('job')), 1)
        with self.assertRaisesRegex(ValueError, 'duyệt'):
            require_pre_render_approval(self.project)
        self.provider.assert_not_called(); self.vault.assert_not_called(); self.load.assert_not_called()

    def test_approval_records_decision_and_never_queues(self):
        response = self.approve()
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertTrue(result['approved'])
        self.assertTrue(result['can_continue'])
        self.assertGreater(result['expected_revision'], 20.)
        self.assertEqual(result['project']['pre_render_review']['job_id'], self.row['id'])
        self.assertEqual(len(core.all_items('job')), 1)
        require_pre_render_approval(result['project'])
        self.provider.assert_not_called(); self.vault.assert_not_called(); self.load.assert_not_called()

    def test_continue_is_blocked_until_approval_then_only_render_is_queued(self):
        route = '/api/studio/jobs/' + self.row['id'] + '/continue-pipeline'
        blocked = self.client.post(route, json={'expected_revision': 20.})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        response = self.approve()
        self.assertEqual(response.status_code, 200, response.text)
        saved = response.json()['project']
        accepted = self.client.post(route, json={'expected_revision': saved['updated']})
        self.assertEqual(accepted.status_code, 200, accepted.text)
        job = accepted.json()
        again = self.client.post(route, json={'expected_revision': saved['updated']})
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()['id'], job['id'])
        packet = json.loads((self.data/'jobs'/job['id']/'input.json').read_text(encoding='utf-8'))
        p = packet['project']['params']
        self.assertEqual(p['pipeline_extract'], 'existing')
        self.assertTrue(all(p[k] is False for k in ('pipeline_translate', 'pipeline_tts', 'pipeline_clean')))
        self.assertNotIn('credential_ref', job)
        self.assertNotIn('api_usage_lineage', job)
        self.assertEqual(packet['project']['segments'], saved['segments'])
        self.assertEqual(packet['project']['pre_render_review'], saved['pre_render_review'])
        self.assertEqual(core.get('project', saved['id']), saved)

    def test_direct_final_render_is_gated_and_preview_is_unaffected(self):
        blocked = self.client.post('/api/jobs', json={'kind': 'render', 'project_id': self.project['id'],
                                                     'expected_revision': 20.})
        self.assertEqual(blocked.status_code, 400, blocked.text)
        preview = self.client.post('/api/jobs', json={'kind': 'subtitle_preview',
            'project_id': self.project['id'], 'expected_revision': 20.})
        self.assertEqual(preview.status_code, 200, preview.text)

    def test_automatic_missing_and_stale_approval_are_rejected(self):
        for values in ({'approved': False}, {'automatic': True}, {'expected_revision': 19.},
                       {'signature': '0'*64}, {'job_id': uuid.uuid4().hex}):
            with self.subTest(values=values):
                response = self.approve(**values)
                self.assertIn(response.status_code, (400, 409), response.text)
                self.assertNotIn('pre_render_review', core.get('project', self.project['id']))

    def test_numeric_form_roundtrip_keeps_review_but_changed_values_do_not(self):
        approved = self.approve().json()['project']
        original = pre_render_signature(approved)
        def as_float(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, int):
                return float(value)
            if isinstance(value, dict):
                return {key: as_float(item) for key, item in value.items()}
            if isinstance(value, list):
                return [as_float(item) for item in value]
            return value
        roundtrip = as_float(approved)
        self.assertEqual(pre_render_signature(roundtrip), original)
        self.assertTrue(pre_render_review_status(roundtrip)['approved'])
        self.assertEqual(approved['params']['review_before_render'], True)
        roundtrip['params']['voice_gain'] = .01
        self.assertNotEqual(pre_render_signature(roundtrip), original)

    def test_text_timing_voice_mix_style_clean_and_provider_changes_invalidate(self):
        approved = self.approve().json()['project']
        original = pre_render_signature(approved)
        mutations = [lambda p: p['segments'][0].update(translated='Nội dung đã sửa'),
                     lambda p: p['segments'][0].update(end=4.9),
                     lambda p: p['segments'][0].update(speech_enabled=False),
                     lambda p: p['params'].update(voice_id='piper:another-voice'),
                     lambda p: p['params'].update(original_gain=0),
                     lambda p: p['params'].update(font_size=60),
                     lambda p: p['params'].update(translation_model='another-model'),
                     lambda p: p['regions'].append({'x': 1, 'y': 1, 'w': 20, 'h': 20, 'start': 0, 'end': 10})]
        for mutate in mutations:
            changed = copy.deepcopy(approved)
            mutate(changed)
            with self.subTest(changed=changed['params']['voice_id']):
                self.assertNotEqual(pre_render_signature(changed), original)
                self.assertFalse(pre_render_review_status(changed)['approved'])
                with self.assertRaises(ValueError):
                    require_pre_render_approval(changed)

    def test_render_only_snapshot_and_revision_change_retain_same_signature(self):
        approved = self.approve().json()['project']
        snapshot = pre_render_continuation_snapshot(approved)
        snapshot['updated'] += 1
        snapshot['name'] = 'Renamed project'
        self.assertEqual(pre_render_signature(snapshot), pre_render_signature(approved))
        require_pre_render_approval(snapshot)

    def test_source_same_path_replacement_and_missing_checkpoint_fail_closed(self):
        Path(self.project['media']['path']).write_bytes(b'same path with other contents')
        plan = self.plan()
        self.assertFalse(plan['can_approve'])
        self.assertFalse(plan['can_continue'])
        (self.folder/'pipeline-review.json').unlink()
        self.assertFalse(self.plan()['can_approve'])

    def test_invalid_checkpoint_and_unknown_approval_fields_do_not_bypass(self):
        checkpoint = self.folder/'pipeline-review.json'
        document = json.loads(checkpoint.read_text(encoding='utf-8'))
        document['prepared_project']['segments'][0]['translated'] = 'Injected replacement'
        checkpoint.write_text(json.dumps(document), encoding='utf-8')
        self.assertFalse(self.plan()['can_approve'])
        self.assertEqual(self.approve().status_code, 409)
        response = self.client.put('/api/projects/' + self.project['id'], json={
            'expected_revision': 20., 'pre_render_review': {'signature': pre_render_signature(self.project),
                                                         'approved': True, 'schema_version': 1}})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('pre_render_review', response.json())

    def test_busy_project_and_failed_preflight_block_approval(self):
        busy = {'id': uuid.uuid4().hex, 'project_id': self.project['id'], 'kind': 'tts', 'state': 'running'}
        core.put('job', busy['id'], busy)
        self.assertFalse(self.plan()['can_approve'])
        busy['state'] = 'cancelled'; core.put('job', busy['id'], busy)
        with patch('preflight.inspect', return_value={'ok': False, 'errors': ['Synthetic invalid config'], 'warnings': []}):
            self.assertFalse(self.plan()['can_approve'])
            self.assertEqual(self.approve().status_code, 409)

    def test_legacy_project_with_missing_flag_can_render_without_new_review(self):
        legacy = copy.deepcopy(self.project)
        legacy['params'].pop('review_before_render')
        require_pre_render_approval(legacy)
        self.assertFalse(pre_render_review_status(legacy)['requires_review'])

    def test_attaching_pause_applies_effective_clean_choice_without_autoapproval(self):
        job = self.row | {'project_revision': 20.}
        result = self.row['result'] | {'prepared_params': {'use_clean': True}}
        self.assertTrue(server.attach_completed_result(job, result))
        saved = core.get('project', self.project['id'])
        self.assertTrue(saved['params']['use_clean'])
        self.assertNotIn('pre_render_review', saved)

    def test_approval_binds_actual_verified_audio_not_only_text_signature(self):
        from stage_journal import describe_file
        from worker import speech_signature
        project = copy.deepcopy(self.project)
        project['params']['voice_gain'] = 1.
        owner = self.data/'jobs'/uuid.uuid4().hex
        owner.mkdir()
        clip = owner/'voice.wav'
        clip.write_bytes(b'synthetic voice payload A, not an inference model')
        project['artifacts']['tts'] = {'job_id': owner.name, 'files': ['voice.wav'],
            'speech_signature': speech_signature(project),
            'verification': {'file_integrity': [describe_file(owner, 'voice.wav')]}}
        project['pre_render_review'] = {'schema_version': 1, 'approved': True,
                                       'signature': pre_render_signature(project), 'job_id': self.row['id']}
        require_pre_render_approval(project)
        # A changed payload cannot inherit approval even if the replacement
        # has its own valid verification and the synthesized words are equal.
        clip.write_bytes(b'synthetic voice payload B with different bytes')
        with self.assertRaises(ValueError):
            require_pre_render_approval(project)
        project['artifacts']['tts']['verification']['file_integrity'] = [describe_file(owner, 'voice.wav')]
        self.assertFalse(pre_render_review_status(project)['approved'])

    def test_new_translation_freezes_clean_corpus_and_keeps_an_undoable_revision(self):
        self.project['params']['subtitle_text_cleanup'] = True
        self.project['segments'] = [
            {'start': 0., 'end': 2., 'text': '但底下却是另一个世界', 'translated': ''},
            {'start': 2., 'end': 4., 'text': '但底下却是另一个世界 @', 'translated': ''},
            {'start': 4., 'end': 6., 'text': '但底下却是另一个世界', 'translated': ''}]
        core.put('project', self.project['id'], self.project)
        with patch.object(server, 'active_secret', return_value='synthetic-fixture-no-real-key'), \
                patch.object(server, 'active_key_id', return_value='synthetic-fixture-id'):
            response = self.client.post('/api/jobs', json={'kind': 'translate',
                'project_id': self.project['id'], 'expected_revision': 20., 'replace_segments': True})
        self.assertEqual(response.status_code, 200, response.text)
        job = response.json()
        saved = core.get('project', self.project['id'])
        self.assertEqual(len(saved['segments']), 1)
        self.assertNotIn('@', saved['segments'][0]['text'])
        self.assertEqual(saved['segments'][0]['start'], 0.)
        self.assertEqual(saved['segments'][0]['end'], 6.)
        self.assertGreater(saved['updated'], 20.)
        self.assertEqual(job['project_revision'], saved['updated'])
        packet = json.loads((self.data/'jobs'/job['id']/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(packet['project']['segments'], saved['segments'])
        report = json.loads((self.data/'jobs'/job['id']/job['text_preparation_file']).read_text(encoding='utf-8'))
        self.assertEqual((report['input_count'], report['output_count']), (3, 1))
        self.assertTrue(core.all_items('project_revision'))

    def test_rejected_request_never_commits_text_preparation(self):
        self.project['params']['subtitle_text_cleanup'] = True
        self.project['segments'][0]['text'] += ' @Account'
        core.put('project', self.project['id'], self.project)
        before = copy.deepcopy(self.project)
        with patch.object(server, 'active_secret', return_value=None):
            response = self.client.post('/api/jobs', json={'kind': 'translate',
                'project_id': self.project['id'], 'expected_revision': 20., 'replace_segments': True})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(core.get('project', self.project['id']), before)
        self.assertEqual(len(core.all_items('job')), 1)

    def test_selected_preparation_keeps_ids_metadata_and_unselected_rows(self):
        self.project['params']['subtitle_text_cleanup'] = True
        self.project['segments'][0].update(id='source-track-A', text='An entire complete dialogue sentence @Account',
                                          custom_note={'authored': True})
        self.project['segments'][1].update(id='source-track-B', text='Untouched complete sentence @Account',
                                          custom_note={'untouched': 'exactly'})
        core.put('project', self.project['id'], self.project)
        untouched = copy.deepcopy(self.project['segments'][1])
        with patch.object(server, 'active_secret', return_value='synthetic-fixture-no-real-key'), \
                patch.object(server, 'active_key_id', return_value='synthetic-fixture-id'):
            response = self.client.post('/api/jobs', json={'kind': 'translate',
                'project_id': self.project['id'], 'expected_revision': 20., 'replace_segments': True,
                'segment_indices': [0]})
        self.assertEqual(response.status_code, 200, response.text)
        saved = core.get('project', self.project['id'])
        self.assertEqual(saved['segments'][1], untouched)
        self.assertEqual(saved['segments'][0]['id'], 'source-track-A')
        self.assertEqual(saved['segments'][0]['custom_note'], {'authored': True})
        self.assertNotIn('@', saved['segments'][0]['text'])
        self.assertEqual(response.json()['segment_indices'], [0])

    def test_render_continuation_preserves_the_entire_approved_table(self):
        self.project['segments'][0].update(id='caption-id-1', custom_note={'reviewed': True})
        self.row['result']['segments'] = copy.deepcopy(self.project['segments'])
        self.row['result']['pre_render_signature'] = pre_render_signature(self.project)
        write_review_checkpoint(self.project, self.project, self.row, self.folder, self.row['result'])
        core.put('project', self.project['id'], self.project)
        core.put('job', self.row['id'], self.row)
        approved = self.approve()
        self.assertEqual(approved.status_code, 200, approved.text)
        saved = approved.json()['project']
        response = self.client.post('/api/studio/jobs/' + self.row['id'] + '/continue-pipeline',
                                    json={'expected_revision': saved['updated']})
        self.assertEqual(response.status_code, 200, response.text)
        packet = json.loads((self.data/'jobs'/response.json()['id']/'input.json').read_text(encoding='utf-8'))
        self.assertEqual(packet['project']['segments'], saved['segments'])
        require_pre_render_approval(packet['project'])

    def test_review_narration_uses_canonical_clean_text_and_roles_without_editing_captions(self):
        self.project['params'].update(subtitle_text_cleanup=True, tts_text_source='translated')
        self.project['segments'][0]['translated'] = 'Một lời thoại hoàn chỉnh @Account'
        self.project['segments'][1]['translated'] = 'Nguồn video: @Municipal'
        self.row['result']['segments'] = copy.deepcopy(self.project['segments'])
        self.row['result']['pre_render_signature'] = pre_render_signature(self.project)
        write_review_checkpoint(self.project, self.project, self.row, self.folder, self.row['result'])
        core.put('project', self.project['id'], self.project)
        core.put('job', self.row['id'], self.row)
        before = copy.deepcopy(self.project)
        plan = self.plan()
        self.assertTrue(plan['can_approve'], plan)
        self.assertEqual(plan['narration'], [
            {'index': 0, 'text': 'Một lời thoại hoàn chỉnh', 'enabled': True},
            {'index': 1, 'text': '', 'enabled': False}])
        self.assertEqual(core.get('project', self.project['id']), before)
        self.assertIn('@Account', before['segments'][0]['translated'])
        self.assertNotIn('speech_enabled', before['segments'][1])
        self.provider.assert_not_called(); self.vault.assert_not_called(); self.load.assert_not_called()

    def test_review_narration_sanitizes_voice_even_when_legacy_cleanup_is_off(self):
        self.project['params'].update(subtitle_text_cleanup=False, tts_text_source='translated')
        self.project['segments'][0]['translated'] = 'Exact legacy narration @Account'
        self.row['result']['pre_render_signature'] = pre_render_signature(self.project)
        write_review_checkpoint(self.project, self.project, self.row, self.folder, self.row['result'])
        core.put('project', self.project['id'], self.project)
        core.put('job', self.row['id'], self.row)
        self.assertEqual(self.plan()['narration'][0],
                         {'index': 0, 'text': 'Exact legacy narration', 'enabled': True})
        self.assertEqual(self.project['segments'][0]['translated'], 'Exact legacy narration @Account')


if __name__ == '__main__':
    unittest.main()
