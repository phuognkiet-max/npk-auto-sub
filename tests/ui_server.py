"""Disposable test server. Never reads or writes real credentials or project DB."""
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
temporary=tempfile.TemporaryDirectory(prefix='npk-ui-',dir=root/'tests')
os.environ['VIETDUB_DATA_DIR']=temporary.name
os.environ['VIETDUB_TOKEN']='npk-ui-test-token'
import credentials
fake_keys={'gemini':'fake-unit-key-ABCD'}
credentials.keyring.get_password=lambda service,provider:fake_keys.get(provider)
credentials.keyring.set_password=lambda service,provider,value:fake_keys.__setitem__(provider,value)
credentials.keyring.delete_password=lambda service,provider:fake_keys.pop(provider,None)
from core import create_project,put
import server
import uvicorn

project=create_project(r'D:\Dựng video\NPK Auto Sub\Tests\NPK-0.2-20260903\source-3s.mp4','Kiểm thử UI · video mèo')
project['regions']=[dict(x=79,y=918,w=1770,h=135,start=0,end=3)]
project['segments']=[dict(start=0,end=2.7,text='请举起双手',translated='Hãy giơ hai tay lên.')]
put('project',project['id'],project)
http=uvicorn.Server(uvicorn.Config(server.app,host='127.0.0.1',port=18773,log_level='error',access_log=False))
@server.app.post('/api/test/shutdown')
def finish():
    http.should_exit=True
    return {'stopped':True}
# A finite fallback lifetime prevents a failed browser test leaving a test server.
threading.Timer(180,lambda:setattr(http,'should_exit',True)).start()
server.start_workers()
try:
    http.run()
finally:
    server.shutdown()
    # Timer must not keep the disposable process alive.
    for thread in threading.enumerate():
        if isinstance(thread,threading.Timer):thread.cancel()
    temporary.cleanup()
