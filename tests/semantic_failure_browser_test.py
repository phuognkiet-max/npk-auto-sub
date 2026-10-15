"""Saved OCR / failed Gemini selection UX with private DATA and fixture engines."""
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

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'build/semantic-failure-browser'
PORT = 19885
TOKEN = 'semantic-failure-isolated-fixture'
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/'tests'))


def serve():
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=OUT, prefix='private-') as temporary:
        os.environ['VIETDUB_DATA_DIR'] = temporary
        os.environ['VIETDUB_TOKEN'] = TOKEN
        import credentials
        credentials.keyring.get_password = lambda *args: None
        credentials.active_secret = lambda *args: 'fixture-never-sent'
        from core import DATA, get, put, save_srt
        from ocr_tracks import auto_selection, rows_from_selection
        from subtitle_quality import coverage_record
        from stage_journal import summary
        from test_auto_subtitle_director import fixture, response
        from job_progress import verify_outputs
        import server
        import worker
        import uvicorn
        project, document = fixture()
        project['params'].update(subtitle_automation='gemini', pipeline_extract='ocr',
            pipeline_translate=False, pipeline_tts=False, pipeline_clean=False,
            use_clean=False, burn_subtitles=False, voice_gain=0,
            export_name='Semantic continued fixture', export_directory=str(DATA/'exports'))
        project['name'] = 'Saved OCR semantic failure fixture'
        (DATA/'exports').mkdir(exist_ok=True)
        raw = rows_from_selection(document, project, auto_selection(document, project))
        job = dict(id=uuid.uuid4().hex, project_id=project['id'], project_name=project['name'],
            kind='pipeline', state='running', stage='ocr', created=time.time(), started=time.time()-30,
            source_revision=project['source_revision'], project_revision=project['updated'],
            configuration=copy.deepcopy(project['params']))
        folder = DATA/'jobs'/job['id']; folder.mkdir()
        (folder/'input.json').write_text(json.dumps({'project':project, 'job':job}, ensure_ascii=False), encoding='utf-8')
        calls = []

        def recognize(row, step, destination):
            calls.append('ocr')
            save_srt(raw, destination/'source.srt', translated=False)
            (destination/'ocr-tracks.json').write_text(json.dumps(document, ensure_ascii=False), encoding='utf-8')
            return {'type':'ocr', 'files':['source.srt','ocr-tracks.json'], 'segments':copy.deepcopy(raw),
                'coverage':coverage_record(row, 'ocr', 0., 8., sampling_interval=.5)}

        def selection_failure(*args):
            calls.append('auto_subtitles_failed')
            raise RuntimeError('HTTP 503 · Fixture semantic service unavailable after 3 attempts')

        with patch.dict(worker.TASKS, {'ocr':recognize, 'auto_subtitles':selection_failure}), patch('worker.emit'), contextlib.redirect_stdout(io.StringIO()):
            try:
                worker.pipeline(copy.deepcopy(project), job, folder)
            except RuntimeError as exc:
                job.update(state='error', stage='auto_subtitles', failure_stage='auto_subtitles',
                    failure_code='provider_unavailable', next_action='continue_auto_subtitles',
                    ended=time.time(), progress=.2, message=str(exc),
                    stage_journal=summary(folder))
        assert calls == ['ocr','auto_subtitles_failed'], calls
        assert project['segments'] == []
        put('job', job['id'], job); put('project', project['id'], project)
        http = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=PORT,
            access_log=False, log_level='warning', timeout_graceful_shutdown=2))

        @server.app.get('/api/test/info')
        def info():
            return {'id':project['id'], 'job_id':job['id'], 'calls':calls}

        @server.app.get('/api/test/snapshot/{identifier}')
        def snapshot(identifier: str):
            assert get('job', identifier)['project_id'] == project['id']
            return json.loads((DATA/'jobs'/identifier/'input.json').read_text(encoding='utf-8'))

        @server.app.post('/api/test/complete')
        def complete(body: dict):
            current = get('job', body['id']); assert current['project_id'] == project['id']
            destination = DATA/'jobs'/current['id']
            work = snapshot(current['id'])['project']
            assert work['params']['pipeline_extract'] == 'existing'

            def forbid_recognition(*args):
                raise AssertionError('Semantic continuation unexpectedly reran OCR')

            def render(row, step, output):
                calls.append('render')
                shutil.copyfile(row['media']['path'], output/'final.mp4')
                return {'type':'render','files':['final.mp4'],'primary_file':'final.mp4'}

            actual_selection = worker.TASKS['auto_subtitles']
            def select(row, step, output):
                calls.append('auto_subtitles')
                return actual_selection(row, step, output)

            with patch.dict(worker.TASKS, {'ocr':forbid_recognition,'asr':forbid_recognition,
                    'auto_subtitles':select,'render':render}), \
                    patch('auto_subtitle_director.request_decision', return_value=response(document)), \
                    patch('worker.emit'), contextlib.redirect_stdout(io.StringIO()):
                result = worker.pipeline(work, current, destination)
                result['verification'] = verify_outputs(result, work, current, destination)
            from exports import publish_or_keep_local
            result = publish_or_keep_local(work, destination, result)
            with server.state_lock:
                applied = server.attach_completed_result(current, result)
                current.update(state='done', stage='done', ended=time.time(), progress=1.,
                    result=result, project_applied=applied)
                put('job',current['id'],current)
            return current

        @server.app.post('/api/test/stop')
        def stop():
            http.should_exit = True; return {}

        timer = threading.Timer(180, lambda: setattr(http,'should_exit',True)); timer.daemon=True; timer.start()
        try:
            http.run()
        finally:
            timer.cancel()
    (OUT/'cleanup.json').write_text(json.dumps({'private_removed':not Path(temporary).exists(),
        'provider_calls':0, 'inference_calls':0, 'production_data_mutated':False}), encoding='utf-8')


