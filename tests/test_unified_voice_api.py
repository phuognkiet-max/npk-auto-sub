"""Standalone narration queue and media contract; real API, no video/model inference."""
import array
import json
from pathlib import Path
import unittest
import uuid
import wave

from fastapi.testclient import TestClient
from core import DATA, all_items, get, put
from job_progress import verify_outputs
import server


class UnifiedVoiceAPITests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(server.app, headers={'x-studio-token': server.TOKEN})
        self.addCleanup(self.client.close)
        self.body = {'kind': 'text_to_speech', 'sample_text': 'Xin chào ' + uuid.uuid4().hex,
                     'params': {'voice_id': 'piper:vais1000', 'voice_speed': 1.1, 'workload_mode': 'quiet'}}

    def enqueue(self, **changes):
        return self.client.post('/api/jobs', json=self.body | changes)

    def queued(self):
        response = self.enqueue()
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_enqueue_without_project_preserves_text_voice_speed_and_private_snapshot(self):
        projects_before = {project['id'] for project in all_items('project')}
        self.body['sample_text'] = '  [cười] Xin chào. ' + uuid.uuid4().hex + '  '
        job = self.queued()
        self.assertTrue(job['standalone'])
        self.assertEqual(job['project_id'], 'text-to-speech')
        self.assertEqual(job['kind'], 'text_to_speech')
        self.assertEqual(job['state'], 'queued')
        self.assertEqual(job['sample_text'], self.body['sample_text'].strip())
        snapshot = json.loads((DATA / 'jobs' / job['id'] / 'input.json').read_text(encoding='utf-8'))
        project = snapshot['project']
        self.assertEqual(project['speech_text'], self.body['sample_text'].strip())
        self.assertEqual(project['media']['duration'], 0)
        self.assertNotIn('path', project['media'])
        self.assertEqual(project['segments'], [])
        self.assertEqual(project['params']['voice_id'], 'piper:vais1000')
        self.assertEqual(project['params']['voice_speed'], 1.1)
        self.assertEqual(project['params']['workload_mode'], 'quiet')
        self.assertEqual({project['id'] for project in all_items('project')}, projects_before)

    def test_standalone_limit_accepts_10000_characters_and_rejects_10001(self):
        response = self.enqueue(sample_text='a' * 10000)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()['sample_text']), 10000)
        before = len(all_items('job'))
        self.assertEqual(self.enqueue(sample_text='a' * 10001).status_code, 400)
        self.assertEqual(len(all_items('job')), before)

    def test_standalone_text_must_be_a_nonempty_string(self):
        before = len(all_items('job'))
        for value in (None, 123, True, [], {}, '', '   '):
            with self.subTest(value=value):
                response = self.enqueue(sample_text=value)
                self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(len(all_items('job')), before)

    def test_standalone_voice_and_speed_are_validated_before_queueing(self):
        for params in ({'voice_id': '../invalid-voice'}, {'voice_speed': .49}, {'voice_speed': 2.01}):
            with self.subTest(params=params):
                response = self.enqueue(params=self.body['params'] | params)
                self.assertEqual(response.status_code, 400, response.text)

    def test_duplicate_request_id_returns_same_standalone_job(self):
        request_id = 'standalone-api-' + uuid.uuid4().hex
        first = self.enqueue(request_id=request_id)
        second = self.enqueue(request_id=request_id)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(first.json()['id'], second.json()['id'])
        self.assertEqual(get('job_request', request_id)['job_id'], first.json()['id'])

    def test_completed_standalone_wav_and_mp3_are_served_only_from_result_allowlist(self):
        job = self.queued()
        folder = DATA / 'jobs' / job['id']
        wav_path = folder / 'speech.wav'
        with wave.open(str(wav_path), 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(22050)
            audio.writeframes(array.array('h', [1000, -1000] * 220).tobytes())
        # Media-route test only: no encoder/model and no claim this fixture MP3 is playable.
        mp3_bytes = b'ID3-test-only-media-allowlist'
        (folder / 'speech.mp3').write_bytes(mp3_bytes)
        job.update(state='done', result={'type': 'text_to_speech',
                   'files': ['speech.wav', 'speech.mp3'], 'primary_file': 'speech.wav'})
        put('job', job['id'], job)
        wav_response = self.client.get(f'/media/job/{job["id"]}/speech.wav')
        mp3_response = self.client.get(f'/media/job/{job["id"]}/speech.mp3')
        self.assertEqual(wav_response.status_code, 200)
        self.assertEqual(wav_response.content, wav_path.read_bytes())
        self.assertEqual(mp3_response.status_code, 200)
        self.assertEqual(mp3_response.content, mp3_bytes)
        self.assertEqual(self.client.get(f'/media/job/{job["id"]}/input.json').status_code, 404)
        (folder / 'speech.mp3').unlink()
        self.assertEqual(self.client.get(f'/media/job/{job["id"]}/speech.mp3').status_code, 410)

    def test_standalone_verifier_requires_both_wav_and_mp3(self):
        job = self.queued()
        folder = DATA / 'jobs' / job['id']
        snapshot = json.loads((folder / 'input.json').read_text(encoding='utf-8'))
        for missing in ('speech.wav', 'speech.mp3'):
            files = [name for name in ('speech.wav', 'speech.mp3') if name != missing]
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, 'thiếu kết quả'):
                verify_outputs({'files': files}, snapshot['project'], job, folder)
