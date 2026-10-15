"""Independent voice studio browser acceptance, with isolated data and no inference.

Use the webapp-testing with_server.py helper to run this file with --serve on
port 19875, then run it normally as the browser client. The server does not
start the queue worker. A test-only endpoint finishes a job with a clearly
labelled synthetic tone WAV/MP3; the actual production /api/jobs endpoint is
used to validate and enqueue requests against the installed local catalog.
"""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
BASE = 'http://127.0.0.1:19875'
HEADERS = {'x-studio-token': 'npk-studio-test'}
OUT = ROOT / 'tests' / 'unified-voice-ui-results'


def serve():
    import math
    import os
    import struct
    import subprocess
    import tempfile
    import threading
    import time
    import wave

    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix='npk-unified-voice-ui-', dir=ROOT / 'tests') as isolated:
        os.environ['VIETDUB_DATA_DIR'] = isolated
        os.environ['VIETDUB_TOKEN'] = HEADERS['x-studio-token']
        import credentials
        fake = {}
        credentials.keyring.get_password = lambda service, key: fake.get(key)
        credentials.keyring.set_password = lambda service, key, value: fake.__setitem__(key, value)
        credentials.keyring.delete_password = lambda service, key: fake.pop(key, None)
        from core import DATA, binary, create_project, get, put
        import server
        import uvicorn

        http = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=19875,
                                             log_level='warning', access_log=False))

        @server.app.post('/api/test/stop')
        def stop():
            http.should_exit = True
            return {}

        @server.app.post('/api/test/finish-speech')
        def finish_speech(body: dict):
            job = get('job', body['id'])
            assert job and job['kind'] == 'text_to_speech' and job['state'] == 'queued'
            folder = DATA / 'jobs' / job['id']
            assert folder.is_dir()
            with wave.open(str(folder / 'speech.wav'), 'wb') as audio:
                audio.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
                audio.writeframes(b''.join(struct.pack('<h', int(1600 * math.sin(2 * math.pi * 440 * n / 48000)))
                                          for n in range(9600)))
            subprocess.run([binary('ffmpeg'), '-v', 'error', '-i', str(folder / 'speech.wav'),
                            '-c:a', 'libmp3lame', '-b:a', '128k', '-y', str(folder / 'speech.mp3')],
                           check=True, timeout=10, capture_output=True)
            result = dict(primary_file='speech.wav', files=['speech.wav', 'speech.mp3'],
                          sample_rate=48000, duration=.2, fixture=True)
            job.update(state='done', progress=1, result=result, ended=time.time(),
                       message='Âm kiểm thử tổng hợp, không phải kết quả suy luận giọng.')
            put('job', job['id'], job)
            return job

        @server.app.post('/api/test/create-video')
        def create_video():
            project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Video kiểm thử giọng độc lập')
            project['params'].update(use_clean=False, voice_gain=0, pipeline_translate=False,
                                     pipeline_tts=False, method='blur')
            project['segments'] = [dict(start=.5, end=2.5, text='Hello', translated='Xin chào bạn.')]
            put('project', project['id'], project)
            return {'id': project['id']}

        # There is deliberately no server.start_workers() here.
        timer = threading.Timer(600, lambda: setattr(http, 'should_exit', True))
        timer.daemon = True
        timer.start()
        try:
            http.run()
        finally:
            timer.cancel()


