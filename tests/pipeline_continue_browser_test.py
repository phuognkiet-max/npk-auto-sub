"""Paused full-pipeline UX against isolated DATA and real continuation APIs.

Stage engines are fixture stubs. No user data, provider calls, queue worker,
OCR/TTS model or GPU is used. WAV bytes and copied MP4 are actually verified.
"""
import asyncio
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
from unittest.mock import patch
import uuid
import wave

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'build/pipeline-continue-browser'
PORT = 19882
TOKEN = 'npk-pipeline-continue-isolated-fixture'
sys.path.insert(0, str(ROOT))


def serve():
    OUT.mkdir(parents=True, exist_ok=True)
    cleanup = OUT / 'fixture-cleanup.json'
    cleanup.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='npk-pipeline-continue-browser-', dir=ROOT / 'tests') as temporary:
        os.environ['VIETDUB_DATA_DIR'] = temporary
        os.environ['VIETDUB_TOKEN'] = TOKEN
        import credentials
        credentials.keyring.get_password = lambda *args: None
        credentials.active_secret = lambda *args: 'fixture-only-never-sent-to-provider'
        from core import DATA, create_project, get, put, save_srt, signature
        from subtitle_quality import coverage_record
        from job_progress import verify_outputs
        import server
        import worker
        import uvicorn
        project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Paused full workflow fixture')
        project['source_revision'] = uuid.uuid4().hex
        (DATA / 'exports').mkdir(exist_ok=True)
        project['params'].update(pipeline_extract='ocr', pipeline_translate=True,
            pipeline_tts=True, pipeline_clean=True, use_clean=True, burn_subtitles=True,
            voice_gain=1, original_gain=0, voice_id='piper:vais1000', tts_device='cpu',
            tts_text_source='translated', subtitle_flow='wrap', subtitle_preset='clean',
            subtitle_karaoke=False, font_size=24, margin=20, method='blur',
            export_directory=str(DATA / 'exports'), export_name='Continued fixture')
        project['regions'] = [dict(x=20, y=280, w=600, h=60, start=0, end=8)]
        project['segments'] = [dict(start=0, end=.6, text='啊', translated=''),
            dict(start=.6, end=4., text='完整保留所有字幕', translated=''),
            dict(start=4., end=8., text='谢谢，再见', translated='')]
        calls, opened, translated_indices = [], [], []
        delay = {'seconds': 0}
        original_open = getattr(os, 'startfile', None)
        os.startfile = lambda path: opened.append(str(Path(path).resolve()))
        job_id = uuid.uuid4().hex
        job = dict(id=job_id, project_id=project['id'], project_name=project['name'],
            kind='pipeline', created=time.time(), started=time.time() - 10,
            ended=time.time(), state='review', progress=1, stage='done',
            message='Cần kiểm tra phụ đề trước khi tiếp tục', project_applied=True,
            project_revision=project['updated'], source_revision=project['source_revision'],
            configuration=copy.deepcopy(project['params']), events=[dict(stage='ocr', message='Đã đọc chữ trong toàn bộ video')])
        folder = DATA / 'jobs' / job_id
        folder.mkdir(parents=True)
        (folder / 'input.json').write_text(json.dumps({'project': project, 'job': job}, ensure_ascii=False), encoding='utf-8')
        project['coverage'] = coverage_record(project, 'ocr', 0, 8, clipped_samples=[.3])
        save_srt(project['segments'], folder / 'source.srt', translated=False)
        job['result'] = dict(type='pipeline', files=['source.srt'],
            segments=copy.deepcopy(project['segments']), coverage=copy.deepcopy(project['coverage']),
            needs_review=True, steps=['ocr'], completed_steps=['ocr'],
            remaining_steps=['translate', 'tts', 'clean', 'render'],
            verification={'files_checked': 1}, warnings=['Advisory OCR hint'])
        project['artifacts']['pipeline'] = job['result'] | {'job_id': job_id}
        put('job', job_id, job)
        put('project', project['id'], project)
        http = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=PORT,
            access_log=False, log_level='warning', timeout_graceful_shutdown=2))

        @server.app.middleware('http')
        async def delayed_plan(request, call_next):
            latency = delay['seconds'] if request.url.path.endswith('/pipeline-continuation') else 0
            response = await call_next(request)
            if latency:
                await asyncio.sleep(latency)
            return response

        @server.app.get('/api/test/info')
        def info():
            return {'project_id': project['id'], 'job_id': job_id, 'calls': calls, 'opened': opened, 'translated_indices': translated_indices}

        @server.app.post('/api/test/plan-delay')
        def plan_delay(body: dict):
            delay['seconds'] = body['seconds']
            return {}

        @server.app.post('/api/test/old-source-final')
        def old_source_final(body: dict):
            previous = copy.deepcopy(job)
            previous.update(id=uuid.uuid4().hex, created=job['created'] - 10,
                source_revision=project['source_revision'] if body.get('same_source') else None,
                state='done', result={'files': ['final.mp4'], 'primary_file': 'final.mp4'})
            destination = DATA / 'jobs' / previous['id']
            destination.mkdir()
            shutil.copyfile(project['media']['path'], destination / 'final.mp4')
            put('job', previous['id'], previous)
            row = get('project', project['id'])
            row['artifacts']['render'] = previous['result'] | {'job_id': previous['id']}
            row['updated'] = time.time()
            put('project', row['id'], row)
            return previous

        @server.app.post('/api/test/coverage')
        def coverage(body: dict):
            row = get('project', project['id'])
            row['coverage'] = coverage_record(row, 'ocr', 0, body['end'], clipped_samples=[.3])
            # Both the original recognition result and editor are the same
            # deliberately partial evidence; no review acknowledgement is forged.
            previous = get('job', job_id)
            previous['result']['coverage'] = copy.deepcopy(row['coverage'])
            row['artifacts']['pipeline']['coverage'] = copy.deepcopy(row['coverage'])
            row.pop('subtitle_review', None)
            row['updated'] = time.time()
            put('job', job_id, previous)
            put('project', row['id'], row)
            return row

        def recognize(*args):
            raise AssertionError('Continuation unexpectedly restarted OCR/ASR')

        def translate(row, step, destination):
            calls.append('translate')
            translated_indices.extend(index for index,cue in enumerate(row['segments']) if not cue['translated'])
            rows = [dict(cue, translated=cue['translated'] or f'Lời dịch đầy đủ {index + 1}.')
                    for index, cue in enumerate(row['segments'])]
            save_srt(rows, destination / 'translated.srt')
            return {'type': 'translate', 'files': ['translated.srt'], 'segments': rows}

        def synthesize(row, step, destination):
            calls.append('tts')
            assert all(cue['translated'] for cue in row['segments'])
            with wave.open(str(destination / 'voice.wav'), 'wb') as stream:
                stream.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                stream.writeframes(b'\x10\x00' * round(row['media']['duration'] * 16000))
            return {'type': 'tts', 'files': ['voice.wav'], 'sample_rate': 16000,
                    'speech_signature': worker.speech_signature(row)}

        def clean(row, step, destination):
            calls.append('clean')
            shutil.copyfile(row['media']['path'], destination / 'clean.mp4')
            return {'type': 'clean', 'files': ['clean.mp4'], 'signature': signature(row)}

        def render(row, step, destination):
            calls.append('render')
            shutil.copyfile(row['media']['path'], destination / 'final.mp4')
            return {'type': 'render', 'files': ['final.mp4'], 'primary_file': 'final.mp4'}

        @server.app.post('/api/test/fail-translation')
        def fail_translation(body: dict):
            from translation_checkpoint import write_checkpoint
            current=get('job',body['id'])
            destination=DATA/'jobs'/current['id']
            snapshot=json.loads((destination/'input.json').read_text(encoding='utf-8'))['project']
            rows=copy.deepcopy(snapshot['segments']);rows[0]['translated']='Bản dịch đã lưu trong checkpoint.'
            write_checkpoint(destination/'translation-checkpoint.json',snapshot,rows,{0})
            current.update(state='error',stage='translate',progress=.3,ended=time.time(),message='HTTP 429 fixture quota')
            put('job',current['id'],current)
            return current

        @server.app.post('/api/test/terminal-history')
        def terminal_history(body: dict):
            current = get('job', body['id'])
            assert current['project_id'] == project['id']
            assert body['state'] in ('error', 'cancelled')
            current.update(state=body['state'], ended=time.time(),
                message=('Hết thời gian chờ dịch vụ. Đã thử 3 lần.' if body['state'] == 'error'
                         else 'Người dùng đã hủy tác vụ trước lần thử tiếp theo.'),
                events=[dict(stage='prepare', message='Đã kiểm tra đầu vào.'),
                        dict(stage='translate', message='Đang chờ thử lại dịch vụ trong 4.4 giây.')])
            put('job', current['id'], current)
            return current

        @server.app.post('/api/test/complete')
        def complete(body: dict):
            current = get('job', body['id'])
            destination = DATA / 'jobs' / current['id']
            snapshot = json.loads((destination / 'input.json').read_text(encoding='utf-8'))['project']
            assert snapshot['params']['pipeline_extract'] == 'existing'
            assert current['continuation_from'] == job_id
            with patch.dict(worker.TASKS, {'ocr': recognize, 'asr': recognize,
                    'translate': translate, 'tts': synthesize, 'clean': clean, 'render': render}), \
                    patch('worker.emit'), contextlib.redirect_stdout(io.StringIO()):
                result = worker.pipeline(snapshot, current, destination)
                result['verification'] = verify_outputs(result, snapshot, current, destination)
            from exports import publish_or_keep_local
            result = publish_or_keep_local(snapshot, destination, result)
            with server.state_lock:
                applied = server.attach_completed_result(current, result)
                current.update(state='done', stage='done', progress=1, ended=time.time(),
                    result=result, project_applied=applied)
                put('job', current['id'], current)
            return current

        @server.app.get('/api/test/snapshot/{identifier}')
        def snapshot(identifier: str):
            assert get('job', identifier)['project_id'] == project['id']
            return json.loads((DATA / 'jobs' / identifier / 'input.json').read_text(encoding='utf-8'))

        @server.app.post('/api/test/stop')
        def stop():
            http.should_exit = True
            return {}

        timer = threading.Timer(240, lambda: setattr(http, 'should_exit', True))
        timer.daemon = True
        timer.start()
        try:
            http.run()
        finally:
            timer.cancel()
            if original_open is not None:
                os.startfile = original_open
    cleanup.write_text(json.dumps({'data_removed': not Path(temporary).exists(),
        'queue_workers_started': False, 'inference_models_loaded': False,
        'gpu_work': False}), encoding='utf-8')


