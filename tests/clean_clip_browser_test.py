"""Real heavy-code-path sample, EOF comparison and non-blocking job UI."""
import atexit
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

BASE='http://127.0.0.1:19864'
HEADERS={'x-studio-token':'npk-studio-test'}
OUT=Path(__file__).parent/'clean-clip-ui-results'
OUT.mkdir(exist_ok=True)
checks,errors=[],[]

def stop():
    try: httpx.post(BASE+'/api/test/stop',headers=HEADERS,json={},timeout=3)
    except httpx.HTTPError: pass
atexit.register(stop)

with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(BASE+'/studio#token=npk-studio-test');page.wait_for_load_state('networkidle')
    expect(page.locator('#projectName')).to_contain_text('Dự án kiểm thử')
    identifier=page.evaluate('localStorage.getItem("npk-current")')
    project_url=BASE+'/api/studio/projects/'+identifier
    original=page.request.get(project_url,headers=HEADERS).json()
    response=page.request.put(BASE+'/api/projects/'+identifier,headers=HEADERS,data={
        'expected_revision':original['updated'],'params':{'method':'telea','temporal':True},
        'regions':[dict(x=100,y=265,w=80,h=35,start=0,end=8)]})
    assert response.ok,response.text()
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('[data-area="clean"]').click()
    expect(page.get_by_role('button',name='Xem thử 3 giây',exact=True)).to_be_visible()
    page.wait_for_function('video.readyState>=2')
    page.locator('#cleanLiveToggle').click()
    page.evaluate('video.currentTime=video.duration')
    page.wait_for_function('!video.seeking && video.currentTime>=7.99')
    with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as received:
        page.locator('#previewClean').click()
    assert received.value.ok,received.value.text()
    job=received.value.json()
    assert job['kind']=='clip' and job['time']==8
    expect(page.locator('#progressDetails')).to_be_hidden()
    assert page.evaluate('video.paused')
    checks.append('At exact EOF, click queues successfully and keeps progress drawer closed')

    response=page.request.post(BASE+'/api/test/complete-clean',headers=HEADERS,data={'id':job['id']},timeout=60000)
    assert response.ok,response.text()
    result=response.json()['result']
    assert result['start']==5 and result['duration']==3 and result['frames']==36
    assert result['verification']['video_frames_decoded']['clean.mp4']==36
    page.wait_for_function('!pollBusy');page.evaluate('poll()')
    expect(page.locator('#compareAfter')).to_be_visible()
    expect(page.locator('#compareConfig')).to_contain_text('00:05.00–00:08.00')
    expect(page.locator('#compareConfig')).to_contain_text('Đã lùi mẫu')
    page.wait_for_function('document.querySelector("#compareBefore").readyState>=2 && document.querySelector("#compareAfter").readyState>=3')
    page.wait_for_function('Math.abs(document.querySelector("#compareBefore").currentTime-5)<.09')
    expect(page.locator('#progressDetails')).to_be_hidden()
    page.locator('#compareConfig').scroll_into_view_if_needed()
    page.screenshot(path=str(OUT/'tail-comparison.png'),full_page=True)
    checks.append('Actual Telea sample is complete, verified, and before/after both start at second 5')
    page.evaluate('document.querySelector("#compareAfter").currentTime=1.5')
    page.wait_for_function('Math.abs(document.querySelector("#compareBefore").currentTime-6.5)<.09')
    page.evaluate('document.querySelector("#compareAfter").play()')
    page.wait_for_function('document.querySelector("#compareAfter").ended',timeout=10000)
    assert page.evaluate('document.querySelector("#compareBefore").paused')
    checks.append('Seek and playback synchronize the adjusted window and stop both viewers at sample end')

    # A failed clean job must not suggest restoring a translation that never ran.
    response=page.request.post(BASE+'/api/test/recognition-state',headers=HEADERS,data={'id':job['id'],'state':'error'})
    assert response.ok
    page.wait_for_function('!pollBusy');page.evaluate('poll()')
    page.locator('#progressToggle').click()
    expect(page.locator('#retryCurrent')).to_be_visible()
    expect(page.locator('#applyJobResult')).to_have_count(0)
    page.locator('#closeProgress').click()
    checks.append('Failed restoration offers retry, not an unrelated restore-translation action')

    # Delayed preflight must not shift the clicked moment as playback continues.
    page.evaluate('video.currentTime=1.25')
    page.wait_for_function('!video.seeking')
    page.route('**/api/preflight',lambda route:(page.wait_for_timeout(900),route.continue_()))
    page.evaluate('video.play()')
    with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as received:
        page.locator('#previewClean').click()
    second=received.value.json()
    assert 1.25<=second['time']<2.1,second['time']
    assert page.evaluate('video.paused')
    assert abs(page.evaluate('video.currentTime')-second['time'])<.09
    expect(page.locator('#progressDetails')).to_be_hidden()
    checks.append('Click time is captured before save/preflight and playback pauses for a stable sample')
    assert not errors,errors
    browser.close()
(OUT/'results.json').write_text(json.dumps(dict(checks=checks,errors=errors),ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(checks=checks,errors=errors),ensure_ascii=False),flush=True)
