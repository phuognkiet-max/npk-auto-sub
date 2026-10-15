"""Inspect live effects on the reported source through an isolated test project."""
import atexit
import json
from pathlib import Path
import sqlite3
import httpx
from playwright.sync_api import sync_playwright, expect
root=Path(__file__).resolve().parents[1]
BASE='http://127.0.0.1:19864'
HEADERS={'x-studio-token':'npk-studio-test'}
OUT=root/'tests/live-clean-ui-results'
def stop():
    try: httpx.post(BASE+'/api/test/stop',headers=HEADERS,json={},timeout=3)
    except httpx.HTTPError: pass
atexit.register(stop)
with sqlite3.connect(f'file:{root.as_posix()}/data/studio.sqlite3?mode=ro',uri=True) as db:
    original=json.loads(db.execute('SELECT value FROM objects WHERE kind=? AND id=?',
        ('project','29463f3e388847e3afd7e1eabf93ada7')).fetchone()[0])
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1920,'height':1080})
    page.goto(BASE+'/studio#token=npk-studio-test');page.wait_for_load_state('networkidle')
    expect(page.locator('#projectName')).to_contain_text('Dự án kiểm thử')
    response=page.request.post(BASE+'/api/projects',headers=HEADERS,data={'path':original['media']['path'],'name':'Kiểm thử riêng · preview trực tiếp'})
    assert response.ok,response.text()
    p=response.json()
    assert page.request.put(BASE+'/api/projects/'+p['id'],headers=HEADERS,data={
        'expected_revision':p['updated'],'regions':original['regions'],
        'params':dict(method='blur',blur_radius=32,cover_opacity=1,edge_feather=4,dilate=4)}).ok
    page.evaluate('id=>loadProject(id)',p['id'])
    page.locator('[data-area="clean"]').click()
    expect(page.locator('#cleanLiveCanvas')).to_be_visible()
    page.wait_for_function('video.readyState>=2 && !video.seeking')
    page.screenshot(path=str(OUT/'vlog-blur-live.png'),full_page=True)
    timings=page.evaluate('''() => {
      const s=cleanLiveSnapshot(), times=[];
      for(let i=0;i<10;i++){const t=performance.now();cleanLive.quick(s,s.regions);times.push(performance.now()-t)}
      return times;
    }''')
    page.locator('.param[data-key="method"]').select_option('stretch')
    page.get_by_role('slider',name='Khoảng cách đến dải nền',exact=True).fill('20')
    page.evaluate('cleanLive.paint()')
    page.screenshot(path=str(OUT/'vlog-stretch-live.png'),full_page=True)
    assert not page.request.get(BASE+'/api/studio/state',headers=HEADERS).json()['jobs']
    browser.close()
(OUT/'vlog-live-timing.json').write_text(json.dumps({'blur_paint_ms':timings,'source_resolution':[1920,1080]}),encoding='utf-8')
print(json.dumps({'blur_paint_ms':timings,'real_project_modified':False}),flush=True)