def browser():
    import httpx
    from playwright.sync_api import sync_playwright, expect
    OUT.mkdir(parents=True, exist_ok=True)
    base = f'http://127.0.0.1:{PORT}'
    headers = {'x-studio-token': TOKEN}
    checks, errors, fresh_jobs, continue_requests = [], [], [], []
    try:
        with sync_playwright() as pw:
            app = pw.chromium.launch(channel='msedge', headless=True)
            page = app.new_page(viewport={'width': 1440, 'height': 1050})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: fresh_jobs.append(request.post_data_json)
                if request.method == 'POST' and request.url.endswith('/api/jobs') else
                continue_requests.append(request.post_data_json) if request.method == 'POST'
                and request.url.endswith('/continue-pipeline') else None)
            try:
                page.goto(base + '/studio#token=' + TOKEN)
                page.wait_for_load_state('networkidle')
                info = page.request.get(base + '/api/test/info', headers=headers).json()
                identifier, original = info['project_id'], info['job_id']
                page.locator('#setupTop').click()
                expect(page.locator('#setupPipelineState')).to_contain_text('Chưa có video thành phẩm')
                expect(page.locator('#setupRun')).to_have_text('Tiếp tục các bước còn lại')
                expect(page.locator('#setupRun')).to_be_enabled()
                assert page.locator('[data-stage-state="done"]').count() == 1
                assert page.locator('[data-stage-state="pending"]').count() == 4
                expect(page.locator('#setupFinalResult')).to_be_hidden()
                assert not page.locator('#viewSource option[value="final"]').count()
                expect(page.locator('#progressText')).to_contain_text('Chưa xuất video')
                checks.append('Paused full setup shows saved OCR plus four unfinished stages, explicit no-final status and one-click continuation')

                assert page.request.post(base + '/api/test/old-source-final', headers=headers, data={}).ok
                page.reload(); page.wait_for_load_state('networkidle'); page.locator('#setupTop').click()
                expect(page.locator('#setupRun')).to_be_enabled()
                expect(page.locator('#setupFinalResult')).to_be_hidden()
                checks.append('A legacy final job without the current source revision is never shown as the result of a newly selected source')

                page.evaluate('showJob(' + json.dumps(original) + ')')
                expect(page.locator('#continuePipelineCurrent')).to_be_enabled()
                assert not page.locator('#jobFinal').count()
                assert not page.locator('#jobFinalFolder').count()
                assert page.request.post(base + '/api/studio/jobs/' + original + '/open-result-folder', headers=headers, data={}).status == 400
                checks.append('Review job details offer continuation without inventing a final video or allowing a result-folder open')

                assert page.request.post(base + '/api/test/coverage', headers=headers, data={'end': 4}).ok
                page.reload(); page.wait_for_load_state('networkidle'); page.locator('#setupTop').click()
                expect(page.locator('#setupRun')).to_be_disabled()
                expect(page.locator('#setupReview')).to_be_visible()
                assert not page.evaluate('P.subtitle_review')
                page.locator('#setupReview').click()
                assert page.evaluate('subtab') == 'quality'
                assert not fresh_jobs and not continue_requests
                checks.append('True partial coverage still blocks continuation and links to actual subtitle review without automatic acknowledgement')

                assert page.request.post(base + '/api/test/coverage', headers=headers, data={'end': 8}).ok
                page.reload(); page.wait_for_load_state('networkidle'); page.locator('#setupTop').click()
                expect(page.locator('#setupRun')).to_be_enabled()
                assert page.request.post(base + '/api/test/plan-delay', headers=headers, data={'seconds': .6}).ok
                page.locator('#setupOutputName').fill('Manual edit preserved')
                page.locator('#setupOutputName').dispatch_event('change')
                page.locator('#nav [data-area="voice"]').click()
                page.locator('#setupTop').click()
                expect(page.locator('#setupRun')).to_be_enabled()
                assert not errors, errors
                assert page.request.post(base + '/api/test/plan-delay', headers=headers, data={'seconds': 0}).ok
                checks.append('Delayed continuation inspection survives setup unmount/remount and preserves current output preferences')

                previous=page.request.post(base + '/api/test/old-source-final', headers=headers, data={'same_source': True}).json()
                page.reload();page.wait_for_load_state('networkidle');page.locator('#setupTop').click()
                expect(page.locator('#setupFinalResult')).to_contain_text('Video hoàn tất của lượt trước')
                page.wait_for_function('document.querySelector("#viewSource option[value=final]")')
                page.locator('#viewSource').select_option('final')
                assert '/media/job/'+previous['id']+'/final.mp4' in page.locator('#video').get_attribute('src')

                for width in (1440, 1024, 720, 400):
                    page.set_viewport_size({'width': width, 'height': 1050})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
                page.set_viewport_size({'width': 1440, 'height': 1050})
                page.screenshot(path=str(OUT / 'paused-ready.png'), full_page=True)
                with page.expect_response(lambda response: response.url.endswith('/continue-pipeline') and response.request.method == 'POST') as response:
                    page.locator('#setupRun').click()
                resumed = response.value.json()
                assert resumed['kind'] == 'pipeline' and resumed['continuation_from'] == original
                expect(page.locator('#setupRun')).to_be_disabled()
                snapshot = page.request.get(base + '/api/test/snapshot/' + resumed['id'], headers=headers).json()
                assert snapshot['project']['params']['pipeline_extract'] == 'existing'
                assert snapshot['project']['params']['export_name'] == 'Manual edit preserved'
                assert snapshot['project']['params']['pipeline_translate']
                assert snapshot['project']['params']['pipeline_tts']
                assert snapshot['project']['params']['pipeline_clean']
                assert page.evaluate('P.params.pipeline_extract') == 'ocr'
                assert not fresh_jobs and len(continue_requests) == 1
                checks.append('Original Run continues one revision-bound job using existing cues, keeps full steps and manual settings, leaves editor OCR choice unchanged')

                page.wait_for_function('!pollBusy')
                assert page.request.post(base+'/api/test/fail-translation',headers=headers,data={'id':resumed['id']}).ok
                page.evaluate('poll()')
                expect(page.locator('#setupRun')).to_have_text('Tiếp tục các bước còn lại')
                expect(page.locator('#setupRun')).to_be_enabled()
                expect(page.locator('#setupPipelineState')).to_contain_text('Giữ 1 câu dịch đã lưu')
                checks.append('A terminal translation error releases the live setup screen immediately and exposes checkpoint continuation without a reload')
                for state, terminal_text, history_label in (
                        ('error', 'Hết thời gian chờ dịch vụ. Đã thử 3 lần.', 'Nhật ký trước khi dừng'),
                        ('cancelled', 'Người dùng đã hủy tác vụ trước lần thử tiếp theo.', 'Nhật ký trước khi hủy')):
                    assert page.request.post(base + '/api/test/terminal-history', headers=headers,
                        data={'id': resumed['id'], 'state': state}).ok
                    page.evaluate('showJob(' + json.dumps(resumed['id']) + ')')
                    page.evaluate('setProgressExpanded(true)')
                    expect(page.locator('[data-job-terminal-message="' + state + '"]')).to_contain_text(terminal_text)
                    expect(page.locator('[data-job-status="' + state + '"]')).to_contain_text('Không còn xử lý')
                    assert not page.locator('#cancelCurrent').count()
                    expect(page.locator('[data-job-event-history] .step').last).to_be_hidden()
                    assert 'Đang chờ thử lại dịch vụ' not in page.locator('#jobDetails').inner_text()
                    expect(page.locator('[data-job-event-history] > summary')).to_have_text(history_label)
                    page.locator('[data-job-event-history] > summary').click()
                    expect(page.locator('[data-job-event-history] .step').last).to_contain_text('Đang chờ thử lại dịch vụ')
                    expect(page.locator('[data-job-event-history] .step').last).to_be_visible()
                    page.locator('[data-job-event-history] > summary').click()
                    checks.append('Terminal ' + state + ' presents the actual outcome, no active cancellation and a collapsed labelled past retry history')
                assert page.request.post(base + '/api/test/terminal-history', headers=headers,
                    data={'id': resumed['id'], 'state': 'error'}).ok
                page.evaluate('setProgressExpanded(false)')
                page.evaluate('async()=>{change(row=>row.segments[1].translated="Bản dịch chỉnh tay được giữ.");await save();renderPanel();}')
                expect(page.locator('#setupRun')).to_be_enabled()
                page.evaluate('showJob('+json.dumps(resumed['id'])+')')
                page.evaluate('setProgressExpanded(true)')
                expect(page.locator('#continuePipelineCurrent')).to_be_enabled()
                expect(page.locator('#retryCurrent')).to_be_hidden()
                with page.expect_response(lambda response: response.url.endswith('/continue-pipeline') and response.request.method=='POST') as response:
                    page.locator('#continuePipelineCurrent').click()
                recovered=response.value.json()
                assert recovered['recovery_from']==resumed['id']
                resumed=recovered
                snapshot=page.request.get(base+'/api/test/snapshot/'+resumed['id'],headers=headers).json()
                assert snapshot['project']['segments'][0]['translated']=='Bản dịch đã lưu trong checkpoint.'
                assert snapshot['project']['segments'][1]['translated']=='Bản dịch chỉnh tay được giữ.'
                assert snapshot['project']['params']['pipeline_extract']=='existing'
                page.evaluate('setProgressExpanded(false)')
                checks.append('A failed translation child offers continuation from its checkpoint, restores saved translations only into blanks and preserves newer manual edits')

                done = page.request.post(base + '/api/test/complete', headers=headers,
                    data={'id': resumed['id']}, timeout=120000)
                assert done.ok, done.text()
                final = done.json()
                page.wait_for_function('!pollBusy')
                page.evaluate('poll()')
                expect(page.locator('#setupFinalResult')).to_be_visible()
                expect(page.locator('#setupFinalResult')).to_contain_text('Đã xuất video thành phẩm')
                path = final['result']['published']['path']
                expect(page.locator('.setup-result-path').first).to_contain_text(path)
                page.locator('#setupOpenFinal').click()
                page.locator('#setupOpenFinalFolder').click()
                page.wait_for_load_state('networkidle')
                after = page.request.get(base + '/api/test/info', headers=headers).json()
                assert after['calls'] == ['translate', 'tts', 'clean', 'render']
                assert after['translated_indices']==[2]
                assert after['opened'] == [str(Path(path).resolve()), str(Path(path).parent.resolve())]
                assert page.request.get(base + '/api/studio/projects/' + identifier + '/pipeline-continuation', headers=headers).json()['job_id'] is None
                page.wait_for_function('latestFinalArtifact()?.job_id === ' + json.dumps(resumed['id']))
                page.locator('#viewSource').select_option('final')
                assert '/media/job/'+resumed['id']+'/final.mp4' in page.locator('#video').get_attribute('src')
                checks.append('The latest verified pipeline final supersedes an older render artifact in the video dropdown as well as the result card')
                page.screenshot(path=str(OUT / 'final-visible.png'), full_page=True)
                checks.append('Remaining stages complete without OCR, actual verified final path is visible, video/folder actions open that exact published result')
                duplicate=page.request.post(base+'/api/studio/projects/'+identifier+'/duplicate',headers=headers,data={})
                assert duplicate.ok,duplicate.text()
                duplicate=duplicate.json()
                assert duplicate['artifacts']['pipeline']['source_project_id']==identifier
                page.evaluate('loadProject('+json.dumps(duplicate['id'])+')')
                page.locator('#setupTop').click()
                expect(page.locator('#setupFinalResult')).to_contain_text(path)
                page.wait_for_function('latestFinalArtifact()?.job_id === '+json.dumps(resumed['id']))
                page.locator('#viewSource').select_option('final')
                assert '/media/job/'+resumed['id']+'/final.mp4' in page.locator('#video').get_attribute('src')
                replacement=page.request.post(base+'/api/studio/projects/'+duplicate['id']+'/replace-source',headers=headers,data={
                    'expected_revision':page.evaluate('P.updated'),'path':str(ROOT/'tests/fixtures/demo.mp4'),'mode':'keep'})
                assert replacement.ok,replacement.text()
                page.evaluate('loadProject('+json.dumps(duplicate['id'])+',true)')
                page.locator('#setupTop').click()
                expect(page.locator('#setupFinalResult')).to_be_hidden()
                assert not page.locator('#viewSource option[value="final"]').count()
                checks.append('Official Duplicate retains a verified shared final through server provenance, while Replace Source removes access to that old final')
                page.evaluate('loadProject('+json.dumps(identifier)+')')
                page.locator('#setupTop').click();expect(page.locator('#setupRun')).to_be_enabled()
                with page.expect_response(lambda response: response.url.endswith('/api/jobs') and response.request.method == 'POST') as response:
                    page.locator('#setupRun').click()
                assert response.value.json()['kind'] == 'pipeline'
                expect(page.locator('#setupFinalResult')).to_be_hidden()
                expect(page.locator('#setupPipelineState')).to_contain_text('Chưa có video mới')
                checks.append('Starting a deliberate new full run hides the previous success card and clearly says a new final is not ready yet')
                assert not errors, errors
            finally:
                app.close()
    finally:
        (OUT / 'report.json').write_text(json.dumps({'checks': checks, 'javascript_errors': errors,
            'fresh_job_requests': len(fresh_jobs), 'continuation_requests': len(continue_requests)}, ensure_ascii=False, indent=2), encoding='utf-8')
        try:
            httpx.post(base + '/api/test/stop', headers=headers, json={}, timeout=3)
        except httpx.HTTPError:
            pass
    print(json.dumps({'checks': checks, 'javascript_errors': errors,
        'fresh_job_requests': len(fresh_jobs), 'continuation_requests': len(continue_requests)}, ensure_ascii=False))


if __name__ == '__main__':
    serve() if '--serve' in sys.argv else browser()
