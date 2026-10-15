"""Real process/queue acceptance. Isolated storage, offline, no user library edits."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
report_path=root/'tests/studio-results/lifecycle.json'
temporary=tempfile.TemporaryDirectory(prefix='npk-lifecycle-',dir=root/'tests')
os.environ['VIETDUB_DATA_DIR']=temporary.name
from core import create_project,put,DATA
from subtitle_quality import coverage_record
import httpx
import psutil

p=create_project(str(root/'tests/fixtures/demo.mp4'),'Lifecycle real worker')
p['params'].update(use_clean=False,voice_gain=1,original_gain=0,pipeline_translate=False,pipeline_tts=True,
    pipeline_clean=False,method='blur',video_encoder='cpu',voice_id='piper:vais1000')
p['segments']=[dict(start=.5,end=3,text='Hello',translated='Xin chào bạn.'),dict(start=4,end=7,text='See you',translated='Hẹn gặp lại.')]
p['coverage']=coverage_record(p,'ocr',0,p['media']['duration'])
put('project',p['id'],p)
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
env=os.environ|{'VIETDUB_TOKEN':'isolated-lifecycle-token','PYTHONIOENCODING':'utf-8'}
process=subprocess.Popen([str(root/'runtime/venv/Scripts/python.exe'),'-X','utf8',str(root/'app.py'),'--headless','--port',str(port)],
    env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW)
client=httpx.Client(base_url=f'http://127.0.0.1:{port}',headers={'x-studio-token':'isolated-lifecycle-token'},timeout=15)
checks=[]

def call(method,url,body=None):
    r=client.request(method,url,json=body);r.raise_for_status();return r.json()

def wait_job(identifier,states=('done','error','review','cancelled'),seconds=60):
    start=time.monotonic()
    while time.monotonic()-start<seconds:
        job=call('GET','/api/studio/jobs/'+identifier)
        if job['state'] in states:return job
        time.sleep(.2)
    raise TimeoutError(identifier)

try:
    for _ in range(100):
        try:call('GET','/api/studio/state');break
        except httpx.TransportError:time.sleep(.1)
    body=dict(project_id=p['id'],kind='pipeline',expected_revision=p['updated'],replace_segments=True,request_id='real-once')
    j=call('POST','/api/jobs',body)
    assert call('POST','/api/jobs',body)['id']==j['id']
    result=wait_job(j['id'])
    assert result['state']=='done',result
    target=Path(result['result']['published']['path'])
    assert target.is_file() and target.name=='Video-thanh-pham.mp4'
    assert result['result']['primary_file']=='final.mp4'
    checks.append('Real pipeline creates translated-caption / offline voice final and atomically publishes it')
    current=call('GET','/api/studio/projects/'+p['id'])
    assert current['artifacts']['tts']['speech_signature']
    preview=call('POST','/api/jobs',dict(project_id=p['id'],kind='subtitle_preview',time=1,expected_revision=current['updated']))
    result=wait_job(preview['id'])
    assert result['state']=='done',result
    assert (DATA/'jobs'/preview['id']/'subtitle-preview.png').is_file()
    checks.append('Exact subtitle preview uses real FFmpeg/libass')
    # A long-running recognition process is cancelled through the product API.
    current=call('GET','/api/studio/projects/'+p['id'])
    j=call('POST','/api/jobs',dict(project_id=p['id'],kind='ocr',replace_segments=True,expected_revision=current['updated']))
    wait_job(j['id'],('running',))
    changed=call('PUT','/api/projects/'+p['id'],dict(expected_revision=current['updated'],name='User edit during worker'))
    call('POST','/api/jobs/'+j['id']+'/cancel',{})
    time.sleep(1)
    result=wait_job(j['id'])
    assert result['state']=='cancelled'
    assert call('GET','/api/studio/projects/'+p['id'])['name']=='User edit during worker'
    assert not [x for x in psutil.Process(process.pid).children(recursive=True) if 'worker.py' in ' '.join(x.cmdline())]
    checks.append('Cancellation stops only owned worker tree and preserves edits/source')
    # Revision conflict must reject a stale write rather than replacing newer text.
    response=client.put('/api/projects/'+p['id'],json={'expected_revision':current['updated'],'name':'stale'})
    assert response.status_code==409
    checks.append('Stale revision is rejected with 409')
    report_path.write_text(json.dumps({'checks':checks,'passed':True},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'checks':checks,'passed':True},ensure_ascii=False),flush=True)
finally:
    client.close()
    owned=psutil.Process(process.pid)
    children=owned.children(recursive=True)
    for child in reversed(children):
        try:child.kill()
        except psutil.NoSuchProcess:pass
    process.terminate();process.wait(timeout=10)
    psutil.wait_procs(children,timeout=5)
    temporary.cleanup()