def browser():
    import httpx
    from playwright.sync_api import sync_playwright, expect
    OUT.mkdir(parents=True, exist_ok=True)
    base=f'http://127.0.0.1:{PORT}'; headers={'x-studio-token':TOKEN}; checks=[]; errors=[]; fresh=[]; continued=[]
    try:
        with sync_playwright() as pw:
            app=pw.chromium.launch(channel='msedge',headless=True)
            page=app.new_page(viewport={'width':1440,'height':1050})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: fresh.append(request.post_data_json)
                if request.method=='POST' and request.url.endswith('/api/jobs') else
                continued.append(request.post_data_json) if request.method=='POST' and request.url.endswith('/continue-pipeline') else None)
            try:
                page.goto(base+'/studio#token='+TOKEN); page.wait_for_load_state('networkidle')
                info=page.request.get(base+'/api/test/info',headers=headers).json()
                identifier, original=info['id'],info['job_id']
                plan=page.request.get(base+'/api/studio/projects/'+identifier+'/pipeline-continuation',headers=headers).json()
                assert plan['can_continue'],plan
                assert plan['recognition_complete'] and plan['recognition_count']==8,plan
                assert plan['next_action']=='continue_auto_subtitles',plan
                assert plan['completed_steps']==['ocr'],plan
                assert plan['remaining_steps']==['auto_subtitles','render'],plan
                assert not page.evaluate('P.segments.length')
                page.locator('#setupTop').click()
                expect(page.locator('#setupRun')).to_have_text('Tiếp tục tự xử lý phụ đề')
                expect(page.locator('#setupRun')).to_be_enabled()
                expect(page.locator('#setupPipelineState')).to_contain_text('Đã lưu 8 câu nhận dạng')
                expect(page.locator('#setupSubtitleCount')).to_contain_text('Chờ tự xử lý phụ đề')
                assert 'Nhận dạng chưa hoàn tất' not in page.locator('#setupPipelineState').inner_text()
                assert 'Giữ 0 câu dịch' not in page.locator('#setupPipelineState').inner_text()
                assert page.locator('#setupPipelineState [data-stage-state="done"]').count()==1
                assert page.locator('#setupPipelineState [data-stage-state="pending"]').count()==2
                checks.append('Empty editor plus verified raw OCR shows saved 8 cues, pending semantic stage and enabled semantic continuation')
                page.locator('#nav [data-area="subtitles"]').click()
                expect(page.locator('#recognitionTitle')).to_contain_text('Đã nhận dạng xong')
                expect(page.locator('#recognitionHint')).to_contain_text('Tự xử lý phụ đề')
                page.locator('#setupTop').click()
                page.evaluate('showJob('+json.dumps(original)+')');page.evaluate('setProgressExpanded(true)')
                expect(page.locator('[data-job-failure-stage="auto_subtitles"]')).to_contain_text('Tự xử lý phụ đề')
                expect(page.locator('#continuePipelineCurrent')).to_have_text('Tiếp tục tự xử lý phụ đề')
                expect(page.locator('#continuePipelineCurrent')).to_be_enabled()
                expect(page.locator('#retryCurrent')).to_be_hidden()
                assert 'Giữ 0 câu dịch' not in page.locator('#jobDetails').inner_text()
                assert 'Nhận dạng chưa hoàn tất' not in page.locator('#jobDetails').inner_text()
                checks.append('HTTP 503 after OCR is labelled semantic failure and offers continuation without claiming 0/0 translations or incomplete OCR')
                page.screenshot(path=str(OUT/'saved-ocr-semantic-failure.png'),full_page=True)
                with page.expect_response(lambda r:r.url.endswith('/continue-pipeline') and r.request.method=='POST') as observed:
                    page.locator('#continuePipelineCurrent').click()
                resumed=observed.value.json();assert observed.value.ok,resumed
                work=page.request.get(base+'/api/test/snapshot/'+resumed['id'],headers=headers).json()['project']
                assert work['params']['pipeline_extract']=='existing' and len(work['segments'])==8,work
                assert not fresh and len(continued)==1
                result=page.request.post(base+'/api/test/complete',headers=headers,data={'id':resumed['id']},timeout=120000)
                assert result.ok,result.text()
                final=result.json();assert final['state']=='done' and final['result']['primary_file']=='final.mp4',final
                after=page.request.get(base+'/api/test/info',headers=headers).json()
                assert after['calls']==['ocr','auto_subtitles_failed','auto_subtitles','render'],after
                page.evaluate('setProgressExpanded(false)');page.wait_for_function('!pollBusy');page.evaluate('poll()')
                expect(page.locator('#setupFinalResult')).to_contain_text('Đã xuất video thành phẩm')
                assert page.evaluate('P.segments.length')==8
                checks.append('One semantic continuation reuses OCR, applies actual validated selector and completes a verified fixture final without fresh full job')
                assert not errors,errors
            finally:
                app.close()
    finally:
        (OUT/'report.json').write_text(json.dumps({'checks':checks,'javascript_errors':errors,
            'fresh_job_requests':len(fresh),'continuation_requests':len(continued)},ensure_ascii=False,indent=2),encoding='utf-8')
        try:
            httpx.post(base+'/api/test/stop',headers=headers,json={},timeout=3)
        except httpx.HTTPError:
            pass
    print(json.dumps({'checks':checks,'javascript_errors':errors},ensure_ascii=False))


if __name__=='__main__':
    serve() if '--serve' in sys.argv else browser()
