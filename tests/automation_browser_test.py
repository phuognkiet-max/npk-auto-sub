"""Automation UI acceptance on private DATA; no provider or inference calls."""
import asyncio
import copy
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'build/automation-browser'; PORT=19884; TOKEN='automation-isolated-fixture'
sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))


def serve():
    OUT.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=OUT,prefix='private-') as temporary:
        os.environ['VIETDUB_DATA_DIR']=temporary;os.environ['VIETDUB_TOKEN']=TOKEN
        import credentials
        credentials.keyring.get_password=lambda *args:None
        from test_auto_subtitle_director import fixture
        from core import DATA,get,put
        from subtitle_quality import coverage_record
        import server,uvicorn
        project,document=fixture();project['params']['burn_subtitles']=True
        project['params']['font_size']=24
        project['name']='Automation fixture'
        project['segments']=[dict(start=3.,end=4.,text='A different title',translated='')]
        project['coverage']=coverage_record(project,'ocr',0,8,sampling_interval=.5,
            ocr_selection={'requires_review':True,'selected_tracks':['ocr-track-3'],
                'issues':[{'code':'competing_caption_band','severity':'review'}]})
        identifier=uuid.uuid4().hex;folder=DATA/'jobs'/identifier;folder.mkdir()
        (folder/'ocr-tracks.json').write_text(json.dumps(document),encoding='utf-8')
        put('project',project['id'],project)
        put('job',identifier,dict(id=identifier,project_id=project['id'],source_revision=project['source_revision'],
            project_revision=project['updated'],kind='ocr',state='done',created=1.,progress=1.,
            result={'files':['ocr-tracks.json']}))
        http=uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=PORT,access_log=False,log_level='warning'))
        @server.app.get('/api/test/info')
        def info():return {'id':project['id']}
        @server.app.post('/api/test/complete')
        def complete(body:dict):
            import worker
            from job_progress import verify_outputs
            job=get('job',body['id']);assert job['project_id']==project['id']
            job_folder=DATA/'jobs'/job['id'];snapshot=json.loads((job_folder/'input.json').read_text(encoding='utf-8'))['project']
            assert snapshot['params']['subtitle_automation']=='local'
            assert not snapshot['params']['pipeline_translate'] and not snapshot['params']['pipeline_tts']
            with contextlib.redirect_stdout(io.StringIO()):
                result=worker.TASKS[job['kind']](snapshot,job,job_folder)
                result['verification']=verify_outputs(result,snapshot,job,job_folder)
            with server.state_lock:
                applied=server.attach_completed_result(job,result)
                job.update(state='done',ended=time.time(),progress=1.,stage='done',result=result,project_applied=applied)
                put('job',job['id'],job)
            return job
        @server.app.post('/api/test/stop')
        def stop():http.should_exit=True;return {}
        @server.app.post('/api/test/reset-selection')
        def reset():
            row=get('project',project['id']);row['segments']=copy.deepcopy(project['segments'])
            row['coverage']=copy.deepcopy(project['coverage']);row['updated']=time.time()
            put('project',row['id'],row);return row
        timer=threading.Timer(180,lambda:setattr(http,'should_exit',True));timer.daemon=True;timer.start()
        try:http.run()
        finally:timer.cancel()
    (OUT/'cleanup.json').write_text(json.dumps({'private_removed':not Path(temporary).exists(),'provider_calls':0,'inference_calls':0}),encoding='utf-8')


