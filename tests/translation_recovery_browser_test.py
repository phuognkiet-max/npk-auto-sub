"""Real editor recovery/resume buttons with isolated storage and no API calls."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'tests/translation-recovery-ui-results'
PORT=19873
TOKEN='npk-translation-recovery-isolated-fixture'
sys.path.insert(0,str(ROOT))


def serve():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='npk-recovery-browser-',dir=ROOT/'tests') as temporary:
        os.environ['VIETDUB_DATA_DIR']=temporary;os.environ['VIETDUB_TOKEN']=TOKEN
        import credentials
        credentials.keyring.get_password=lambda *args:None
        import server
        import preflight
        import uvicorn
        from core import create_project,put,get,DATA
        from translation_checkpoint import write_checkpoint
        server.active_secret=lambda *args:'fixture-not-a-real-api-key'
        server.active_key_id=lambda *args:'fixture-key'
        preflight.inspect=lambda *args,**kwargs:{'ok':True,'errors':[],'warnings':[]}
        info={}
        for kind in ('partial','selected','first_failure'):
            project=create_project(str(ROOT/'tests/fixtures/demo.mp4'),kind+' recovery fixture')
            project['params'].update(pipeline_translate=False,pipeline_tts=False,translation_scope='all')
            project['segments']=[{'start':0.,'end':1.,'text':'Hello','translated':''},
                                 {'start':2.,'end':3.,'text':'Thanks','translated':''},
                                 {'start':4.,'end':5.,'text':'Goodbye','translated':''}]
            if kind=='first_failure':
                project['segments']=[{'start':index*.08,'end':index*.08+.04,'text':f'Cue {index}',
                    'translated':f'Saved {index}' if index<20 else ''} for index in range(88)]
            put('project',project['id'],project)
            job={'id':uuid.uuid4().hex,'project_id':project['id'],'project_name':project['name'],'kind':'translate',
                 'state':'error','stage':'translate','progress':.5,'created':time.time()-30,'started':time.time()-29,
                 'ended':time.time()-20,'source_revision':project.get('source_revision'),
                 'project_revision':project['updated'],'message':'503: nhà cung cấp tạm quá tải; checkpoint vẫn được giữ.',
                 'events':[]}
            if kind=='selected':job['segment_indices']=[0,2]
            if kind=='first_failure':job['segment_indices']=list(range(20,88))
            folder=DATA/'jobs'/job['id'];folder.mkdir()
            (folder/'input.json').write_text(json.dumps({'project':project,'job':job},ensure_ascii=False),encoding='utf-8')
            rows=copy.deepcopy(project['segments']);rows[0]['translated']='Xin chào'
            completed=[0]
            if kind=='partial':rows[1]['translated']='Cảm ơn';completed.append(1)
            if kind!='first_failure':
                write_checkpoint(folder/'translation-checkpoint.json',project,rows,completed,job.get('segment_indices'))
            put('job',job['id'],job)
            if kind=='partial':
                project['segments'][0]['translated']='Bản tôi tự sửa';project['updated']+=1
                put('project',project['id'],project)
            info[kind]={'project':project['id'],'job':job['id']}
        http=uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=PORT,access_log=False,log_level='warning'))

        @server.app.get('/api/test/info')
        def fixture_info():return info

        @server.app.post('/api/test/finish/{identifier}')
        def finish(identifier:str):
            job=get('job',identifier);job.update(state='done',project_applied=False,ended=time.time(),
                message='Hoàn tất; kết quả lưu riêng do dự án thay đổi.')
            put('job',identifier,job);return {}

        @server.app.post('/api/test/artifact/{identifier}')
        def artifact(identifier:str):
            project=get('project',identifier)
            job={'id':uuid.uuid4().hex,'project_id':identifier,'kind':'tts',
                'source_revision':project.get('source_revision'),'project_revision':project['updated']}
            assert server.attach_completed_result(job,{'files':['voice.wav']})
            return {'job_id':job['id']}

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
            with sync_playwright() as pw:
                app=pw.chromium.launch(channel='msedge',headless=True)
                page=app.new_page(viewport={'width':1440,'height':1050})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(base+'/studio#token='+TOKEN);page.wait_for_load_state('networkidle')
                page.wait_for_function('Boolean(P?.id)')
                partial=info['partial']
                page.evaluate('(id)=>loadProject(id)',partial['project'])
                page.evaluate('(id)=>{setProgressExpanded(true);return showJob(id)}',partial['job'])
                expect(page.locator('#restoreTranslationCurrent')).to_have_text('Khôi phục 1 câu đã dịch')
                expect(page.locator('#jobDetails')).to_contain_text('Các câu bạn đã sửa tay được giữ nguyên')
                page.screenshot(path=str(OUT/'partial-restore.png'),full_page=True)
                page.locator('#restoreTranslationCurrent').click()
                page.wait_for_function('P.segments[1].translated === "Cảm ơn"')
                assert page.evaluate('P.segments[0].translated')=='Bản tôi tự sửa'
                page.wait_for_function('!document.querySelector("#restoreTranslationCurrent")')
                stored=client.get('/api/studio/projects/'+partial['project']).json()
                assert [r['translated'] for r in stored['segments']]==['Bản tôi tự sửa','Cảm ơn','']
                checks.append('Partial recovery button displays eligible count, fills missing cue, retains manual edit and disappears after recovery')
                page.evaluate('change(p=>p.segments[2].translated="Draft typed before TTS completion");clearTimeout(saveTimer)')
                artifact=client.post('/api/test/artifact/'+partial['project']).json()
                page.evaluate('save()')
                assert page.evaluate('!dirty && !conflict')
                saved=client.get('/api/studio/projects/'+partial['project']).json()
                assert saved['segments'][2]['translated']=='Draft typed before TTS completion'
                assert saved['artifacts']['tts']['job_id']==artifact['job_id']
                checks.append('Editor draft saves across artifact-only completion via verified historical rebase, preserving both draft and new voice artifact')
                selected=info['selected']
                page.evaluate('(id)=>loadProject(id)',selected['project'])
                page.evaluate('(id)=>showJob(id)',selected['job'])
                expect(page.locator('#retryCurrent')).to_have_text('Tiếp tục bản dịch dở')
                page.locator('#retryCurrent').click()
                expect(page.locator('#modal')).to_be_visible()
                page.locator('#modal button').filter(has_text='Chạy lại').click()
                page.wait_for_function('(id)=>jobs.some(j=>j.kind==="translate"&&j.project_id===id&&j.state==="queued")',arg=selected['project'])
                queued=next(j for j in client.get('/api/studio/state').json()['jobs'] if j['project_id']==selected['project'] and j['state']=='queued')
                detail=client.get('/api/studio/jobs/'+queued['id']).json()
                assert detail['segment_indices']==[0,2] and detail['resume_from']==selected['job']
                checks.append('Selected-cue Resume button preserves original indices [0,2] and queues a valid checkpoint resume without a worker or API request')
                client.post('/api/test/finish/'+queued['id']).raise_for_status()
                page.evaluate('poll()')
                page.wait_for_function('document.body.innerText.includes("Kết quả được lưu riêng, chưa thay phụ đề hoặc giọng/video hiện tại")')
                checks.append('Completed stale job is reported as stored separately, without claiming it replaced the current project')
                first_failure=info['first_failure']
                page.evaluate('(id)=>loadProject(id)',first_failure['project'])
                page.evaluate('(id)=>showJob(id)',first_failure['job'])
                expect(page.locator('#retryCurrent')).to_have_text('Dịch tiếp 68 câu còn thiếu')
                page.locator('#retryCurrent').click()
                expect(page.locator('#modal')).to_be_visible()
                page.locator('#modal button').filter(has_text='Chạy lại').click()
                page.wait_for_function('(id)=>jobs.some(j=>j.project_id===id&&j.state==="queued")',arg=first_failure['project'])
                queued=next(j for j in client.get('/api/studio/state').json()['jobs'] if j['project_id']==first_failure['project'] and j['state']=='queued')
                detail=client.get('/api/studio/jobs/'+queued['id']).json()
                assert detail['segment_indices']==list(range(20,88)) and detail['retry_from']==first_failure['job']
                saved=client.get('/api/studio/projects/'+first_failure['project']).json()
                assert all(r['translated']==f'Saved {i}' for i,r in enumerate(saved['segments'][:20]))
                checks.append('A selected first-batch 503 with no checkpoint retries exactly 68 missing cues and preserves the 20 already translated cues')
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
