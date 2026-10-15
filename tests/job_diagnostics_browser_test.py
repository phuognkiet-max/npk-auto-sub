"""Actual editor diagnostics for a stopped OCR fixture, no worker or user DATA."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'tests/job-diagnostics-ui-results'
PORT=19868
TOKEN='npk-job-diagnostics-isolated-fixture'
sys.path.insert(0,str(ROOT))


def serve():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='npk-job-diagnostics-',dir=ROOT/'tests') as temporary:
        os.environ['VIETDUB_DATA_DIR']=temporary;os.environ['VIETDUB_TOKEN']=TOKEN
        import credentials
        credentials.keyring.get_password=lambda *args:None
        import server
        import uvicorn
        from core import create_project,put,get
        project=create_project(str(ROOT/'tests/fixtures/demo.mp4'),'OCR diagnostics fixture')
        project['media']['duration']=198.111995
        project['params'].update(pipeline_translate=False,pipeline_tts=False)
        put('project',project['id'],project)
        started=time.time()-3600
        job={'id':uuid.uuid4().hex,'project_id':project['id'],'project_name':project['name'],'kind':'ocr',
             'state':'error','stage':'ocr','progress':.98,'created':started-1,'started':started,'ended':started+133.43,
             'source_revision':project.get('source_revision'),'message':'Không đọc đủ khung cuối. Tiến độ dở đã được lưu.',
             'events':[{'stage':'ocr','at':started+133,'message':'Đã lưu phần nhận dạng dở'}]}
        put('job',job['id'],job)
        http=uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=PORT,access_log=False,log_level='warning'))

        @server.app.get('/api/test/info')
        def info():return {'job':job['id'],'project':project['id'],'data':temporary}

        @server.app.post('/api/test/running')
        def running():
            current=get('job',job['id']);current.update(state='running',started=time.time()-10,ended=None,progress=.2)
            return put('job',job['id'],current)

        @server.app.post('/api/test/stop')
        def stop():http.should_exit=True;return {}

        timer=threading.Timer(180,lambda:setattr(http,'should_exit',True));timer.daemon=True;timer.start()
        try:http.run()
        finally:timer.cancel()
    (OUT/'.cleanup.json').write_text(json.dumps({'data_removed':not Path(temporary).exists(),'workers_started':False}),encoding='utf-8')


def browser():
    import httpx
    from playwright.sync_api import sync_playwright,expect
    OUT.mkdir(exist_ok=True);errors=[];checks=[]
    base=f'http://127.0.0.1:{PORT}'
    with httpx.Client(base_url=base,headers={'x-studio-token':TOKEN},trust_env=False,timeout=20) as client:
        info=client.get('/api/test/info').json()
        try:
            state=client.get('/api/studio/state?project_id='+info['project']).json()
            job=next(j for j in state['jobs'] if j['id']==info['job'])
            assert abs(job['ended']-job['started']-133.43)<.001
            assert state['recognition']['started']==job['started']
            checks.append('Compact jobs and recognition both retain started/ended timestamps')
            with sync_playwright() as pw:
                app=pw.chromium.launch(channel='msedge',headless=True)
                page=app.new_page(viewport={'width':1440,'height':1050})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/studio#token='+TOKEN);page.wait_for_load_state('networkidle')
                page.wait_for_function('Boolean(P?.id) && jobs.length>0')
                page.evaluate('(id)=>{setArea("subtitles");renderProgress();setProgressExpanded(true);return showJob(id)}',info['job'])
                expect(page.locator('#progressText')).to_contain_text('Đã dừng do lỗi')
                expect(page.locator('#progressText')).to_contain_text('2 phút 13 giây')
                expect(page.locator('#progress')).to_be_hidden()
                expect(page.locator('[data-job-status="error"]')).to_contain_text('Không còn xử lý')
                expect(page.locator('[data-job-status="error"]')).to_contain_text('Dừng do lỗi lúc')
                expect(page.locator('[data-job-status="error"]')).to_contain_text('3 phút 18 giây')
                assert page.locator('#cancelCurrent').count()==0
                expect(page.locator('#recognitionTitle')).to_contain_text('2 phút 13 giây')
                expect(page.locator('#recognitionHint')).to_contain_text('không còn chạy')
                before=page.locator('#progressText').inner_text();page.wait_for_timeout(1100)
                page.evaluate('renderProgress()');assert page.locator('#progressText').inner_text()==before
                page.screenshot(path=str(OUT/'stopped-ocr.png'),full_page=True)
                checks.append('An hour after OCR failure, runtime stays 2m13s, source duration is separate, error progress is hidden')
                client.post('/api/test/running').raise_for_status()
                page.evaluate('poll()');page.evaluate('(id)=>showJob(id)',info['job'])
                expect(page.locator('#progress')).to_be_visible()
                expect(page.locator('#cancelCurrent')).to_be_visible()
                expect(page.locator('#progressText')).to_contain_text('Đang xử lý')
                expect(page.locator('#progressText')).to_contain_text('Đã xử lý')
                checks.append('Running job still shows progress, elapsed runtime and cancellation')
                assert not errors,errors
                app.close()
        finally:
            client.post('/api/test/stop')
            for _ in range(100):
                if (OUT/'.cleanup.json').is_file():break
                time.sleep(.1)
    cleanup=json.loads((OUT/'.cleanup.json').read_text(encoding='utf-8'));(OUT/'.cleanup.json').unlink()
    assert cleanup['data_removed'] and not cleanup['workers_started']
    report={'ok':True,'checks':checks,'javascript_errors':errors,'cleanup':cleanup,'fixture':True}
    (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':serve() if '--serve' in sys.argv else browser()
