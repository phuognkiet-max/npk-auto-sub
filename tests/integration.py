"""Real local integration tests. Never contacts paid translation APIs."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from playwright.sync_api import sync_playwright
from core import ROOT, probe

BASE='http://127.0.0.1:18763'
TOKEN=os.environ['VIETDUB_TOKEN']
client=httpx.Client(base_url=BASE,headers={'x-studio-token':TOKEN},timeout=60)
fixture=ROOT/'tests/fixtures/demo.mp4'
initial_hash=hashlib.sha256(fixture.read_bytes()).hexdigest()
report=[]

def req(method,path,body=None):
    response=client.request(method,'/api'+path,json=body)
    response.raise_for_status()
    return response.json()

def wait_job(job,timeout=600):
    start=time.monotonic()
    last=None
    while time.monotonic()-start<timeout:
        state=req('GET','/state')
        current=next(j for j in state['jobs'] if j['id']==job['id'])
        if current['state']!=last:
            print(job['kind'],current['state'],current['message'],flush=True)
            last=current['state']
        if current['state'] in {'done','error','cancelled'}:
            assert current['state']=='done',current
            report.append({'kind':job['kind'],'seconds':round(time.monotonic()-start,2),'state':'passed','job_id':job['id']})
            return current
        time.sleep(1)
    raise TimeoutError(job)

try:
    assert httpx.get(BASE+'/api/state').status_code==403
    assert httpx.get(BASE+'/media/source/anything').status_code==403
    p=req('POST','/projects',{'path':str(fixture),'name':'Demo · Kiểm thử độc lập'})
    pid=p['id']
    p=req('PUT','/projects/'+pid,{'regions':[{'x':145.5,'y':287.2,'w':350.4,'h':43.2,'start':0,'end':8}],
        'params':{'max_side':512,'context':64,'method':'lama'}})
    assert req('GET','/state')['projects'][0]['id']==pid
    frame=client.get('/api/projects/'+pid+'/frame?time=1')
    assert frame.status_code==200 and frame.headers['content-type']=='image/jpeg'
    def job(kind,**extra):
        return req('POST','/jobs',{'project_id':pid,'kind':kind,'time':1,'replace_segments':True}|extra)
    preview=wait_job(job('preview'))
    assert all((ROOT/'data/jobs'/preview['id']/name).is_file() for name in preview['result']['files'])
    detection=wait_job(job('detect'))
    assert any('SUBTITLE' in r.get('text','').upper() for r in detection['result']['regions']),detection
    clean=wait_job(job('clean'))
    cleaned=probe(ROOT/'data/jobs'/clean['id']/'clean.mp4')
    assert abs(cleaned['duration']-8)<.15 and cleaned['audio']
    subtitle_text='1\n00:00:00,300 --> 00:00:03,800\nXin chào các bạn.\n\n2\n00:00:04,000 --> 00:00:07,600\nĐây là bản thử nghiệm độc lập.\n'
    p=req('POST','/projects/'+pid+'/import',{'text':subtitle_text})
    for row in p['segments']:
        row['translated']=row['text']
    p=req('PUT','/projects/'+pid,{'segments':p['segments']})
    voice=wait_job(job('tts'))
    assert (ROOT/'data/jobs'/voice['id']/'voice.wav').stat().st_size>1000
    rendered=wait_job(job('render'))
    final=probe(ROOT/'data/jobs'/rendered['id']/'final.mp4')
    assert abs(final['duration']-8)<.15 and final['audio'] and final['width']==640
    srt=client.get('/api/projects/'+pid+'/subtitles').text
    assert 'Xin chào' in srt
    # Stale clean output must be rejected after a mask change.
    p=req('PUT','/projects/'+pid,{'params':{'dilate':5}})
    stale=job('render')
    for _ in range(90):
        check=next(j for j in req('GET','/state')['jobs'] if j['id']==stale['id'])
        if check['state'] in {'error','done'}:break
        time.sleep(1)
    assert check['state']=='error' and 'thay đổi' in check['message'],check
    req('PUT','/projects/'+pid,{'params':{'dilate':4}})
    report.append({'kind':'stale artifact rejection','state':'passed'})
    # Cancel a real job; make sure the queue accepts the next task.
    cancelled=job('clean')
    for _ in range(20):
        check=next(j for j in req('GET','/state')['jobs'] if j['id']==cancelled['id'])
        if check['state']=='running':break
        time.sleep(.2)
    req('POST','/jobs/'+cancelled['id']+'/cancel')
    time.sleep(2)
    check=next(j for j in req('GET','/state')['jobs'] if j['id']==cancelled['id'])
    assert check['state']=='cancelled'
    report.append({'kind':'cancel running job','state':'passed'})
    # Browser test uses a fresh headless Chrome profile, never the user's profile.
    with sync_playwright() as pw:
        browser=pw.chromium.launch(channel='chrome',headless=True)
        page=browser.new_page(viewport={'width':1440,'height':1000},device_scale_factor=1)
        errors=[]
        page.on('pageerror',lambda e:errors.append(str(e)))
        page.goto(BASE+'/#token='+TOKEN)
        page.wait_for_load_state('networkidle')
        print('DOM buttons:',page.get_by_role('button').all_text_contents()[:25],flush=True)
        page.screenshot(path=str(ROOT/'tests/ui-source.png'),full_page=True)
        page.get_by_role('button',name='03 Xóa chữ gốc').click()
        page.wait_for_function("document.querySelector('#canvasHint').classList.contains('hidden')")
        canvas=page.locator('#frameCanvas')
        box=canvas.bounding_box()
        page.mouse.move(box['x']+box['width']*.35,box['y']+box['height']*.70)
        page.mouse.down()
        page.mouse.move(box['x']+box['width']*.65,box['y']+box['height']*.80,steps=5)
        page.mouse.up()
        assert '2 vùng' in page.locator('#regionCount').inner_text()
        page.get_by_role('button',name='Lưu dự án',exact=True).click()
        page.wait_for_function("document.querySelector('#saveStatus').textContent.includes('Đã lưu')")
        page.screenshot(path=str(ROOT/'tests/ui-clean.png'),full_page=True)
        page.get_by_role('button',name='04 Dịch & giọng').click()
        page.get_by_label('translated câu 1',exact=True).fill('Xin chào Việt Nam.')
        page.get_by_role('button',name='Lưu bảng',exact=True).click()
        page.wait_for_function("document.querySelector('#saveStatus').textContent.includes('Đã lưu')")
        time.sleep(.5)
        updated=next(p for p in req('GET','/state')['projects'] if p['id']==pid)
        assert updated['segments'][0]['translated']=='Xin chào Việt Nam.'
        page.get_by_role('button',name='Hàng đợi',exact=False).click()
        page.screenshot(path=str(ROOT/'tests/ui-queue.png'),full_page=True)
        assert page.locator('.job').count()>=7
        assert not errors,errors
        browser.close()
    report.append({'kind':'headless UI create/draw/edit/save/queue','state':'passed'})
    assert hashlib.sha256(fixture.read_bytes()).hexdigest()==initial_hash
    report.append({'kind':'original SHA-256 unchanged','state':'passed','sha256':initial_hash})
    print('ALL INTEGRATION CHECKS PASSED',flush=True)
finally:
    # Cancel only our integration project's active jobs before the helper stops the server.
    try:
        for item in req('GET','/state')['jobs']:
            if item.get('project_id')==locals().get('pid') and item['state'] in {'queued','running'}:
                req('POST','/jobs/'+item['id']+'/cancel')
    except Exception:
        pass
    (ROOT/'tests/integration-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    client.close()
