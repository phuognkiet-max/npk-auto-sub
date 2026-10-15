"""OCR search-strategy checkpoint compatibility and API rejection, using temporary data only."""
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
import ocr_checkpoint
import server
import storage_manager
import studio_api
from subtitle_quality import digest, source_signature


def project(source):
    return {'id': uuid.uuid4().hex, 'name': 'OCR checkpoint fixture', 'updated': 1.,
            'created': 1., 'source_revision': 'fixture-source', 'segments': [],
            'regions': [], 'extraction_regions': [], 'artifacts': {},
            'media': {'path': str(source), 'duration': 198.111995, 'video_duration': 198.1,
                      'fps': 30., 'width': 1280, 'height': 720},
            'params': copy.deepcopy(core.DEFAULTS)}


def checkpoint(p):
    rows = [{'start': float(i * 2), 'end': i * 2 + 1.8, 'text': f'Câu {i + 1}',
             'translated': ''} for i in range(87)]
    return {'signature': ocr_checkpoint.signature(p), 'rows': rows,
            'current': {'start': 196., 'end': 198., 'text': 'Câu cuối', 'translated': ''},
            'next_time': 198., 'clipped_samples': []}


def write(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')


class OCRCheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='npk-ocr-checkpoint-')
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.source = self.folder / 'source.mp4'
        self.source.write_bytes(b'local source identity fixture')
        self.p = project(self.source)
        self.path = self.folder / 'recognition-checkpoint.json'

    def test_signature_binds_automatic_search_area_and_not_effective_video_end(self):
        for start, end in ((0, 0), (10.2, 100.4)):
            with self.subTest(start=start, end=end):
                p = copy.deepcopy(self.p)
                p['params'].update(extraction_start=start, extraction_end=end)
                params, media = p['params'], p['media']
                original = digest({'source': source_signature(p),
                    'regions': p.get('extraction_regions', p['regions']),
                    'range': [max(0, float(params.get('extraction_start', 0))),
                              min(media['duration'], float(params.get('extraction_end') or media['duration']))],
                    'interval': 1 / float(params.get('ocr_fps', 2)),
                    'confidence': params.get('ocr_confidence', .55),
                    'similarity': params.get('ocr_similarity', .84),
                    'scale': params.get('ocr_scale_mode', 'native'),
                    'search_area': 'automatic_full_frame_v1', 'version': 5})
                self.assertEqual(ocr_checkpoint.signature(p), original)
                p['media']['video_duration'] = 150.
                p['params']['voice_speed'] = 1.2
                self.assertEqual(ocr_checkpoint.signature(p), original)

    def test_legacy_lower_frame_checkpoint_is_preserved_but_not_silently_resumed(self):
        p = copy.deepcopy(self.p)
        saved = checkpoint(p)
        saved['signature'] = digest({'source': source_signature(p), 'regions': [],
            'range': [0., p['media']['duration']], 'interval': .5, 'confidence': .55,
            'similarity': .84, 'scale': 'native', 'version': 4})
        write(self.path, saved)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            ocr_checkpoint.validate_resume(self.path, p)
        self.assertEqual(self.path.read_bytes(), before)

    def test_incomplete_tail_preserves_all_88_cues_and_confirmed_eof_can_pass_end(self):
        saved = checkpoint(self.p)
        for next_time in (198., 198.5):
            with self.subTest(next_time=next_time):
                saved['next_time'] = next_time
                if next_time > self.p['media']['video_duration']:
                    saved['decoded_complete'] = True
                write(self.path, saved)
                restored = ocr_checkpoint.validate_resume(self.path, self.p)
                self.assertEqual(restored, saved)
                self.assertEqual(len(restored['rows']) + (restored['current'] is not None), 88)
                if next_time == 198.:
                    self.assertNotIn('decoded_complete', restored)

    def test_tail_without_decoder_confirmation_never_silently_restarts(self):
        saved = checkpoint(self.p)
        for next_time, video_end in ((198.1, 198.1), (198.5, 198.1), (198., 197.983333)):
            with self.subTest(next_time=next_time, video_end=video_end):
                p = copy.deepcopy(self.p)
                p['media']['video_duration'] = video_end
                saved['next_time'] = next_time
                write(self.path, saved)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, 'chưa xác nhận'):
                    ocr_checkpoint.validate_resume(self.path, p)
                self.assertEqual(self.path.read_bytes(), before)

    def test_changed_source_or_ocr_configuration_is_rejected(self):
        saved = checkpoint(self.p)
        write(self.path, saved)
        alternatives = [({'params': {'ocr_scale_mode': 'legacy'}}),
                        {'params': {'ocr_fps': 4}},
                        {'params': {'extraction_end': 198.}},
                        {'extraction_regions': [{'x': 0, 'y': 1, 'w': 20, 'h': 10}]}]
        for changes in alternatives:
            p = copy.deepcopy(self.p)
            for field, value in changes.items():
                if field == 'params':
                    p['params'].update(value)
                else:
                    p[field] = value
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'không khớp'):
                ocr_checkpoint.validate_resume(self.path, p)
        self.source.write_bytes(b'changed source has a different size')
        with self.assertRaisesRegex(ValueError, 'không khớp'):
            ocr_checkpoint.validate_resume(self.path, self.p)
        self.source.unlink()
        with self.assertRaisesRegex(ValueError, 'video nguồn'):
            ocr_checkpoint.validate_resume(self.path, self.p)

    def test_malformed_nonfinite_and_out_of_bounds_checkpoint_is_rejected(self):
        saved = checkpoint(self.p)
        mutations = [lambda d: d.update(next_time=True), lambda d: d.update(next_time=-1),
                     lambda d: d.update(next_time=199), lambda d: d.update(decoded_complete=1),
                     lambda d: d.update(current=[]), lambda d: d.pop('current'),
                     lambda d: d['current'].update(end=199), lambda d: d['current'].update(start=170),
                     lambda d: d['rows'][0].update(start=-.1), lambda d: d['rows'][0].update(end=0),
                     lambda d: d['rows'][0].update(start=False), lambda d: d['rows'][0].update(text=''),
                     lambda d: d.update(clipped_samples=[2., 1.]), lambda d: d.update(clipped_samples=[float('nan')]),
                     lambda d: d.update(metadata={'invalid': float('inf')}),
                     lambda d: d.update(rows=[saved['rows'][0]] * 10001)]
        for mutation in mutations:
            value = copy.deepcopy(saved)
            mutation(value)
            write(self.path, value)
            with self.subTest(value=str(value)[:100]), self.assertRaises(ValueError):
                ocr_checkpoint.validate_resume(self.path, self.p)
        for raw in ('[]', '{', '{"rows":[],"rows":[]}',
                    '{"metadata":1e10000}', '[' * 1000 + '0' + ']' * 1000):
            self.path.write_text(raw, encoding='utf-8')
            with self.subTest(raw=raw[:30]), self.assertRaises(ValueError):
                ocr_checkpoint.validate_resume(self.path, self.p)
        write(self.path, saved)
        with patch.object(ocr_checkpoint, 'MAX_BYTES', 32), self.assertRaises(ValueError):
            ocr_checkpoint.validate_resume(self.path, self.p)


class OCRResumeAPITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.data = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='npk-ocr-api-')))
        (self.data / 'jobs').mkdir()
        for module in (core, server, studio_api, storage_manager):
            self.stack.enter_context(patch.object(module, 'DATA', self.data))
        self.stack.enter_context(patch.object(server, 'active_id', None))
        self.stack.enter_context(patch.object(server, 'active_process', None))
        self.stack.enter_context(patch('preflight.inspect', return_value={'ok': True}))
        self.client = self.stack.enter_context(TestClient(server.app, headers={'x-studio-token': server.TOKEN}))
        self.source = self.data / 'source.mp4'
        self.source.write_bytes(b'API local source identity fixture')
        self.p = project(self.source)
        core.put('project', self.p['id'], self.p)
        self.previous = {'id': uuid.uuid4().hex, 'kind': 'ocr', 'state': 'error',
                         'project_id': self.p['id'], 'source_revision': self.p['source_revision']}
        core.put('job', self.previous['id'], self.previous)
        self.folder = self.data / 'jobs' / self.previous['id']
        self.folder.mkdir()
        self.path = self.folder / 'recognition-checkpoint.json'
        self.saved = checkpoint(self.p)
        write(self.path, self.saved)
        self.body = {'kind': 'ocr', 'project_id': self.p['id'], 'replace_segments': True,
                     'resume_from': self.previous['id'], 'expected_revision': self.p['updated']}

    def test_valid_resume_copies_compatible_checkpoint_exactly(self):
        detail = self.client.get('/api/studio/jobs/' + self.previous['id'])
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertTrue(detail.json()['can_resume'])
        response = self.client.post('/api/jobs', json=self.body)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual(row['resume_from'], self.previous['id'])
        copied = self.data / 'jobs' / row['id'] / self.path.name
        self.assertEqual(copied.read_bytes(), self.path.read_bytes())
        self.assertEqual(json.loads(copied.read_text(encoding='utf-8')), self.saved)

    def test_stale_or_invalid_resume_does_not_create_job_or_folder(self):
        p = copy.deepcopy(self.p)
        p['params']['ocr_scale_mode'] = 'legacy'
        core.put('project', p['id'], p)
        for raw in (None, '{', json.dumps(self.saved | {'next_time': True}),
                    json.dumps(self.saved | {'next_time': 198.5})):
            if raw is not None:
                core.put('project', self.p['id'], self.p)
                self.path.write_text(raw, encoding='utf-8')
            before = set((self.data / 'jobs').iterdir())
            detail = self.client.get('/api/studio/jobs/' + self.previous['id'])
            self.assertFalse(detail.json()['can_resume'])
            response = self.client.post('/api/jobs', json=self.body)
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(set((self.data / 'jobs').iterdir()), before)
            self.assertEqual(len(core.all_items('job')), 1)
            self.assertEqual(core.get('job', self.previous['id'])['state'], 'error')


if __name__ == '__main__':
    unittest.main()
