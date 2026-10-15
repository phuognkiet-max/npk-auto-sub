"""Isolated real API for browser tests; no provider calls or user credentials."""
import os
from pathlib import Path
import sys
import tempfile
import threading
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
temporary=tempfile.TemporaryDirectory(prefix='npk-studio-ui-',dir=root/'tests')
os.environ['VIETDUB_DATA_DIR']=temporary.name
os.environ['VIETDUB_TOKEN']='npk-studio-test'
import credentials
fake={}
credentials.keyring.get_password=lambda service,key:fake.get(key)
credentials.keyring.set_password=lambda service,key,value:fake.__setitem__(key,value)
credentials.keyring.delete_password=lambda service,key:fake.pop(key,None)
import providers
def fake_provider_check(provider, secret, model='', mode='catalog', **kwargs):
    models = (['test-router/translator', 'test-router/vision-reader', 'oc/muse-spark-1.3-contributor-free'] if provider == 'router9'
              else ['gemini-3.7-flash'] if provider == 'gemini' else ['deepseek-flash', 'deepseek-v4-pro'])
    if mode == 'generation':
        return dict(ok=True,status='ready',scope=mode,
            message='Kết nối kiểm thử: model trả lời; không gọi dịch vụ thật. Ghi nhận phép thử: 1 request (HTTP 200); API báo 2 token; chi phí chưa xác minh.',
            api_usage={'scope':'generation','request_attempts':1,'responses_received':1,
                'http_statuses':[200],'usage_reported_requests':1,'usage_unknown_requests':0,
                'reported_tokens':{'input_tokens':1,'output_tokens':1,'total_tokens':2,
                    'thinking_tokens':0,'cached_input_tokens':0},'cost_verification':'unverified'},checked_at=1)
    return dict(ok=True,status='catalog_ok',scope=mode,
        **({'base_url': kwargs.get('base_url')} if provider == 'router9' else {}),
        message='Kết nối kiểm thử; đã tải danh mục giả. Đã ghi nhận 1 request tải danh mục; không gửi prompt dịch.',
        api_usage={'scope':'catalog','request_attempts':1,'responses_received':1,
            'http_statuses':[200],'usage_reported_requests':0,'usage_unknown_requests':0,
            'reported_tokens':{'input_tokens':0,'output_tokens':0,'total_tokens':0,
                'thinking_tokens':0,'cached_input_tokens':0},'cost_verification':'not_applicable_to_generation'},
        models=models,checked_at=1)
providers.check_connection=fake_provider_check
from core import create_project,put
project=create_project(str(root/'tests/fixtures/demo.mp4'),'Dự án kiểm thử · Phụ đề và giọng')
project['params'].update(use_clean=False,voice_gain=0,pipeline_tts=False,pipeline_translate=False,method='blur',font='')
project['segments']=[dict(start=.5,end=2.5,text='Hello there',translated='Xin chào bạn.'),dict(start=3,end=5,text='See you',translated='Hẹn gặp lại.')]
project['extraction_regions']=[dict(x=40,y=285,w=560,h=50,start=0,end=8)]
put('project',project['id'],project)
import server,uvicorn
http=uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=19864,log_level='warning',access_log=False))
@server.app.post('/api/test/stop')
def stop():
    http.should_exit=True
    return {}
@server.app.post('/api/test/bulk')
def bulk(body:dict):
    count=min(10000,int(body.get('count',1000)))
    p=create_project(str(root/'tests/fixtures/demo.mp4'),f'Kiểm hiệu năng {count} câu')
    p['segments']=[dict(start=i/count*8,end=(i+1)/count*8,text=f'Source {i+1}',translated=f'Bản dịch {i+1}') for i in range(count)]
    put('project',p['id'],p)
    return {'id':p['id']}
@server.app.post('/api/test/partial')
def partial():
    # Metadata-only UI fixture: never run workers against this simulated duration.
    p=create_project(str(root/'tests/fixtures/demo.mp4'),'Kiểm bảng thiếu · 7 câu / 120 giây')
    p['media']['duration']=120
    p['segments']=[dict(start=i*2,end=i*2+2.22,text=f'源 {i}',translated=f'Câu {i}') for i in range(7)]
    put('project',p['id'],p)
    return {'id':p['id']}

@server.app.post('/api/test/storage-fixture')
def storage_fixture():
    import uuid
    from core import DATA, get
    p=create_project(str(root/'tests/fixtures/demo.mp4'),'Dự án kiểm thử xóa an toàn')
    p['segments']=[dict(start=0,end=1,text='Keep subtitles',translated='Giữ phụ đề')]
    put('project',p['id'],p)
    jobs=[]
    for name in ('old-preview.jpg','clean.mp4','final.mp4'):
        identifier=uuid.uuid4().hex
        folder=DATA/'jobs'/identifier
        folder.mkdir()
        (folder/name).write_bytes(b'generated-test-data' * 128)
        j=dict(id=identifier, project_id=p['id'],project_name=p['name'],state='done',kind='clean',
               created=1,updated=1,progress=1,message='Fixture',result={'files':[name]})
        put('job',identifier,j)
        jobs.append(identifier)
    p['artifacts']={'clean':{'job_id':jobs[1]}}
    put('project',p['id'],p)
    return {'id':p['id'],'name':p['name'],'jobs':jobs}

@server.app.post('/api/test/source-fixture')
def source_fixture():
    import shutil
    from core import DATA, binary, run
    copied = DATA/'video cùng nội dung.mp4'
    portrait = DATA/'video dọc mới.mp4'
    shutil.copyfile(root/'tests/fixtures/demo.mp4', copied)
    run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i', 'color=c=blue:s=160x240:r=10',
         '-t', '2', '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-y', portrait], timeout=20)
    return {'same':str(copied), 'different':str(portrait)}

@server.app.post('/api/test/recognition-state')
def recognition_state(body: dict):
    from core import get
    j=get('job',body['id'])
    j.update(state=body['state'],progress=body.get('progress',0),message='Trạng thái kiểm thử')
    put('job',j['id'],j)
    return j

@server.app.post('/api/test/complete-clean')
def complete_clean(body: dict):
    # Test-only endpoint: real lightweight media work, no queue thread/provider.
    import json
    from core import DATA, get
    from worker import preview, clean
    j=get('job',body['id'])
    assert j and j['kind'] in ('preview','clip','clean')
    folder=DATA/'jobs'/j['id']
    snapshot=json.loads((folder/'input.json').read_text(encoding='utf-8'))['project']
    assert snapshot['media']['path']==str(root/'tests/fixtures/demo.mp4')
    assert snapshot['params']['method'] in ('blur','stretch','box','telea')
    result=(preview if j['kind']=='preview' else clean)(snapshot,j,folder)
    from job_progress import verify_outputs
    result['verification'] = verify_outputs(result,snapshot,j,folder)
    p=get('project',j['project_id'])
    p['artifacts'][j['kind']]=result | {'job_id':j['id']}
    put('project',p['id'],p)
    j.update(state='done',progress=1,result=result)
    put('job',j['id'],j)
    return j
timer=threading.Timer(600,lambda:setattr(http,'should_exit',True));timer.daemon=True;timer.start()
try:http.run()
finally:timer.cancel();temporary.cleanup()
