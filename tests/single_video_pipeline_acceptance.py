r"""Real multipart/API/queue/Piper/FFmpeg acceptance in temporary DATA only.

Run with runtime\venv\Scripts\python.exe -X utf8. This script does not connect
to the user's server, read credentials, run GPU inference, or alter live DATA.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUTPUT = ROOT / 'build/single-video-acceptance-results'


def checked(response, expected=200):
    if response.status_code != expected:
        raise AssertionError(f'HTTP {response.status_code}, expected {expected}: {response.text[:1000]}')
    return response.json()


def acceptance():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'isolated': True, 'fixture': str(ROOT / 'tests/fixtures/demo.mp4'),
              'engine': 'Piper vais1000 local CPU', 'gpu_inference': False, 'provider_calls': 0,
              'checks': [], 'started': time.time()}
    with tempfile.TemporaryDirectory(dir=ROOT / 'tests', prefix='npk-single-video-') as isolated:
        os.environ['VIETDUB_DATA_DIR'] = isolated
        os.environ['VIETDUB_TOKEN'] = 'acceptance-' + uuid.uuid4().hex
        os.environ['HF_HUB_OFFLINE'] = '1'
        os.environ['TRANSFORMERS_OFFLINE'] = '1'
        from fastapi.testclient import TestClient
        from core import DATA, all_items, binary, probe, run
        import server
        assert DATA.resolve() == Path(isolated).resolve()
        fixture = ROOT / 'tests/fixtures/demo.mp4'
        job_id = None
        queue_started = False
        try:
            with TestClient(server.app) as unauthenticated:
                with fixture.open('rb') as source:
                    checked(unauthenticated.post('/api/studio/import-video',
                        files={'file': ('demo.mp4', source, 'video/mp4')}), 403)
            report['checks'].append('Multipart authentication rejects missing token')
            with TestClient(server.app, headers={'x-studio-token': server.TOKEN}) as client:
                with fixture.open('rb') as source:
                    project = checked(client.post('/api/studio/import-video',
                        data={'name': 'Single-video acceptance'}, files={'file': ('demo.mp4', source, 'video/mp4')}))
                uploaded = Path(project['media']['path'])
                assert uploaded.is_file() and DATA in uploaded.parents
                assert uploaded.read_bytes() == fixture.read_bytes()
                assert abs(project['media']['duration'] - 8) < .1
                assert project['params']['tts_text_source'] == 'translated'
                report['checks'].append('Real multipart import stores an exact owned copy and creates an 8-second project')
                width, height = project['media']['width'], project['media']['height']
                rows = [
                    {'start': .3, 'end': 3.7, 'text': 'UNSELECTED SOURCE ONE', 'translated': 'Xin chào.',
                     'spoken_text': 'UNSELECTED EDITED ONE'},
                    {'start': 4, 'end': 7.7, 'text': 'UNSELECTED SOURCE TWO', 'translated': 'Hẹn gặp lại.',
                     'spoken_text': 'UNSELECTED EDITED TWO'},
                ]
                params = {
                    'tts_text_source': 'translated', 'voice_id': 'piper:vais1000', 'tts_device': 'cpu',
                    'tts_timing_mode': 'natural', 'voice_speed': 1, 'workload_mode': 'quiet',
                    'pipeline_extract': 'existing', 'pipeline_translate': False, 'pipeline_tts': True,
                    'pipeline_clean': True, 'method': 'blur', 'mask_source': 'manual', 'video_encoder': 'cpu',
                    'blur_radius': 8, 'original_gain': 0, 'voice_gain': 1, 'bgm_gain': 0,
                    'voice_path': '', 'bgm_path': '', 'use_clean': True, 'burn_subtitles': True,
                    'subtitle_preset': 'pop', 'export_directory': '', 'export_name': 'Acceptance-video',
                }
                region = {'x': round(width * .1), 'y': round(height * .8), 'w': round(width * .8),
                          'h': max(8, round(height * .13)), 'start': 0, 'end': 8}
                project = checked(client.put('/api/projects/' + project['id'], json={
                    'expected_revision': project['updated'], 'segments': rows, 'regions': [region], 'params': params}))
                assert project['params']['subtitle_bold'] and project['params']['font_color'] == '#FFD84D'
                assert project['params']['font_size'] == 54
                report['checks'].append('Setup saves explicit translated source, voices, cleaning and actual Pop preset values')
                quality = checked(client.get('/api/studio/projects/' + project['id'] + '/quality'))
                assert not quality['errors'], quality['errors']
                if quality['requires_review']:
                    # This is a fixed test video, not an auto-approval of user media.
                    project = checked(client.post('/api/studio/projects/' + project['id'] + '/review', json={
                        'signature': quality['signature'], 'reason': 'Known eight-second acceptance fixture; two test narration windows, remaining time is intentional silence.'}))
                body = {'project_id': project['id'], 'kind': 'pipeline', 'replace_segments': True,
                        'expected_revision': project['updated'], 'request_id': uuid.uuid4().hex}
                checked(client.post('/api/jobs', json=body | {'expected_revision': project['updated'] - 1,
                    'request_id': uuid.uuid4().hex}), 409)
                assert all_items('job') == []
                report['checks'].append('A stale project revision is rejected without creating a job')
                job = checked(client.post('/api/jobs', json=body))
                job_id = job['id']
                repeated = checked(client.post('/api/jobs', json=body))
                assert repeated['id'] == job_id and len(all_items('job')) == 1
                report['checks'].append('Repeated request_id returns the same queued job, never doubles the pipeline')
                server.start_workers()
                queue_started = True
                deadline = time.monotonic() + 180
                stages, states, last = [], [], None
                while time.monotonic() < deadline:
                    job = checked(client.get('/api/studio/jobs/' + job_id))
                    stamp = (job['state'], job.get('stage'))
                    if stamp != last:
                        states.append(job['state'])
                        stages.append(job.get('stage'))
                        print(json.dumps({'state': stamp[0], 'stage': stamp[1], 'progress': job.get('progress')}, ensure_ascii=False), flush=True)
                        last = stamp
                    if job['state'] in ('done', 'error', 'review', 'cancelled'):
                        break
                    time.sleep(.5)
                assert job['state'] == 'done', job.get('message')
                assert job.get('project_applied') is True, job
                result = job['result']
                assert result['steps'] == ['tts', 'clean', 'render'], result['steps']
                folder = DATA / 'jobs' / job_id
                snapshot = json.loads((folder / 'input.json').read_text(encoding='utf-8'))['project']
                assert snapshot['params']['tts_text_source'] == 'translated'
                from speech_delivery import delivery_window
                from subtitle_quality import digest
                from voices import audio_identity, resolve_voice
                from narration import valid_wave
                entry = resolve_voice('piper:vais1000', require_ready=True)
                for index, row in enumerate(snapshot['segments']):
                    plan = delivery_window(row, snapshot['params'], index + 1, 8)
                    assert plan['text'] == row['translated']
                    rate = snapshot['params']['voice_speed']
                    base = ['npk-speech-v1', *audio_identity(entry, snapshot['params'])]
                    selected = digest(base + [row['translated'], rate])
                    edited = digest(base + [row['spoken_text'], rate])
                    original = digest(base + [row['text'], rate])
                    assert valid_wave(DATA / 'speech-cache' / (selected + '.wav'))
                    assert not (DATA / 'speech-cache' / (edited + '.wav')).exists()
                    assert not (DATA / 'speech-cache' / (original + '.wav')).exists()
                report['checks'].append('Actual Piper cache contains chosen translated speech and neither unselected edited nor source text')
                assert result['artifacts']['tts']['voice_id'] == 'piper:vais1000'
                assert result['artifacts']['clean']['encoder'] == 'H.264 CPU'
                final = folder / 'final.mp4'
                info = probe(final)
                assert abs(info['duration'] - 8) < .1 and info['audio']
                run([binary('ffmpeg'), '-v', 'error', '-xerror', '-i', final, '-f', 'null', '-'], timeout=40)
                assert result['verification']['video_frames_decoded']['final.mp4'] > 0
                for name in ('final.mp4', 'voice.wav', 'subtitles.ass', 'subtitles.srt', 'worker.log'):
                    shutil.copyfile(folder / name, OUTPUT / name)
                assert r'\t(0,140' in (OUTPUT / 'subtitles.ass').read_text(encoding='utf-8')
                run([binary('ffmpeg'), '-v', 'error', '-y', '-ss', '1', '-i', final, '-frames:v', '1',
                     '-threads', '1', OUTPUT / 'final-preview.png'], timeout=30)
                report['checks'].append('Real sequential TTS → blur → render produces decoded 8-second video with audio and animated Pop ASS')
                published = checked(client.get('/api/studio/projects/' + project['id']))
                assert published['artifacts']['tts']['job_id'] == job_id
                assert published['artifacts']['clean']['job_id'] == job_id
                assert published['artifacts']['render']['job_id'] == job_id
                assert checked(client.post('/api/jobs', json=body))['id'] == job_id
                assert len(all_items('job')) == 1
                report['checks'].append('Completed artifacts attach to the original project; post-completion replay still returns the same job')
                report.update(ok=True, duration=info['duration'], dimensions=[info['width'], info['height']],
                              steps=result['steps'], observed_states=states, observed_stages=stages,
                              elapsed_seconds=result['elapsed_seconds'], decoded_frames=result['verification']['video_frames_decoded'],
                              output=str(OUTPUT / 'final.mp4'), preview=str(OUTPUT / 'final-preview.png'))
        except Exception as exc:
            report['error'] = str(exc)
            if job_id:
                folder = DATA / 'jobs' / job_id
                for name in ('worker.log', 'input.json', 'result.json'):
                    if (folder / name).is_file():
                        shutil.copyfile(folder / name, OUTPUT / name)
            raise
        finally:
            if queue_started:
                server.shutdown()
            report['finished'] = time.time()
            (OUTPUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    acceptance()