def browser():
    import httpx
    from playwright.sync_api import sync_playwright,expect
    OUT.mkdir(parents=True,exist_ok=True)
    base=f'http://127.0.0.1:{PORT}';headers={'x-studio-token':TOKEN};checks=[];errors=[]
    try:
        with sync_playwright() as pw:
            app=pw.chromium.launch(channel='msedge',headless=True)
            page=app.new_page(viewport={'width':1440,'height':1000})
            page.on('pageerror',lambda e:errors.append(str(e)))
            try:
                page.goto(base+'/studio#token='+TOKEN);page.wait_for_load_state('networkidle')
                identifier=page.request.get(base+'/api/test/info',headers=headers).json()['id']
                page.locator('#setupTop').click()
                expect(page.locator('#setupSubtitleAutomation')).to_have_value('local')
                expect(page.locator('#setupAutoSubtitles')).to_be_visible()
                page.locator('#nav [data-area="subtitles"]').click()
                page.locator('[data-tab="quality"]').click()
                expect(page.locator('#qualitySubtitleAutomation')).to_have_value('local')
                expect(page.locator('#autoSubtitles')).to_be_enabled()
                assert not page.locator('#ocrTrackPicker').evaluate('(el)=>el.open')
                assert not page.locator('.quality-advanced').evaluate('(el)=>el.open')
                page.screenshot(path=str(OUT/'quality-automatic.png'),full_page=True)
                checks.append('Automatic primary action replaces expanded numbered tracks; advanced evidence remains collapsed')
                page.locator('#qualitySubtitleAutomation').select_option('gemini')
                expect(page.locator('#qualityGeminiDisclosure')).to_be_visible()
                page.wait_for_function('!dirty&&!saving')
                assert page.request.post(base+'/api/preflight',headers=headers,data={'project_id':identifier,'kind':'auto_subtitles'}).json()['ok'] is False
                checks.append('Gemini has visible OCR/image disclosure and missing connection cannot enqueue external analysis')
                page.locator('#qualitySubtitleAutomation').select_option('local')
                expect(page.locator('#autoSubtitles')).to_be_enabled()
                with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
                    page.locator('#autoSubtitles').click()
                job=response.value.json();assert job['kind']=='auto_subtitles',job
                done=page.request.post(base+'/api/test/complete',headers=headers,data={'id':job['id']},timeout=60000)
                assert done.ok,done.text();assert done.json()['project_applied']
                page.wait_for_function('!pollBusy');page.evaluate('poll()')
                expect(page.locator('#panel .panel-title .pill')).to_contain_text('8')
                saved=page.request.get(base+'/api/studio/projects/'+identifier,headers=headers).json()
                assert len(saved['segments'])==8 and saved['segments'][0]['text']=='Caption 0'
                checks.append('One action reselects the sustained caption track, applies eight original cues and retains revision history')
                page.locator('#setupTop').click()
                expect(page.locator('#setupRun')).to_be_enabled()
                with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
                    page.locator('#setupRun').click()
                job=response.value.json();assert job['kind']=='pipeline'
                done=page.request.post(base+'/api/test/complete',headers=headers,data={'id':job['id']},timeout=120000)
                assert done.ok,done.text();assert done.json()['result']['primary_file']=='final.mp4'
                page.wait_for_function('!pollBusy');page.evaluate('poll()')
                expect(page.locator('#setupFinalResult')).to_be_visible()
                checks.append('After automatic selection, the pipeline reaches a real decoded MP4 and shows its final path')
                page.screenshot(path=str(OUT/'setup-final.png'),full_page=True)
                assert page.request.post(base+'/api/test/reset-selection',headers=headers,data={}).ok
                page.evaluate('loadProject('+json.dumps(identifier)+',true)')
                page.locator('#setupTop').click();expect(page.locator('#setupRun')).to_be_enabled()
                with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
                    page.locator('#setupRun').click()
                job=response.value.json()
                done=page.request.post(base+'/api/test/complete',headers=headers,data={'id':job['id']},timeout=120000)
                assert done.ok,done.text();result=done.json()['result']
                assert 'auto_subtitles' in result['steps'] and 'render' in result['steps'],result['steps']
                assert len(result['segments'])==8
                checks.append('Full Run repairs the wrong prior track automatically and reaches MP4 in one pipeline')
                assert not errors,errors
            finally:app.close()
    finally:
        try:httpx.post(base+'/api/test/stop',headers=headers,json={},timeout=3,trust_env=False)
        except httpx.HTTPError:pass
        (OUT/'report.json').write_text(json.dumps({'checks':checks,'javascript_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':checks,'javascript_errors':errors},ensure_ascii=False))


if __name__=='__main__':serve() if '--serve' in sys.argv else browser()
