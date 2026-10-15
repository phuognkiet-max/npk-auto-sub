"""Guided single-video acceptance on isolated DATA, real API and an 8s fixture.

No user projects/keys, queue service, external provider requests or GPU work.
The test-only completion endpoint runs Piper CPU, subtitle rendering and muxing.
"""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'tests/single-video-setup-ui-results'
PORT=19878
TOKEN='npk-single-video-setup-isolated-fixture'
sys.path.insert(0,str(ROOT))


def serve():
    OUT.mkdir(exist_ok=True);cleanup=OUT/'fixture-cleanup.json';cleanup.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='npk-setup-browser-',dir=ROOT/'tests') as temporary:
        os.environ['VIETDUB_DATA_DIR']=temporary;os.environ['VIETDUB_TOKEN']=TOKEN
        import credentials
        credentials.keyring.get_password=lambda *args:None
        from core import DATA,create_project,put,get
        from subtitle_quality import coverage_record
        import server,uvicorn
        p=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'Single video setup fixture')
        p['params'].update(use_clean=False,pipeline_clean=False,pipeline_translate=False,pipeline_tts=False,
            pipeline_extract='existing',voice_gain=0,original_gain=0,tts_device='cpu',workload_mode='quiet',
            voice_id='piper:vais1000',font_size=24)
        p['segments']=[dict(start=.1,end=7.9,text='Lời phụ đề gốc.',translated='Bản dịch tiếng Việt.',spoken_text='Lời đã biên tập.')]
        p['regions']=[dict(x=80,y=280,w=450,h=60,start=0,end=8)]
        p['extraction_regions']=[dict(p['regions'][0])]
        p['coverage']=coverage_record(p,'ocr',0,8,sampling_interval=.5)
        put('project',p['id'],p)
        http=uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=PORT,access_log=False,log_level='warning',timeout_graceful_shutdown=2))
        latency={'seconds':0.}

        @server.app.middleware('http')
        async def delayed_save(request,call_next):
            response=await call_next(request)
            if request.method=='PUT' and request.url.path.startswith('/api/projects/') and latency['seconds']:
                await asyncio.sleep(latency['seconds'])
            return response

        @server.app.post('/api/test/save-delay')
        def save_delay(body:dict):latency['seconds']=float(body['seconds']);return {}

        @server.app.get('/api/test/info')
        def info():return {'id':p['id']}

        @server.app.post('/api/test/complete')
        def complete(body:dict):
            from worker import pipeline,subtitle_preview,render
            from job_progress import verify_outputs
            j=get('job',body['id']);folder=DATA/'jobs'/j['id']
            snapshot=json.loads((folder/'input.json').read_text(encoding='utf-8'))['project']
            assert snapshot['media']['path']==str(ROOT/'tests/fixtures/demo.mp4')
            assert j['kind'] in ('pipeline','subtitle_preview','render')
            assert snapshot['params']['voice_id']=='piper:vais1000'
            assert not snapshot['params']['pipeline_translate']
            with contextlib.redirect_stdout(io.StringIO()):
                result={'pipeline':pipeline,'subtitle_preview':subtitle_preview,'render':render}[j['kind']](snapshot,j,folder)
                result['verification']=verify_outputs(result,snapshot,j,folder)
            with server.state_lock:
                applied=server.attach_completed_result(j,result)
                j.update(state='done',stage='done',progress=1,ended=time.time(),result=result,project_applied=applied)
                put('job',j['id'],j)
            if j['kind']=='pipeline':
                import shutil
                shutil.copyfile(folder/'final.mp4',OUT/'fixture-final.mp4')
                shutil.copyfile(folder/'subtitles.ass',OUT/'fixture-subtitles.ass')
                shutil.copyfile(folder/'voice.wav',OUT/'fixture-voice.wav')
            return j

        @server.app.post('/api/test/review-needed')
        def require_review():
            row=get('project',p['id']);row.pop('coverage',None);row['updated']=time.time();put('project',row['id'],row);return row

        @server.app.post('/api/test/stop')
        def stop():http.should_exit=True;return {}

        timer=threading.Timer(300,lambda:setattr(http,'should_exit',True));timer.daemon=True;timer.start()
        try:http.run()
        finally:timer.cancel()
    cleanup.write_text(json.dumps({'data_removed':not Path(temporary).exists(),'queue_workers_started':False,'gpu_work':False}),encoding='utf-8')