def browser_test():
    import httpx
    from playwright.sync_api import expect, sync_playwright

    OUT.mkdir(exist_ok=True)
    checks, errors, bridge_requests = [], [], []
    report = {'scope': 'isolated temporary DATA; installed local catalog; no queue worker or TTS inference; '
                       'test-only synthetic audio completion; no CIT or paid provider calls'}

    def stop_server():
        try:
            httpx.post(BASE + '/api/test/stop', headers=HEADERS, json={}, timeout=3)
        except httpx.HTTPError:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1680, 'height': 1080}, accept_downloads=True)
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('request', lambda request: bridge_requests.append(request.url)
                if ':8001' in request.url else None)
        try:
            page.goto(BASE + '/studio#token=npk-studio-test')
            page.wait_for_load_state('networkidle')
            # Reconnaissance on the real empty-library DOM before operating controls.
            page.screenshot(path=str(OUT / '00-empty-library.png'), full_page=True)
            report['reconnaissance'] = page.locator('body').inner_text()[:2500]
            assert page.evaluate('P === null')
            assert page.request.get(BASE + '/api/studio/state', headers=HEADERS).json()['projects'] == []
            page.locator('[data-area="text_to_speech"]').click()
            expect(page.get_by_role('textbox', name='Văn bản cần tạo giọng')).to_be_visible()
            expect(page.locator('#viewer')).to_be_hidden()
            assert page.evaluate('P === null')
            checks.append('Tạo giọng opens with no video and P=null in an empty project library')

            def stop_polling():
                page.evaluate('clearTimeout(pollTimer)')
                page.wait_for_function('!pollBusy')

            stop_polling()
            catalog = page.request.get(BASE + '/api/catalog', headers=HEADERS).json()
            native = [voice for voice in catalog['voices'] if voice['provider'] == 'vieneu']
            assert len(native) == 25, [(voice['id'], voice.get('available')) for voice in native]
            assert all(voice['installed'] and voice['available'] for voice in native), native
            assert all(voice['id'].startswith('vieneu:') for voice in native)
            assert 'CIT' not in page.locator('body').inner_text()
            native_id = next(voice['id'] for voice in native if voice['name'] == 'Hải Đăng')
            page.locator('#ttsVoice').select_option(native_id)
            expect(page.locator('#ttsExpressionTools')).to_be_visible()
            page.locator('#ttsText').fill('Xin chào bạn. Đây là bản nháp tạo giọng độc lập.')
            page.locator('#ttsSpeed').fill('1.1')
            page.locator('[data-tts-tag="[cười]"]').click()
            page.locator('[data-tts-tag="[nghỉ 1s]"]').click()
            draft = page.locator('#ttsText').input_value()
            assert '[cười]' in draft and '[nghỉ 1s]' in draft
            checks.append('All 25 installed local VieNeu presets appear; exact voice selection, speed and manual emotion/pause tags work')

            page.locator('[data-area="settings"]').click()
            page.locator('[data-area="text_to_speech"]').click()
            expect(page.locator('#ttsText')).to_have_value(draft)
            expect(page.locator('#ttsSpeed')).to_have_value('1.1')
            expect(page.locator('#ttsVoice')).to_have_value(native_id)
            page.locator('#ttsRefreshVoices').click()
            expect(page.locator('#ttsRefreshVoices')).to_be_enabled()
            expect(page.locator('#ttsText')).to_have_value(draft)
            page.reload()
            page.wait_for_load_state('networkidle')
            stop_polling()
            page.locator('[data-area="text_to_speech"]').click()
            expect(page.locator('#ttsText')).to_have_value(draft)
            expect(page.locator('#ttsVoice')).to_have_value(native_id)
            expect(page.locator('#ttsSpeed')).to_have_value('1.1')
            assert page.evaluate('P === null')
            checks.append('Draft, voice and speed survive tab changes, catalog refresh and browser reload without a video')

            piper = next(voice for voice in catalog['voices'] if voice['provider'] == 'piper' and voice['installed'])
            page.locator('#ttsVoice').select_option(piper['id'])
            expect(page.locator('#ttsExpressionTools')).to_be_hidden()
            page.locator('#ttsVoice').select_option(native_id)
            expect(page.locator('#ttsExpressionTools')).to_be_visible()
            checks.append('Emotion and pause insertion controls are specific to VieNeu; Piper remains available independently')

            def enqueue():
                with page.expect_response(lambda response: response.url == BASE + '/api/jobs'
                                          and response.request.method == 'POST') as response_info:
                    page.locator('#ttsGenerate').click()
                response = response_info.value
                assert response.ok, response.text()
                request = response.request.post_data_json
                assert request['kind'] == 'text_to_speech' and 'project_id' not in request, request
                assert request['sample_text'] == draft.strip(), request
                assert request['params'] == {'voice_id': native_id, 'voice_speed': 1.1, 'workload_mode': 'quiet', 'tts_device': 'auto', 'pronunciation_dictionary': ''}, request
                job = response.json()
                assert job['state'] == 'queued' and job.get('standalone'), job
                page.wait_for_function('(id) => standaloneSpeechDetail?.id === id', arg=job['id'])
                expect(page.locator('#ttsGenerate')).to_be_enabled()
                return job

            first = enqueue()
            expect(page.locator('#ttsCancel')).to_be_visible()
            page.locator('#ttsCancel').click()
            page.wait_for_function('standaloneSpeechDetail?.state === "cancelled"')
            expect(page.locator('#ttsText')).to_have_value(draft)
            stored = page.request.get(BASE + '/api/studio/jobs/' + first['id'], headers=HEADERS).json()
            assert stored['state'] == 'cancelled'
            checks.append('Production /api/jobs accepts the standalone payload without project_id; cancelling a queued job keeps the draft')

            second = enqueue()
            response = page.request.post(BASE + '/api/test/finish-speech', headers=HEADERS, data={'id': second['id']})
            assert response.ok, response.text()
            assert response.json()['result']['fixture']
            page.evaluate('poll()')
            expect(page.locator('#ttsAudio')).to_be_visible()
            page.wait_for_function('document.querySelector("#ttsAudio").readyState >= 1')
            duration = page.locator('#ttsAudio').evaluate('(audio) => audio.duration')
            assert .19 <= duration <= .21, duration
            page.locator('#ttsAudio').evaluate('(audio) => { audio.dataset.pollProof = "kept"; }')
            page.evaluate('poll()')
            expect(page.locator('#ttsAudio')).to_have_attribute('data-poll-proof', 'kept')
            for suffix, label in [('wav', 'Tải WAV'), ('mp3', 'Tải MP3')]:
                with page.expect_download() as download_info:
                    page.get_by_role('link', name=label).click()
                download_info.value.save_as(OUT / ('synthetic-test-audio.' + suffix))
            assert (OUT / 'synthetic-test-audio.wav').read_bytes().startswith(b'RIFF')
            mp3 = (OUT / 'synthetic-test-audio.mp3').read_bytes()
            assert mp3.startswith(b'ID3') or mp3[:1] == b'\xff', mp3[:20]
            page.screenshot(path=str(OUT / '01-standalone-voice-studio.png'), full_page=True)
            checks.append('Clearly labelled synthetic completion renders playable WAV plus real WAV/MP3 downloads; polling preserves the audio element')

            response = page.request.post(BASE + '/api/test/create-video', headers=HEADERS, data={})
            assert response.ok, response.text()
            page.evaluate('(id) => loadProject(id)', response.json()['id'])
            page.locator('[data-area="voice"]').click()
            expect(page.locator('#viewer')).to_be_visible()
            expect(page.locator('#voiceTimingMode')).to_be_visible()
            expect(page.locator('#voiceSearch')).to_be_visible()
            page.locator('#voiceSearch').fill('Hải Đăng')
            page.locator('[data-voice="' + native_id + '"]').click()
            for gain in page.locator('.param[data-key="voice_gain"]').all():
                expect(gain).to_have_value('1')
            page.locator('#voiceSourceBody details').filter(has=page.locator('#sampleText')).locator('summary').click()
            page.locator('#sampleText').fill('Câu nghe thử đang soạn phải được giữ.')
            page.locator('#refreshVoiceCatalog').click()
            expect(page.locator('#refreshVoiceCatalog')).to_be_enabled()
            expect(page.locator('#sampleText')).to_have_value('Câu nghe thử đang soạn phải được giữ.')
            assert 'CIT' not in page.locator('body').inner_text()
            page.screenshot(path=str(OUT / '02-video-voice-controls.png'), full_page=True)
            page.locator('[data-area="text_to_speech"]').click()
            expect(page.locator('#ttsText')).to_have_value(draft)
            expect(page.locator('#ttsAudio')).to_be_visible()
            checks.append('Existing video voice/timing controls still work, gain fields sync, and catalog refresh preserves an audition draft; no companion-app instructions')

            assert not errors, errors
            assert not bridge_requests, bridge_requests
            report.update(ok=True, checks=checks, page_errors=errors, bridge_requests=bridge_requests)
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            (OUT / 'failure.png').unlink(missing_ok=True)
            print('PASS', json.dumps(checks, ensure_ascii=True), flush=True)
        except Exception as error:
            page.screenshot(path=str(OUT / 'failure.png'), full_page=True)
            report.update(ok=False, checks=checks, page_errors=errors, bridge_requests=bridge_requests, error=str(error))
            (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            raise
        finally:
            browser.close()
            stop_server()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--serve', action='store_true', help='Start the isolated no-worker test API on port 19875')
    args = parser.parse_args()
    serve() if args.serve else browser_test()
