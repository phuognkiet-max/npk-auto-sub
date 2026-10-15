"""Isolated editor test server; only synthetic media and in-memory fake credentials."""
import os
from pathlib import Path
import sys
import tempfile
import threading

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
temporary = tempfile.TemporaryDirectory(prefix='npk-editor-', dir=root/'tests')
os.environ['VIETDUB_DATA_DIR'] = temporary.name
os.environ['VIETDUB_TOKEN'] = 'npk-editor-test-token'
import credentials
import providers
import time
# Test-only network boundary: no real provider traffic or account credentials.
def fake_check(provider, secret, model='', mode='catalog', client=None):
    return {'ok':True,'checked_at':time.time(),'scope':mode,'model':model,
        'status':'catalog_ok' if mode=='catalog' else 'ready',
        'message':'TEST DOUBLE · danh mục model' if mode=='catalog' else 'TEST DOUBLE · model trả lời',
        **({'models':['gemini-test-catalog']} if mode=='catalog' else {})}
providers.check_connection=fake_check
fake_keys = {'gemini': 'fake-editor-key-ABCD'}
credentials.keyring.get_password = lambda service, provider: fake_keys.get(provider)
credentials.keyring.set_password = lambda service, provider, value: fake_keys.__setitem__(provider, value)
credentials.keyring.delete_password = lambda service, provider: fake_keys.pop(provider, None)
from core import create_project, put
import server
import uvicorn

project = create_project(str(root/'tests/fixtures/demo.mp4'), 'Kiểm thử editor · video tổng hợp')
project['regions'] = [dict(x=100, y=280, w=400, h=50, start=0, end=8)]
project['segments'] = [dict(start=.2, end=2.7, text='Original subtitle', translated='Xin chào các bạn.'),
                       dict(start=3, end=5, text='Second line', translated='Đây là video thử nghiệm.')]
project['params'].update(method='telea', workload_mode='quiet', font_size=24)
put('project', project['id'], project)
http = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=18774, log_level='error'))

@server.app.post('/api/test/shutdown')
def finish():
    http.should_exit = True
    return {'stopped': True}

timer = threading.Timer(300, lambda: setattr(http, 'should_exit', True))
timer.daemon = True
timer.start()
server.start_workers()
try:
    http.run()
finally:
    server.shutdown()
    timer.cancel()
    temporary.cleanup()
