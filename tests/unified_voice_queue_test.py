"""Opt-in real offline VieNeu queue acceptance, isolated from the user's DATA.

Run with webapp-testing's with_server.py and this file's --server mode.
Unlike the unit tests, this intentionally performs one short local inference.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tests' / 'vieneu-independent-results'
PORT = 19865
TOKEN = 'npk-unified-real-queue-test'
TEXT = 'Xin chào, đây là giọng đọc trong NPK Auto Sub.'
VOICE = 'vieneu:Hải Đăng'
sys.path.insert(0, str(ROOT))


def serve():
    import threading
    OUT.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='npk-real-voice-queue-', dir=ROOT / 'tests')
    owned_data = Path(temporary.name).resolve()
    assert owned_data.parent == (ROOT / 'tests').resolve()
    os.environ['VIETDUB_DATA_DIR'] = str(owned_data)
    os.environ['VIETDUB_TOKEN'] = TOKEN
    # These credentials exist only in this isolated server process. No synthesis
    # function, model runner, worker, or result verification is mocked.
    import credentials
    fake = {}
    credentials.keyring.get_password = lambda service, key: fake.get(key)
    credentials.keyring.set_password = lambda service, key, value: fake.__setitem__(key, value)
    credentials.keyring.delete_password = lambda service, key: fake.pop(key, None)
    import server
    import uvicorn
    from core import DATA, get
    http = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=PORT,
                                        log_level='warning', access_log=False))

    @server.app.get('/api/test/worker-log/{identifier}')
    def worker_log(identifier: str):
        if not get('job', identifier):
            return {'log': ''}
        path = DATA / 'jobs' / identifier / 'worker.log'
        return {'log': path.read_text(encoding='utf-8', errors='replace')[-20000:] if path.is_file() else ''}

    @server.app.post('/api/test/stop')
    def stop():
        http.should_exit = True
        return {'stopping': True}

    timer = threading.Timer(150, lambda: setattr(http, 'should_exit', True))
    timer.daemon = True
    timer.start()
    try:
        server.start_workers()
        http.run()
    finally:
        timer.cancel()
        server.shutdown()
        temporary.cleanup()
        (OUT / '.queue-cleanup.json').write_text(json.dumps({
            'data_removed': not owned_data.exists(),
            'queue_thread_stopped': server.queue_thread is None or not server.queue_thread.is_alive(),
            'active_worker_cleared': server.active_process is None,
        }), encoding='utf-8')


def acceptance():
    import httpx
    OUT.mkdir(exist_ok=True)
    cleanup_marker = OUT / '.queue-cleanup.json'
    cleanup_marker.unlink(missing_ok=True)
    started = time.monotonic()
    report = {'ok': False, 'voice_id': VOICE, 'sample_text': TEXT,
              'inference': 'Real NPK-owned local VieNeu; no CIT or remote inference',
              'states_seen': [], 'outputs': {}}
    caught = None
    with httpx.Client(base_url=f'http://127.0.0.1:{PORT}', headers={'x-studio-token': TOKEN},
                      trust_env=False, timeout=20) as client:
        try:
            response = client.post('/api/jobs', json={'kind': 'text_to_speech', 'sample_text': TEXT,
                                  'params': {'voice_id': VOICE, 'voice_speed': 1, 'workload_mode': 'quiet'}})
            response.raise_for_status()
            job = response.json()
            report['job_id'] = identifier = job['id']
            assert job['standalone'] is True and job['project_id'] == 'text-to-speech', job
            assert job['state'] == 'queued', job
            assert job['sample_text'] == TEXT, job
            report['states_seen'].append('queued')
            print(f'Queued real offline speech job {identifier}', flush=True)
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                response = client.get(f'/api/studio/jobs/{identifier}')
                response.raise_for_status()
                job = response.json()
                if job['state'] not in report['states_seen']:
                    report['states_seen'].append(job['state'])
                    print(f'Queue state: {job["state"]} ({job.get("stage", "")})', flush=True)
                if job['state'] in {'done', 'error', 'cancelled', 'review'}:
                    break
                time.sleep(.5)
            report['job'] = job
            assert job['state'] == 'done', f'Job did not complete successfully: {job}'
            result = job['result']
            assert result['type'] == 'text_to_speech', result
            assert result['provider'] == 'vieneu' and result['voice_id'] == VOICE, result
            assert result['device'] == 'cuda', result
            assert result['primary_file'] == 'speech.wav', result
            assert {'speech.wav', 'speech.mp3'}.issubset(result['files']), result
            assert result['verification']['files_checked'] >= 2, result
            assert result['sample_rate'] == 48000 and result['duration'] > 0, result
            for filename in ('speech.wav', 'speech.mp3'):
                url = f'/media/job/{identifier}/{filename}'
                response = client.get(url)
                response.raise_for_status()
                assert len(response.content) > 100, filename
                target = OUT / ('queued-' + filename)
                target.write_bytes(response.content)
                report['outputs'][filename] = {'path': str(target), 'bytes': len(response.content),
                                              'sha256': hashlib.sha256(response.content).hexdigest()}
                unauthorized = httpx.get(f'http://127.0.0.1:{PORT}{url}', trust_env=False, timeout=10)
                assert unauthorized.status_code == 403, unauthorized.text
            with wave.open(str(OUT / 'queued-speech.wav'), 'rb') as audio:
                assert audio.getframerate() == 48000 and audio.getsampwidth() == 2
                assert audio.getnchannels() == 1 and audio.getnframes() > 0
                report['wav'] = {'sample_rate': audio.getframerate(), 'channels': audio.getnchannels(),
                                 'sample_width': audio.getsampwidth(),
                                 'duration': audio.getnframes() / audio.getframerate()}
            report['authenticated_downloads'] = True
            report['unauthenticated_downloads_blocked'] = True
            report['ok'] = True
        except BaseException as exc:
            caught = exc
            report['error'] = traceback.format_exc()
        finally:
            report['elapsed_seconds'] = round(time.monotonic() - started, 3)
            if report.get('job_id'):
                try:
                    log = client.get(f'/api/test/worker-log/{report["job_id"]}')
                    report['worker_log'] = log.json()['log']
                except Exception as exc:
                    report['worker_log_error'] = str(exc)
            try:
                client.post('/api/test/stop').raise_for_status()
                for _ in range(100):
                    if cleanup_marker.is_file():
                        report['cleanup'] = json.loads(cleanup_marker.read_text(encoding='utf-8'))
                        cleanup_marker.unlink()
                        break
                    time.sleep(.1)
                if not all(report.get('cleanup', {}).get(key) for key in
                           ('data_removed', 'queue_thread_stopped', 'active_worker_cleared')):
                    report['ok'] = False
                    report['cleanup_error'] = 'Isolated server cleanup was not confirmed.'
            except Exception as exc:
                report['ok'] = False
                report['cleanup_error'] = str(exc)
            (OUT / 'queue-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report.get(key) for key in ('ok', 'elapsed_seconds', 'states_seen', 'cleanup', 'outputs')},
                     ensure_ascii=False, indent=2), flush=True)
    if caught:
        raise caught
    assert report['ok'], report


if __name__ == '__main__':
    serve() if '--server' in sys.argv else acceptance()