def browser():
    import httpx
    from playwright.sync_api import sync_playwright,expect
    OUT.mkdir(exist_ok=True);base=f'http://127.0.0.1:{PORT}';headers={'x-studio-token':TOKEN}
    checks,errors,job_requests=[],[],[]
    try:
        with sync_playwright() as pw:
            app=pw.chromium.launch(channel='msedge',headless=True);page=app.new_page(viewport={'width':1440,'height':1050})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('console',lambda message:errors.append(message.text) if message.type=='error' and ('Cannot set properties' in message.text or 'Cannot read properties' in message.text) else None)
            page.on('request',lambda request:job_requests.append(request.post_data_json) if request.method=='POST' and request.url.endswith('/api/jobs') else None)
            try:
                page.goto(base+'/studio#token='+TOKEN);page.wait_for_load_state('networkidle')
                expect(page.locator('#projectName')).to_have_text('Single video setup fixture')
                page.locator('#setupTop').click();expect(page.locator('#setupCheck')).to_contain_text('Cấu hình sẵn sàng')
                assert page.locator('.setup-section').count()==5
                identifier=page.request.get(base+'/api/test/info',headers=headers).json()['id']

                def saved():page.wait_for_function('!dirty&&!saving')
                def ready():expect(page.locator('#setupCheck')).not_to_have_text('Đang kiểm tra cấu hình…')
                def current():return page.request.get(base+'/api/studio/projects/'+identifier,headers=headers).json()
                def complete(job):
                    response=page.request.post(base+'/api/test/complete',headers=headers,data={'id':job['id']},timeout=120000)
                    assert response.ok,response.text();page.wait_for_function('!pollBusy');page.evaluate('poll()');return response.json()

                page.locator('#setupClean').check();saved();ready()
                page.locator('#setupMethod').select_option('box');saved();ready()
                assert current()['params']['pipeline_clean'] and current()['params']['method']=='box'
                page.locator('#setupClean').uncheck();saved();ready()
                page.locator('#setupExtraction').select_option('ocr');saved();ready();expect(page.locator('#setupRecognitionHint')).to_contain_text('1 vùng đọc')
                page.locator('#setupExtraction').select_option('asr');saved();ready();expect(page.locator('#setupRecognitionHint')).to_contain_text('Nghe âm thanh')
                page.locator('#setupExtraction').select_option('existing');saved();ready()
                checks.append('One saved configuration controls clean method and existing/OCR/STT extraction without starting work')

                for preset in ('clean','pop','highlight','karaoke'):
                    page.locator(f'[data-setup-preset="{preset}"]').click();saved();ready()
                    p=current()['params'];assert p['subtitle_preset']==preset
                    assert p['subtitle_karaoke']==(preset=='karaoke')
                assert page.request.post(base+'/api/test/save-delay',headers=headers,data={'seconds':.6}).ok
                page.locator('[data-setup-preset="highlight"]').click()
                page.wait_for_function('saving')
                page.locator('#nav [data-area="voice"]').click()
                page.locator('#setupTop').click();saved();ready()
                assert current()['params']['subtitle_preset']=='highlight'
                assert not errors,errors
                assert page.request.post(base+'/api/test/save-delay',headers=headers,data={'seconds':0}).ok
                checks.append('Navigating away and remounting setup during a delayed save does not let stale callbacks update missing controls')
                with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
                    page.locator('#setupExactPreview').click()
                preview=response.value.json();assert preview['kind']=='subtitle_preview';complete(preview)
                expect(page.locator('.setup-exact-image')).to_be_visible()
                page.wait_for_function('document.querySelector(".setup-exact-image")?.naturalWidth>0')
                checks.append('All four preset cards persist renderer parameters and a real FFmpeg subtitle-preview image is displayed')

                page.locator('#setupDub').check();saved();ready()
                for mode in ('translated','edited','source'):
                    page.locator('#setupTextSource').select_option(mode);saved();ready();assert current()['params']['tts_text_source']==mode
                page.locator('#setupPickVoice').click()
                expect(page.locator('#voiceSource').locator('..')).to_contain_text('Cách tạo giọng')
                expect(page.locator('[data-key="tts_text_source"]')).to_have_value('source')
                page.locator('#favoriteOnly').check();expect(page.locator('#voiceGrid')).to_contain_text('Chưa có giọng yêu thích')
                page.locator('#showAllVoices').click();expect(page.locator('#favoriteOnly')).not_to_be_checked();assert page.locator('[data-voice]').count()>0
                page.locator('#setupTop').click();ready()
                checks.append('Dubbing content source persists separately from voice method; empty favorites recover to the installed catalog')

                baseline=current();missing=[dict(row,translated='') for row in baseline['segments']]
                assert page.request.put(base+'/api/projects/'+identifier,headers=headers,data={'expected_revision':baseline['updated'],'segments':missing}).ok
                page.reload();page.wait_for_load_state('networkidle');page.locator('#setupTop').click()
                page.locator('#setupTextSource').select_option('translated');saved();ready()
                expect(page.locator('#setupRun')).to_be_disabled();expect(page.locator('#setupCheck')).to_contain_text('Đang chọn Bản dịch')
                page.locator('#setupTextSource').select_option('edited');saved();ready();expect(page.locator('#setupRun')).to_be_enabled()
                page.locator('#setupTextSource').select_option('source');saved();ready();expect(page.locator('#setupRun')).to_be_enabled()
                latest=current();assert page.request.put(base+'/api/projects/'+identifier,headers=headers,data={'expected_revision':latest['updated'],'segments':baseline['segments']}).ok
                page.reload();page.wait_for_load_state('networkidle');page.locator('#setupTop').click();ready()
                checks.append('Missing translations block translated dubbing while explicitly edited/original content remain valid')

                for width in (1920,1440,1280,1024,720,400):
                    page.set_viewport_size({'width':width,'height':1050})
                    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
                page.set_viewport_size({'width':1440,'height':1050});page.screenshot(path=str(OUT/'setup-ready.png'),full_page=True)
                before=current();expect(page.locator('#setupRun')).to_be_enabled()
                with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
                    page.locator('#setupRun').click()
                job=response.value.json();assert job['kind']=='pipeline'
                expect(page.locator('#setupRun')).to_be_disabled();expect(page.locator('#setupTextSource')).to_be_disabled()
                locked_revision=current()['updated']
                page.locator('#nav [data-area="clean"]').click()
                expect(page.locator('#pipelineEditLock')).to_be_visible()
                expect(page.locator('#cleanAll')).to_be_disabled()
                assert page.locator('#panel input:enabled').count()==0
                page.evaluate('change(row => row.params.blur_radius = 27)')
                assert page.evaluate('dirty') is False
                assert current()['updated']==locked_revision
                expect(page.locator('#undo')).to_be_disabled()
                page.locator('#setupTop').click()
                pipelines=[j for j in job_requests if j['kind']=='pipeline'];assert len(pipelines)==1
                assert pipelines[0]['expected_revision']==before['updated'];assert pipelines[0]['request_id']
                result=complete(job);assert result['project_applied'];assert result['result']['primary_file']=='final.mp4'
                assert result['result']['verification']['video_frames_decoded']['final.mp4']>0
                assert 'tts' in result['result']['steps'] and 'render' in result['result']['steps']
                expect(page.locator('#setupRun')).to_be_enabled()
                checks.append('Responsive setup queues exactly one revision-bound pipeline, freezes setup and advanced editors, and completes real Piper CPU dubbing plus muxing')

                page.locator('[data-setup-preset="highlight"]').click();saved();ready()
                with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
                    page.locator('#setupStyleExport').click()
                style_job=response.value.json();assert style_job['kind']=='render'
                expect(page.locator('#setupStyleExport')).to_be_disabled()
                expect(page.locator('#setupTextSource')).to_be_disabled()
                style_result=complete(style_job);assert style_result['project_applied']
                expect(page.locator('#setupStyleExport')).to_be_enabled()
                assert len([j for j in job_requests if j['kind']=='pipeline'])==1
                checks.append('Style export queues render alone, freezes edits until completion, and reuses existing speech without another pipeline')

                assert page.request.post(base+'/api/test/review-needed',headers=headers,data={}).ok
                page.reload();page.wait_for_load_state('networkidle');page.locator('#setupTop').click()
                expect(page.locator('#setupReview')).to_be_visible();expect(page.locator('#setupRun')).to_be_disabled()
                page.locator('#setupReview').click();assert page.evaluate('subtab')=='quality'
                checks.append('Unreviewed subtitle coverage blocks Run and links to the actual quality editor')

                page.locator('#setupTop').click()
                page.locator('#setupVideoFile').set_input_files(str(ROOT/'tests/fixtures/demo.mp4'))
                expect(page.locator('#projectName')).to_have_text('demo')
                assert page.evaluate('area')=='setup'
                imported=page.evaluate('P.id');assert imported!=identifier
                assert page.request.get(base+'/api/studio/projects/'+imported,headers=headers).json()['media']['path'].startswith(str(ROOT/'tests'))
                checks.append('Browser file selection streams an actual video through the import endpoint and opens its new setup')
                transfer=page.evaluate_handle('''async () => { const response=await fetch(document.querySelector('#video').src);const blob=await response.blob();const data=new DataTransfer();data.items.add(new File([blob],'drop-fixture.mp4',{type:'video/mp4'}));return data; }''')
                page.locator('#setupDrop').dispatch_event('drop',{'dataTransfer':transfer})
                expect(page.locator('#projectName')).to_have_text('drop-fixture');assert page.evaluate('P.id')!=imported
                checks.append('Actual File drag/drop creates a new imported video through the same streaming API')

                # A DeepSeek translation setup must not require a Gemini key
                # merely to recognize burned-in subtitles. The guided action
                # configures local OCR while preserving translation settings.
                page.locator('#setupExtraction').select_option('existing');saved();ready()
                page.locator('#setupSubtitleAutomation').select_option('gemini');saved();ready()
                page.locator('#setupProvider').select_option('deepseek');saved();ready()
                page.locator('#setupTranslate').uncheck();saved();ready()
                page.locator('#setupDub').uncheck();saved();ready()
                page.locator('#setupClean').uncheck();saved();ready()
                page.locator('#setupDisplay').check();saved();ready()
                project_id=page.evaluate('P.id')
                current_project=page.request.get(base+'/api/studio/projects/'+project_id,headers=headers).json()
                expected_model=current_project['params']['translation_model']
                expect(page.locator('#setupRun')).to_be_disabled()
                expect(page.locator('#setupCheck')).to_contain_text('Chưa có phụ đề')
                expect(page.locator('#setupAutomaticRecognition')).to_be_visible()
                page.locator('#setupAutomaticRecognition').click();saved();ready()
                configured=page.request.get(base+'/api/studio/projects/'+project_id,headers=headers).json()
                assert configured['params']['pipeline_extract']=='ocr'
                assert configured['params']['subtitle_automation']=='local'
                assert configured['params']['provider']=='deepseek'
                assert configured['params']['translation_model']==expected_model
                assert page.locator('#setupRun').is_enabled(), page.locator('#setupCheck').inner_text()
                checks.append('No subtitles offers one full-frame local OCR action; provider/model stay unchanged and Run becomes ready')

                page.locator('#setupSubtitleAutomation').select_option('gemini');saved();ready()
                expect(page.locator('#setupRun')).to_be_disabled()
                expect(page.locator('#setupCheck')).to_contain_text('Chọn kết nối Gemini')
                expect(page.locator('#setupAutomaticRecognition')).to_be_visible()
                page.locator('#setupAutomaticRecognition').click();saved();ready()
                configured=page.request.get(base+'/api/studio/projects/'+project_id,headers=headers).json()
                assert configured['params']['subtitle_automation']=='local'
                assert configured['params']['provider']=='deepseek'
                assert configured['params']['translation_model']==expected_model
                expect(page.locator('#setupRun')).to_be_enabled()
                checks.append('Unavailable Gemini subtitle coordination switches to the local automatic flow without changing DeepSeek translation settings')
                assert not errors,errors
            finally:app.close()
    finally:
        try:httpx.post(base+'/api/test/stop',headers=headers,json={},timeout=3,trust_env=False)
        except httpx.HTTPError:pass
        deadline=time.monotonic()+5
        while not (OUT/'fixture-cleanup.json').exists() and time.monotonic()<deadline:time.sleep(.05)
        (OUT/'report.json').write_text(json.dumps({'checks':checks,'javascript_errors':errors,'pipeline_requests':len([r for r in job_requests if r['kind']=='pipeline'])},ensure_ascii=False,indent=2),encoding='utf-8')
    assert json.loads((OUT/'fixture-cleanup.json').read_text())['data_removed']
    print(json.dumps({'checks':checks,'javascript_errors':errors,'pipeline_requests':len([r for r in job_requests if r['kind']=='pipeline'])},ensure_ascii=False))


if __name__=='__main__':serve() if '--serve' in sys.argv else browser()
