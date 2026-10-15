"""Real multipart import contract in isolated data; no workers or model inference."""
import asyncio
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.requests import Request, ClientDisconnect
import core
import server
import storage_manager
import studio_api


class VideoUploadTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='npk-upload-test-')
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name).resolve()
        for module in (core, server, storage_manager, studio_api):
            override = patch.object(module, 'DATA', self.data)
            override.start()
            self.addCleanup(override.stop)
        self.client = TestClient(server.app, headers={'x-studio-token': server.TOKEN})
        self.addCleanup(self.client.close)
        self.fixture = core.ROOT / 'tests/fixtures/demo.mp4'

    def upload(self, filename='demo.mp4', content=None, name=None):
        if content is None:
            content = self.fixture.read_bytes()
        return self.client.post('/api/studio/import-video',
                                files={'file': (filename, content, 'video/mp4')},
                                data={'name': name} if name is not None else {})

    def assert_nothing_imported(self):
        self.assertEqual(core.all_items('project'), [])
        source_folder = self.data / 'sources'
        if source_folder.exists():
            self.assertEqual(list(source_folder.iterdir()), [])

    def test_valid_upload_creates_owned_video_and_project_without_starting_jobs(self):
        response = self.upload('Video tiếng Việt.MP4', name='Dự án kéo thả')
        self.assertEqual(response.status_code, 200, response.text)
        project = response.json()
        self.assertEqual(project['name'], 'Dự án kéo thả')
        source = Path(project['media']['path'])
        self.assertEqual(source.parent.parent, self.data / 'sources')
        self.assertEqual(source.name, 'source.mp4')
        self.assertEqual(source.read_bytes(), self.fixture.read_bytes())
        self.assertGreater(project['media']['duration'], 0)
        self.assertGreater(project['media']['width'], 0)
        self.assertEqual(core.get('project', project['id']), project)
        self.assertEqual(core.all_items('job'), [])

    def test_filename_is_default_display_name_and_same_name_never_overwrites_source(self):
        first, second = self.upload('Cảnh mở đầu.mp4'), self.upload('Cảnh mở đầu.mp4')
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        a, b = first.json(), second.json()
        self.assertEqual(a['name'], 'Cảnh mở đầu')
        self.assertNotEqual(a['media']['path'], b['media']['path'])
        self.assertEqual(Path(a['media']['path']).read_bytes(), self.fixture.read_bytes())
        self.assertEqual(len(core.all_items('project')), 2)

    def test_authentication_rejects_before_any_import(self):
        with TestClient(server.app) as unauthenticated:
            response = unauthenticated.post('/api/studio/import-video',
                                           files={'file': ('demo.mp4', self.fixture.read_bytes())})
        self.assertEqual(response.status_code, 403)
        self.assert_nothing_imported()

    def test_path_traversal_and_nonvideo_extension_are_rejected(self):
        for filename in ('../escape.mp4', '..\\escape.mp4', 'C:\\escape.mp4', 'audio.wav', 'bad?.mp4'):
            with self.subTest(filename=filename):
                response = self.upload(filename)
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_nothing_imported()

    def test_empty_upload_is_rejected_and_removed(self):
        response = self.upload(content=b'')
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_imported()

    def test_invalid_video_probe_does_not_commit_and_keeps_unrelated_source(self):
        kept = self.data / 'sources' / 'keep'
        kept.mkdir(parents=True)
        marker = kept / 'original.mp4'
        marker.write_bytes(b'preserve')
        with patch.object(studio_api, 'probe', side_effect=ValueError('Không có video.')):
            response = self.upload(content=b'not-video')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(core.all_items('project'), [])
        self.assertEqual(marker.read_bytes(), b'preserve')
        self.assertEqual(list((self.data / 'sources').iterdir()), [kept])

    def test_real_probe_failure_is_an_actionable_client_error(self):
        response = self.upload(content=b'This is not an MP4 file.')
        self.assertEqual(response.status_code, 400, response.text)
        self.assertIn('Không đọc được video', response.json()['detail'])
        self.assert_nothing_imported()

    def test_declared_limit_is_checked_before_creating_folder(self):
        with patch.object(studio_api, 'VIDEO_UPLOAD_LIMIT', 4):
            response = self.client.post('/api/studio/import-video',
                content=b'', headers={'content-type': 'multipart/form-data; boundary=x',
                                      'content-length': str(studio_api.VIDEO_UPLOAD_OVERHEAD + 5)})
        self.assertEqual(response.status_code, 413)
        self.assert_nothing_imported()

    def test_streamed_file_limit_is_checked_even_without_content_length(self):
        boundary = b'npk-upload-stream'
        body = (b'--' + boundary + b'\r\nContent-Disposition: form-data; name="file"; filename="demo.mp4"\r\n'
                b'Content-Type: video/mp4\r\n\r\n12345\r\n--' + boundary + b'--\r\n')
        with patch.object(studio_api, 'VIDEO_UPLOAD_LIMIT', 4):
            response = self.client.post('/api/studio/import-video', content=iter([body[:75], body[75:]]),
                headers={'content-type': 'multipart/form-data; boundary=npk-upload-stream'})
        self.assertEqual(response.status_code, 413, response.text)
        self.assert_nothing_imported()

    def test_low_space_is_rejected_without_committing_source(self):
        usage = shutil._ntuple_diskusage(total=10**10, used=10**10, free=0)
        with patch.object(studio_api.shutil, 'disk_usage', return_value=usage):
            response = self.upload()
        self.assertEqual(response.status_code, 507, response.text)
        self.assert_nothing_imported()

    def test_duplicate_file_fields_rejected_and_partial_file_removed(self):
        content = self.fixture.read_bytes()
        response = self.client.post('/api/studio/import-video',
            files=[('file', ('one.mp4', content)), ('file', ('two.mp4', content))])
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_imported()

    def test_project_name_length_and_control_characters_rejected(self):
        for name in ('a' * 201, 'two\nlines', 'null\x00name'):
            with self.subTest(name=name):
                response = self.upload(name=name)
                self.assertEqual(response.status_code, 400, response.text)
                self.assert_nothing_imported()

    def test_truncated_multipart_does_not_commit_partial_video(self):
        body = (b'--x\r\nContent-Disposition: form-data; name="file"; filename="demo.mp4"\r\n'
                b'Content-Type: video/mp4\r\n\r\npartial-video')
        response = self.client.post('/api/studio/import-video', content=body,
                                   headers={'content-type': 'multipart/form-data; boundary=x'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_imported()

    def test_disconnected_upload_removes_its_partial_source(self):
        prefix = (b'--x\r\nContent-Disposition: form-data; name="file"; filename="demo.mp4"\r\n'
                  b'Content-Type: video/mp4\r\n\r\npartial-video')
        messages = iter([{'type': 'http.request', 'body': prefix, 'more_body': True},
                         {'type': 'http.disconnect'}])

        async def receive():
            return next(messages)

        endpoint = next(route.endpoint for route in server.app.routes
                        if getattr(route, 'path', '') == '/api/studio/import-video')
        request = Request({'type': 'http', 'method': 'POST', 'path': '/api/studio/import-video',
                           'headers': [(b'content-type', b'multipart/form-data; boundary=x')]}, receive)
        with self.assertRaises(ClientDisconnect):
            asyncio.run(endpoint(request))
        self.assert_nothing_imported()

    def test_commit_rechecks_storage_pending_after_upload_and_probe(self):
        with patch.object(storage_manager, '_ensure_no_pending',
                          side_effect=[None, ValueError('Dọn dữ liệu đang bị gián đoạn.')]):
            response = self.upload()
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_imported()

    def test_nonmultipart_request_has_actionable_error(self):
        response = self.client.post('/api/studio/import-video', json={'path': 'demo.mp4'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_imported()

    def test_malformed_multipart_is_rejected_and_removed(self):
        response = self.client.post('/api/studio/import-video', content=b'not-a-boundary',
                                   headers={'content-type': 'multipart/form-data; boundary=x'})
        self.assertEqual(response.status_code, 400, response.text)
        self.assert_nothing_imported()


if __name__ == '__main__':
    unittest.main()
