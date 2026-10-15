"""Production admission and artifact identities, using only fixture files."""
from contextlib import ExitStack
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zipfile

from fastapi.testclient import TestClient
import core
import server
import studio_api
import project_bundle
import storage_manager
from artifact_resolver import final_result_path, render_fingerprint, resolve_final
from stage_journal import describe_file


class ExecutionStateArtifactTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='state-artifacts-', dir=core.DATA)))
        (self.data/'jobs').mkdir()
        for module in (core, server, studio_api, project_bundle, storage_manager):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        # Fail closed if an accidental path in a test reaches Windows secrets.
        for name in ('get_password', 'set_password', 'delete_password'):
            self.stack.enter_context(patch('keyring.'+name, side_effect=AssertionError('No test credentials')))
        self.stack.enter_context(patch('preflight.inspect', return_value={'ok': True, 'errors': [], 'warnings': []}))
        self.stack.enter_context(patch('hardware_admission.hardware_info', return_value={}))
        self.source = self.data/'source.mp4'; self.source.write_bytes(b'fixture source, never decoded')
        self.p = {'id': uuid.uuid4().hex, 'name': 'Fixture', 'updated': 10., 'created': 1.,
            'source_revision': uuid.uuid4().hex,
            'media': {'path': str(self.source), 'duration': 10., 'width': 1280, 'height': 720, 'audio': True},
            'params': core.DEFAULTS | {'use_clean': False, 'pipeline_clean': False, 'pipeline_translate': False,
                'pipeline_extract': 'existing', 'pipeline_tts': False, 'voice_gain': 0},
            'regions': [], 'extraction_regions': [], 'artifacts': {},
            'segments': [{'start': 0., 'end': 10., 'text': 'Original', 'translated': 'Xin chào'}]}
        core.put('project', self.p['id'], self.p)
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token': server.TOKEN}))

    def final(self, kind='render', created=1, project=None, fingerprint=True, payload=b'fixture final', anchor=True):
        project = project or self.p
        identifier = uuid.uuid4().hex
        folder = self.data/'jobs'/identifier; folder.mkdir()
        (folder/'final.mp4').write_bytes(payload)
        result = {'files': ['final.mp4'], 'primary_file': 'final.mp4', 'verification': {'fixture': True}}
        # The fixture represents the bytes captured at a successful verify;
        # it exercises resolver identities rather than video decoding.
        if anchor:
            result['verification']['file_integrity'] = [describe_file(folder, 'final.mp4')]
        if fingerprint: result['render_fingerprint'] = render_fingerprint(project)
        row = {'id': identifier, 'project_id': self.p['id'], 'project_revision': project['updated'],
            'source_revision': project['source_revision'], 'kind': kind, 'state': 'done',
            'created': created, 'result': result}
        core.put('job', identifier, row)
        self.p['artifacts'][kind] = result | {'job_id': identifier}
        core.put('project', self.p['id'], self.p)
        return row

    def test_active_channel_survives_history_pagination(self):
        active = {'id': uuid.uuid4().hex, 'project_id': self.p['id'], 'kind': 'pipeline',
            'state': 'running', 'created': 1, 'source_revision': self.p['source_revision']}
        core.put('job', active['id'], active)
        for i in range(65):
            identifier = uuid.uuid4().hex
            core.put('job', identifier, dict(id=identifier, project_id=self.p['id'], kind='voice_preview', state='done', created=2+i))
        data = self.client.get('/api/studio/state').json()
        self.assertNotIn(active['id'], [row['id'] for row in data['jobs']])
        self.assertIn(active['id'], [row['id'] for row in data['active_jobs']])
        self.assertEqual(len(data['jobs']), 60)
        active.update(state='done', ended=data['now']+.01)
        core.put('job', active['id'], active)
        changed = self.client.get('/api/studio/state', params={'after': data['now']}).json()
        self.assertNotIn(active['id'], [row['id'] for row in changed['jobs']])
        self.assertIn(active['id'], [row['id'] for row in changed['job_updates']])

    def test_new_request_id_deduplicates_existing_pipeline_and_render(self):
        for kind in ('pipeline', 'render'):
            with self.subTest(kind=kind):
                request = {'kind': kind, 'project_id': self.p['id'], 'replace_segments': True, 'request_id': uuid.uuid4().hex}
                one = self.client.post('/api/jobs', json=request)
                self.assertEqual(one.status_code, 200, one.text)
                request['request_id'] = uuid.uuid4().hex
                two = self.client.post('/api/jobs', json=request)
                self.assertEqual(two.status_code, 200, two.text)
                self.assertEqual(one.json()['id'], two.json()['id'])
                row = one.json(); row['state'] = 'done'; core.put('job', row['id'], row)
                request['request_id'] = uuid.uuid4().hex
                three = self.client.post('/api/jobs', json=request)
                self.assertEqual(three.status_code, 200, three.text)
                self.assertNotEqual(three.json()['id'], row['id'])
                row = three.json(); row['state'] = 'done'; core.put('job', row['id'], row)

    def test_changed_plan_and_cross_kind_conflict(self):
        request = {'kind': 'pipeline', 'project_id': self.p['id'], 'replace_segments': True}
        self.assertEqual(self.client.post('/api/jobs', json=request).status_code, 200)
        response = self.client.post('/api/jobs', json=request | {'kind': 'render'})
        self.assertEqual(response.status_code, 409)
        self.p['params']['font_size'] = 60; core.put('project', self.p['id'], self.p)
        self.assertEqual(self.client.post('/api/jobs', json=request).status_code, 409)

    def test_current_fingerprint_ignores_name_revision_delivery_and_alignment(self):
        before = render_fingerprint(self.p)
        changed = copy.deepcopy(self.p); changed.update(name='Renamed', updated=99.)
        changed['params'].update(export_name='Other', export_directory='D:\\Elsewhere', translation_model='metadata change')
        changed['segments'][0].update(caption_words=[{'start': 0, 'end': 1, 'text': 'Xin'}], speaker='Person label')
        self.assertEqual(render_fingerprint(changed), before)
        self.final(project=changed)
        self.assertIsNotNone(resolve_final(self.p, core.all_items('job'), self.data)['current'])

    def test_text_style_audio_and_region_changes_make_final_historical(self):
        self.final()
        for change in ('text', 'style', 'audio', 'clean'):
            changed = copy.deepcopy(self.p)
            if change == 'text': changed['segments'][0]['translated'] = 'Lời khác'
            if change == 'style': changed['params']['font_size'] = 60
            if change == 'audio': changed['params']['original_gain'] = .8
            if change == 'clean':
                changed['params'].update(use_clean=True, method='blur')
                changed['regions'] = [{'x': 0, 'y': 0, 'w': 100, 'h': 30, 'start': 0, 'end': 10}]
            result = resolve_final(changed, core.all_items('job'), self.data)
            self.assertIsNone(result['current'], change)
            self.assertIsNotNone(result['historical'], change)

    def test_model_identity_changes_invalidate_generated_voice_final(self):
        self.p['params']['voice_gain'] = 1
        with patch('worker.speech_signature', return_value='voice-model-version-1'):
            self.final()
            self.assertIsNotNone(resolve_final(self.p, core.all_items('job'), self.data)['current'])
        with patch('worker.speech_signature', return_value='voice-model-version-2'):
            result = resolve_final(self.p, core.all_items('job'), self.data)
        self.assertIsNone(result['current']); self.assertIsNotNone(result['historical'])

    def test_newer_detached_wrong_contents_never_supersedes_current(self):
        current = self.final(created=1)
        changed = copy.deepcopy(self.p); changed['segments'][0]['translated'] = 'Other editor'
        old = self.final(kind='pipeline', created=20, project=changed)
        old['project_applied'] = False; core.put('job', old['id'], old)
        result = self.client.get('/api/studio/projects/'+self.p['id']+'/final-result').json()
        self.assertEqual(result['current']['job_id'], current['id'])
        self.assertEqual(result['historical']['job_id'], old['id'])

    def test_bundle_uses_newer_current_pipeline_and_rejects_stale_only(self):
        self.final(created=1, payload=b'old render')
        current = self.final(kind='pipeline', created=20, payload=b'current pipeline')
        path = project_bundle.export_bundle(self.p, False, False, True)
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.read('artifacts/final.mp4'), b'current pipeline')
        self.p['segments'][0]['translated'] = 'Updated after export'
        with self.assertRaisesRegex(ValueError, 'khớp nội dung'):
            project_bundle.export_bundle(self.p, False, False, True)

    def test_unproven_legacy_final_remains_historical_even_same_revision(self):
        self.final(fingerprint=False)
        result = resolve_final(self.p, core.all_items('job'), self.data)
        self.assertIsNone(result['current'])
        self.assertFalse(result['historical']['identity_verified'])

    def change_payload(self, path):
        before = path.stat()
        data = bytearray(path.read_bytes())
        data[-1] ^= 1
        path.write_bytes(data)
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1000000))

    def publish_copy(self, row):
        folder = self.data / 'exports'
        folder.mkdir(exist_ok=True)
        path = folder / (row['id'] + '-human-title.mp4')
        path.write_bytes((self.data / 'jobs' / row['id'] / 'final.mp4').read_bytes())
        row['result']['published'] = {'path': str(path), 'bytes': path.stat().st_size}
        core.put('job', row['id'], row)
        return path

    def test_same_size_local_corruption_is_never_current_or_reanchored(self):
        row = self.final()
        self.assertIsNotNone(resolve_final(self.p, [row], self.data)['current'])
        self.change_payload(self.data / 'jobs' / row['id'] / 'final.mp4')
        with patch('core.probe', side_effect=AssertionError('No decode during polling')):
            resolved = resolve_final(self.p, [row], self.data)
        self.assertIsNone(resolved['current'])
        self.assertIsNone(resolved['historical'])
        self.assertEqual(resolved['unavailable'][0]['status'], 'corrupt')
        with self.assertRaisesRegex(ValueError, 'lần kiểm chứng'):
            final_result_path(row, self.data)

    def test_changed_published_copy_falls_back_to_original_verified_local(self):
        row = self.final()
        published = self.publish_copy(row)
        self.change_payload(published)
        resolved = resolve_final(self.p, [row], self.data)
        current = resolved['current']
        self.assertIsNotNone(current)
        self.assertEqual(Path(current['path']), (self.data / 'jobs' / row['id'] / 'final.mp4').resolve())
        self.assertTrue(current['delivery_warning'])
        self.assertEqual(current['copies'][0]['status'], 'corrupt')
        self.assertEqual(final_result_path(row, self.data), Path(current['path']))

    def test_changed_local_copy_uses_published_checksum_even_with_different_name(self):
        row = self.final()
        published = self.publish_copy(row)
        self.change_payload(self.data / 'jobs' / row['id'] / 'final.mp4')
        current = resolve_final(self.p, [row], self.data)['current']
        self.assertEqual(Path(current['path']), published.resolve())
        self.assertTrue(current['identity_verified'])
        self.assertEqual(current['copies'][1]['status'], 'corrupt')
        self.assertEqual(final_result_path(row, self.data), published.resolve())

    def test_verified_published_copy_survives_missing_local_and_never_decodes(self):
        row = self.final()
        published = self.publish_copy(row)
        (self.data / 'jobs' / row['id'] / 'final.mp4').unlink()
        with patch('core.probe', side_effect=AssertionError('No decode during polling')):
            for _ in range(2):
                current = resolve_final(self.p, [row], self.data)['current']
                self.assertEqual(Path(current['path']), published.resolve())
                self.assertEqual(current['payload_status'], 'verified')

    def test_both_changed_copies_are_unavailable_even_with_matching_fingerprint(self):
        row = self.final()
        published = self.publish_copy(row)
        self.change_payload(published)
        self.change_payload(self.data / 'jobs' / row['id'] / 'final.mp4')
        resolved = resolve_final(self.p, [row], self.data)
        self.assertIsNone(resolved['current'])
        self.assertIsNone(resolved['historical'])
        self.assertEqual(resolved['unavailable'][0]['status'], 'corrupt')

    def test_legacy_payload_without_anchor_is_unknown_and_explicit_link_remains(self):
        row = self.final(anchor=False)
        resolved = resolve_final(self.p, [row], self.data)
        self.assertIsNone(resolved['current'])
        self.assertEqual(resolved['historical']['payload_status'], 'unknown')
        self.assertFalse(resolved['historical']['identity_verified'])
        self.assertIn('cần kiểm lại', resolved['historical']['reason'])
        self.assertEqual(final_result_path(row, self.data), (self.data / 'jobs' / row['id'] / 'final.mp4').resolve())

    def test_media_final_streams_verified_published_when_local_is_changed_or_missing(self):
        row = self.final()
        published = self.publish_copy(row)
        expected = published.read_bytes()
        local = self.data / 'jobs' / row['id'] / 'final.mp4'
        self.change_payload(local)
        route = '/media/job/' + row['id'] + '/final.mp4'
        response = self.client.get(route)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, expected)
        local.unlink()
        response = self.client.get(route)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, expected)

    def test_media_final_rejects_known_corruption_before_streaming(self):
        row = self.final()
        published = self.publish_copy(row)
        self.change_payload(published)
        self.change_payload(self.data / 'jobs' / row['id'] / 'final.mp4')
        response = self.client.get('/media/job/' + row['id'] + '/final.mp4')
        self.assertEqual(response.status_code, 410)
        self.assertIn('lần kiểm chứng', response.json()['detail'])

    def test_deleted_or_other_source_final_not_offered(self):
        row = self.final()
        (self.data/'jobs'/row['id']/'final.mp4').unlink()
        self.assertIsNone(resolve_final(self.p, core.all_items('job'), self.data)['current'])
        other = copy.deepcopy(self.p); other['source_revision'] = 'other'
        self.final(project=other)
        result = resolve_final(self.p, core.all_items('job'), self.data)
        self.assertIsNone(result['current']); self.assertIsNone(result['historical'])


if __name__ == '__main__': unittest.main()
