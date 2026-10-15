"""Opt-in real queue acceptance for local dubbing, paragraphs and own voices.

Every worker and API is production code. Only DATA and keyring storage are
isolated. The script owns its generated fixtures and leaves no user jobs.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import traceback
import wave

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tests/full-voice-upgrade-results'
PORT = 19866
TOKEN = 'npk-full-voice-upgrade-real-queue-test'
VOICE = 'vieneu:Hải Đăng'
SECOND = 'vieneu:Thùy Dung'
sys.path.insert(0, str(ROOT))


def serve():
    OUT.mkdir(exist_ok=True)
    temporary = tempfile.TemporaryDirectory(prefix='npk-full-voice-queue-', dir=ROOT / 'tests')
    owned_data = Path(temporary.name).resolve()
    assert owned_data.parent == (ROOT / 'tests').resolve()
    os.environ['VIETDUB_DATA_DIR'] = str(owned_data)
    os.environ['VIETDUB_TOKEN'] = TOKEN
    import credentials
    fake = {}
    credentials.keyring.get_password = lambda service, key: fake.get(key)
    credentials.keyring.set_password = lambda service, key, value: fake.__setitem__(key, value)
    credentials.keyring.delete_password = lambda service, key: fake.pop(key, None)
    import server
    import uvicorn
    from core import DATA, get, binary, run
    source = ROOT / 'tests/speaker-groups-results/two-presets.wav'
    if not source.is_file():
        raise ValueError('Run own generated speaker fixture acceptance first.')
    video = OUT / 'fixture.mp4'
    run([binary('ffmpeg'), '-v', 'error', '-y', '-f', 'lavfi', '-i',
         'color=c=navy:s=320x180:r=15:d=18', '-i', source, '-af', 'apad',
         '-t', '18', '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1',
         '-c:a', 'aac', '-b:a', '128k', video], timeout=30)
    http = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=PORT,
                                        log_level='warning', access_log=False))

    @server.app.get('/api/test/info')
    def info():
        return {'pid': os.getpid(), 'data': str(DATA), 'video': str(video)}

    @server.app.get('/api/test/worker-log/{identifier}')
    def worker_log(identifier: str):
        if not get('job', identifier):
            return {'log': ''}
        path = DATA / 'jobs' / identifier / 'worker.log'
        return {'log': path.read_text(encoding='utf-8', errors='replace')[-30000:] if path.is_file() else ''}

    @server.app.post('/api/test/stop')
    def stop():
        http.should_exit = True
        return {'stopping': True}

    timer = threading.Timer(600, lambda: setattr(http, 'should_exit', True))
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
    import psutil
    OUT.mkdir(exist_ok=True)
    marker = OUT / '.queue-cleanup.json'
    marker.unlink(missing_ok=True)
    report = {'ok': False, 'inference': 'Real NPK local CPU/CUDA; no remote API', 'jobs': []}
    began, caught = time.monotonic(), None
    with httpx.Client(base_url=f'http://127.0.0.1:{PORT}', headers={'x-studio-token': TOKEN},
                      trust_env=False, timeout=25) as client:
        def request(method, url, **kwargs):
            response = client.request(method, url, **kwargs)
            assert response.is_success, f'{method} {url}: {response.status_code} {response.text}'
            return response.json()

        info = request('GET', '/api/test/info')
        parent = psutil.Process(info['pid'])
        assert Path(info['data']).parent == ROOT / 'tests'
        assert Path(info['data']).name.startswith('npk-full-voice-queue-')
        report['isolated_data'] = info['data']

        def run_job(label, body, expected_native=None):
            tick = time.monotonic()
            job = request('POST', '/api/jobs', json=body)
            native, states = {}, ['queued']
            deadline = tick + 150
            while time.monotonic() < deadline:
                try:
                    for process in parent.children(recursive=True):
                        try:
                            if process.name().lower() == 'audiocpp_cli.exe':
                                native[process.pid] = process.cmdline()
                        except (psutil.NoSuchProcess, psutil.AccessDenied):
                            pass
                except psutil.NoSuchProcess:
                    break
                job = request('GET', '/api/studio/jobs/' + job['id'])
                if job['state'] not in states:
                    states.append(job['state'])
                if job['state'] in {'done', 'error', 'cancelled', 'review'}:
                    break
                time.sleep(.1)
            log = request('GET', '/api/test/worker-log/' + job['id'])['log']
            item = dict(label=label, elapsed_seconds=round(time.monotonic() - tick, 3), states_seen=states,
                        native_process_count=len(native), native_commands=list(native.values()), job=job, worker_log=log)
            report['jobs'].append(item)
            print(json.dumps({key: item[key] for key in ('label', 'elapsed_seconds', 'states_seen', 'native_process_count')}, ensure_ascii=False), flush=True)
            assert job['state'] == 'done', f'{label}: {job}\n{log}'
            if expected_native is not None:
                assert len(native) == expected_native, f'{label}: expected {expected_native} model sessions, got {len(native)}'
            assert job['result']['verification']['files_checked'] >= 1
            assert 'queued' in states and 'running' in states and 'done' in states
            for filename in job['result']['files']:
                response = client.get(f'/media/job/{job["id"]}/{filename}')
                response.raise_for_status()
                target = OUT / (label + '-' + filename)
                target.write_bytes(response.content)
                item.setdefault('outputs', {})[filename] = dict(path=str(target), bytes=len(response.content),
                    sha256=hashlib.sha256(response.content).hexdigest())
            return job

        try:
            project = request('POST', '/api/projects', json={'path': info['video'], 'name': 'NPK isolated voice queue fixture'})
            pid = project['id']
            params = dict(voice_id=VOICE, tts_device='cuda', workload_mode='balanced',
                          tts_timing_mode='natural', pronunciation_dictionary='NPK => en pi cây', use_clean=False)
            rows = [dict(start=0, end=6, text='Xin chào.', translated='Xin chào, đây là NPK.'),
                    dict(start=6, end=12, text='Tiếng Việt.', translated='Giọng Việt thật tự nhiên.'),
                    dict(start=12, end=18, text='Hẹn gặp lại.', translated='Cảm ơn và hẹn gặp lại.')]
            project = request('PUT', f'/api/projects/{pid}', json={'params': params, 'segments': rows})
            first = run_job('video-same-voice', {'kind': 'tts', 'project_id': pid}, 1)
            assert first['result']['cache_hits'] == 0 and first['result']['generated'] == 3
            assert first['result'].get('speech_signature')
            with wave.open(str(OUT / 'video-same-voice-voice.wav'), 'rb') as audio:
                assert audio.getframerate() == 48000 and abs(audio.getnframes() / 48000 - 18) < .001
            for index, row in enumerate(rows):
                row['translated'] = ['Hôm nay trời rất đẹp.', 'Mời bạn xem sản phẩm này.', 'Xin cảm ơn các bạn.'][index]
                row['voice_id'] = SECOND if index == 1 else VOICE
            project = request('PUT', f'/api/projects/{pid}', json={'segments': rows})
            second = run_job('video-two-voices', {'kind': 'tts', 'project_id': pid}, 2)
            assert second['result']['generated'] == 3
            assert second['result']['speech_signature'] != first['result']['speech_signature']

            paragraphs = [dict(text='Chào bạn, đây là bản đọc theo đoạn.', voice_id=VOICE, rate=1, pause_after_ms=350),
                          dict(text='Tôi đọc phần tiếp theo với giọng nữ.', voice_id=SECOND, rate=1, pause_after_ms=700),
                          dict(text='Chúc bạn có một ngày làm việc vui vẻ.', voice_id=VOICE, rate=1.05, pause_after_ms=0)]
            narration = run_job('narration-original', {'kind': 'text_to_speech', 'narration_segments': paragraphs, 'params': params}, 2)
            assert narration['result']['generated'] == 3 and narration['result']['cache_hits'] == 0
            chunks = narration['result']['chunks']
            assert len(chunks) == 3 and narration['result']['voice_id'] == 'multiple'
            assert abs(chunks[1]['start'] - chunks[0]['end'] - .35) < 1 / 48000
            assert abs(chunks[2]['start'] - chunks[1]['end'] - .7) < 1 / 48000
            paragraphs[1]['text'] = 'Đoạn này được chỉnh sửa, chỉ tạo lại riêng tôi.'
            changed = run_job('narration-edited', {'kind': 'text_to_speech', 'narration_segments': paragraphs, 'params': params}, 1)
            assert changed['result']['cache_hits'] == 2 and changed['result']['generated'] == 1
            assert [row['cache_hit'] for row in changed['result']['chunks']] == [True, False, True]
            assert changed['id'] != narration['id']
            for label, result in [('narration-original', narration), ('narration-edited', changed)]:
                with wave.open(str(OUT / (label + '-speech.wav')), 'rb') as audio:
                    assert audio.getframerate() == 48000 and audio.getnchannels() == 1
                    for index, chunk in enumerate(result['result']['chunks'][:-1]):
                        audio.setpos(round(chunk['end'] * 48000))
                        assert not any(audio.readframes(round(chunk['pause_after_ms'] / 1000 * 48000)))

            cpu = run_job('quiet-cpu', {'kind': 'text_to_speech', 'sample_text': 'NPK chạy giọng đọc ngay trên máy.',
                          'params': params | {'tts_device': 'cpu', 'workload_mode': 'quiet'}}, 1)
            assert cpu['result']['device'] == 'cpu' and cpu['result']['threads'] == 1
            assert cpu['result']['native_batches'] == 1 and cpu['result']['chunks']

            enrollment = run_job('enrollment', {'kind': 'enroll_voice', 'name': 'NPK acceptance own voice',
                'reference_path': str(ROOT / 'tests/vieneu-independent-results/NPK-text-to-speech.wav'),
                'reference_duration': 6, 'params': {'tts_device': 'cpu', 'workload_mode': 'quiet'}}, 1)
            own_id = enrollment['result']['voice_id']
            assert own_id.startswith('vieneu:custom:')
            catalog = request('GET', '/api/catalog')
            assert any(row['id'] == own_id and row.get('is_custom') for row in catalog['voices'])
            own = run_job('enrolled-speech', {'kind': 'text_to_speech', 'sample_text': 'Đây là giọng riêng vừa được lưu trong NPK.',
                'params': params | {'voice_id': own_id}}, 1)
            assert own['result']['voice_id'] == own_id

            # The original video audio contains male / female / male generated
            # speech fixtures. These review-only jobs must not change cue voices.
            # The male fixture is shorter than four seconds. Match the actual
            # concat recipe instead of cutting across two different speakers.
            source_rows, cursor = [], 0
            fixtures = ROOT / 'tests/vieneu-independent-results'
            for index, (fixture, voice, text) in enumerate([
                    ('NPK-Hai-Dang.wav', VOICE, 'Một giọng nam đang nói.'),
                    ('NPK-text-to-speech.wav', SECOND, 'Một giọng nữ đang nói.'),
                    ('NPK-Hai-Dang.wav', VOICE, 'Giọng nam xuất hiện lại.')]):
                with wave.open(str(fixtures / fixture), 'rb') as audio:
                    frames = min(audio.getnframes(), 4 * 48000)
                source_rows.append(dict(start=cursor / 48000, end=(cursor + frames) / 48000,
                                        text=text, translated=text, voice_id=voice))
                cursor += frames + 12000
            request('PUT', f'/api/projects/{pid}', json={'segments': source_rows, 'params': {'workload_mode': 'quiet'}})
            analysis = run_job('source-audio-analysis', {'kind': 'analyze_audio', 'project_id': pid}, 0)
            assert len(analysis['result']['insights']['cues']) == 3
            groups = run_job('speaker-groups', {'kind': 'speaker_groups', 'project_id': pid}, 0)
            suggestion = [row['group_id'] for row in groups['result']['suggestions']]
            assert suggestion[0] == suggestion[2] and suggestion[0] != suggestion[1], suggestion
            assert groups['result']['automatic_voice_assignment'] is False
            updated = request('GET', '/api/studio/projects/' + pid)
            assert [row.get('voice_id') for row in updated['segments']] == [VOICE, SECOND, VOICE]
            report['ok'] = True
        except BaseException as exc:
            caught = exc
            report['error'] = traceback.format_exc()
        finally:
            try:
                request('POST', '/api/test/stop')
                for _ in range(100):
                    if marker.is_file():
                        report['cleanup'] = json.loads(marker.read_text(encoding='utf-8'))
                        marker.unlink()
                        break
                    time.sleep(.1)
                if not all(report.get('cleanup', {}).get(key) for key in
                           ('data_removed', 'queue_thread_stopped', 'active_worker_cleared')):
                    report['ok'] = False
                    report['cleanup_error'] = 'Isolated cleanup not confirmed.'
            except Exception as exc:
                report['ok'] = False
                report['cleanup_error'] = str(exc)
            report['elapsed_seconds'] = round(time.monotonic() - began, 3)
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({key: report.get(key) for key in ('ok', 'elapsed_seconds', 'cleanup', 'error')}, ensure_ascii=False), flush=True)
    if caught:
        raise caught
    assert report['ok'], report


if __name__ == '__main__':
    serve() if '--server' in sys.argv else acceptance()
