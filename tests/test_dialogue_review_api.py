"""Authenticated explicit dialogue review with a private editor database."""
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
import storage_manager


class DialogueReviewAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(dir=core.DATA,
                                                                            prefix='dialogue-review-api-')))
        (self.data / 'jobs').mkdir()
        for module in (core, server, studio_api, storage_manager):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        # Apply uses the real segment/voice validator. Supply a ready, private
        # identity instead of relying on Piper weights installed on this host.
        # These bytes are intentionally not a usable inference model.
        model, config = self.data / 'fixture.onnx', self.data / 'fixture.onnx.json'
        model.write_bytes(b'synthetic-dialogue-review-identity-only')
        config.write_text('{}', encoding='utf-8')
        entry = {'id': 'piper:vais1000', 'name': 'Synthetic review voice',
                 'model': model, 'config': config, 'speaker_id': None}
        self.stack.enter_context(patch('voices.entries', return_value=[entry]))
        self.load_voice = self.stack.enter_context(patch('voices.load_voice', side_effect=AssertionError(
            'Dialogue review tests must not load an inference model.')))
        source = self.data / 'source.mp4'; source.write_bytes(b'private synthetic source identity')
        self.project = {'id': uuid.uuid4().hex, 'name': 'Dialogue review synthetic fixture',
            'created': 1., 'updated': 1., 'source_revision': 'synthetic-source-v1',
            'regions': [{'x': 20., 'y': 650., 'w': 900., 'h': 40., 'start': 0., 'end': 10.}],
            'extraction_regions': [{'x': 10., 'y': 630., 'w': 1000., 'h': 80., 'start': 0., 'end': 10.}],
            'params': copy.deepcopy(core.DEFAULTS) | {'voice_speed': 1.1, 'voice_gain': .9,
                'tts_text_source': 'translated', 'method': 'box', 'font_size': 42.,
                'pronunciation_dictionary': 'synthetic => fixture'},
            'media': {'path': str(source), 'duration': 10., 'width': 1280, 'height': 720,
                      'fps': 30., 'audio': False},
            'segments': [
                {'start': 0., 'end': 2., 'text': '完整的叙述内容继续下去', 'translated': 'Đây là lời thoại nguyên vẹn',
                 'subtitle_style': {'font_size': 44.}},
                {'start': 2., 'end': 4., 'text': '完整的叙述内容继续下去', 'translated': 'Đây là lời thoại nguyên vẹn',
                 'words': [{'start': 2., 'end': 3., 'word': '完整'}]},
                {'start': 4., 'end': 6., 'text': '视频来源：@Municipal', 'translated': 'Nguồn video: @Municipal'},
                {'start': 6., 'end': 8., 'text': '另一句全新的叙述内容', 'translated': 'Một câu khác cần được giữ đầy đủ',
                 'voice_id': 'piper:vais1000', 'character_name': 'Người dẫn', 'speech_rate': 1.2,
                 'pause_before_ms': 50, 'pause_after_ms': 40}],
            'coverage': {'method': 'ocr', 'source_signature': 'synthetic-source-signature'},
            'subtitle_review': {'signature': 'preserved-review'},
            'artifacts': {'clean': {'job_id': 'synthetic-clean'}}}
        core.put('project', self.project['id'], self.project)
        self.generate = self.stack.enter_context(patch('providers.generate', side_effect=AssertionError('no provider')))
        self.vault = self.stack.enter_context(patch('credentials.active_secret', side_effect=AssertionError('no credential')))
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token': server.TOKEN}))
        self.endpoint = '/api/studio/projects/' + self.project['id'] + '/dialogue-review'

    def objects(self):
        return {kind: core.all_items(kind) for kind in ('project', 'project_backup', 'project_revision', 'job')}

    def preview(self):
        response = self.client.get(self.endpoint)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def body(self, *, kind='duplicate_group'):
        preview = self.preview()
        proposal = next(p for p in preview['proposals'] if p['kind'] == kind)
        return {'expected_revision': preview['expected_revision'], 'signature': preview['signature'],
                'proposal_ids': [proposal['id']]}

    def assert_no_provider(self):
        self.generate.assert_not_called()
        self.vault.assert_not_called()
        self.load_voice.assert_not_called()

    def test_get_is_read_only_and_calls_neither_provider_nor_credentials(self):
        before = self.objects()
        preview = self.preview()
        self.assertEqual(preview['expected_revision'], self.project['updated'])
        self.assertEqual([p['kind'] for p in preview['proposals']], ['duplicate_group', 'source_credit'])
        self.assertEqual(self.objects(), before)
        self.assertEqual(preview, self.preview())
        self.assert_no_provider()

    def test_apply_only_selected_speech_role_preserves_authored_fields_and_revision_history(self):
        response = self.client.post(self.endpoint + '/apply', json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        expected = copy.deepcopy(self.project)
        expected['segments'][1]['speech_enabled'] = False
        self.assertGreater(result['updated'], expected['updated'])
        expected['updated'] = result['updated']
        self.assertEqual(result, expected)
        self.assertEqual(core.get('project', self.project['id']), result)
        self.assertEqual(core.get('project_backup', self.project['id']), self.project)
        revisions = core.all_items('project_revision')
        self.assertEqual(revisions, [self.project])
        self.assertNotIn('speech_enabled', result['segments'][2])
        self.assert_no_provider()

    def test_selecting_credit_does_not_apply_duplicate_proposal(self):
        response = self.client.post(self.endpoint + '/apply', json=self.body(kind='source_credit'))
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertFalse(result['segments'][2]['speech_enabled'])
        self.assertNotIn('speech_enabled', result['segments'][0])
        self.assertNotIn('speech_enabled', result['segments'][1])
        self.assert_no_provider()

    def test_stale_revision_is_409_without_any_write(self):
        body = self.body() | {'expected_revision': 0.}
        before = self.objects()
        response = self.client.post(self.endpoint + '/apply', json=body)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.objects(), before)
        self.assert_no_provider()

    def test_same_revision_but_stale_signature_is_400_without_write(self):
        body = self.body()
        changed = copy.deepcopy(self.project)
        changed['segments'][3]['translated'] += ' đã đổi'
        core.put('project', changed['id'], changed)
        before = self.objects()
        response = self.client.post(self.endpoint + '/apply', json=body)
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.objects(), before)
        self.assert_no_provider()

    def test_unknown_ids_are_400_without_partially_applying_known_id(self):
        body = self.body()
        before = self.objects()
        response = self.client.post(self.endpoint + '/apply', json=body | {
            'proposal_ids': body['proposal_ids'] + ['unknown-proposal']})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.objects(), before)
        self.assert_no_provider()

    def test_queued_and_running_project_jobs_block_apply_with_409(self):
        body = self.body()
        for state in ('queued', 'running'):
            with self.subTest(state=state):
                core.put('job', 'synthetic-active', {'id': 'synthetic-active', 'project_id': self.project['id'],
                    'kind': 'pipeline', 'state': state, 'created': 3.})
                before = self.objects()
                response = self.client.post(self.endpoint + '/apply', json=body)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.objects(), before)
        self.assert_no_provider()

    def test_unrelated_active_project_does_not_block_review_application(self):
        core.put('job', 'synthetic-other', {'id': 'synthetic-other', 'project_id': 'another-project',
                 'kind': 'pipeline', 'state': 'running', 'created': 3.})
        response = self.client.post(self.endpoint + '/apply', json=self.body())
        self.assertEqual(response.status_code, 200, response.text)
        self.assert_no_provider()

    def test_missing_or_invalid_auth_blocks_get_and_apply_with_403(self):
        body = self.body(); before = self.objects()
        for supplied in ('', 'invalid-synthetic-token'):
            with self.subTest(supplied=supplied):
                headers = {'x-studio-token': supplied}
                response = self.client.get(self.endpoint, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                response = self.client.post(self.endpoint + '/apply', json=body, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(self.objects(), before)
        self.assert_no_provider()

    def test_bool_or_non_numeric_revision_is_409_without_write(self):
        body = self.body(); before = self.objects()
        for revision in (True, False, '1.0', None):
            with self.subTest(revision=revision):
                response = self.client.post(self.endpoint + '/apply', json=body | {'expected_revision': revision})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.objects(), before)
        self.assert_no_provider()


if __name__ == '__main__':
    unittest.main()
