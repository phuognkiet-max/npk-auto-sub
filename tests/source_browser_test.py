"""Source replacement uses isolated real files/API; native picker return is simulated."""
import atexit
import json
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:19864'
HEADERS = {'x-studio-token':'npk-studio-test'}
OUT = Path(__file__).parent/'source-results'
OUT.mkdir(exist_ok=True)
checks, errors = [], []


def stop():
    try:
        httpx.post(BASE+'/api/test/stop', headers=HEADERS, json={}, timeout=3)
    except httpx.HTTPError:
        pass


atexit.register(stop)
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width':1440,'height':900})
    page.on('pageerror', lambda e:errors.append(str(e)))
    fixture = page.request.post(BASE+'/api/test/source-fixture', headers=HEADERS, data={}).json()
    page.goto(BASE+'/studio#token=npk-studio-test')
    page.wait_for_load_state('networkidle')
    expect(page.get_by_role('button',name='Thay video nguồn',exact=True)).to_be_visible()
    identifier = page.evaluate('localStorage.getItem("npk-current")')
    project_url = BASE+'/api/studio/projects/'+identifier
    before = page.request.get(project_url, headers=HEADERS).json()
    page.locator('#replaceSource').click()
    expect(page.get_by_role('dialog')).to_be_visible()
    page.locator('#replacementPath').fill(fixture['different'])
    # A cancelled file picker must retain the typed path.
    page.evaluate('window.pywebview = {api:{pick_video:async()=>null}}')
    page.get_by_role('button',name='Chọn tệp',exact=True).click()
    expect(page.locator('#replacementPath')).to_have_value(fixture['different'])
    page.get_by_role('button',name='Hủy',exact=True).click()
    assert page.request.get(project_url,headers=HEADERS).json()==before
    checks.append('Visible source action; cancelling dialog/file picker leaves the project unchanged')

    page.locator('#replaceSource').click()
    page.evaluate('(path)=>window.pywebview.api.pick_video=async()=>path', fixture['same'])
    page.get_by_role('button',name='Chọn tệp',exact=True).click()
    expect(page.locator('#replacementPath')).to_have_value(fixture['same'])
    page.locator('#replacementMode').select_option('keep')
    with page.expect_response(lambda r:r.url.endswith('/replace-source')) as response:
        page.get_by_role('button',name='Thay video',exact=True).click()
    assert response.value.ok
    expect(page.get_by_role('dialog')).to_be_hidden()
    after = page.request.get(project_url,headers=HEADERS).json()
    assert after['media']['path']==fixture['same']
    assert after['segments']==before['segments']
    assert after['regions']==before['regions']
    assert after['artifacts']=={}
    checks.append('Picker result reaches real API; keeping matching content preserves subtitle edits')

    # Pending edits must be saved into history before starting fresh.
    page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).fill('Bản cần giữ trong lịch sử')
    page.locator('#replaceSource').click()
    expect(page.locator('#replacementMode')).to_have_value('reset')
    page.locator('#replacementPath').fill(fixture['different'])
    with page.expect_response(lambda r:r.url.endswith('/replace-source')) as response:
        page.get_by_role('button',name='Thay video',exact=True).click()
    assert response.value.ok
    expect(page.get_by_role('dialog')).to_be_hidden()
    page.wait_for_function('document.querySelector("#video").videoWidth===160 && document.querySelector("#video").videoHeight===240')
    after = page.request.get(project_url,headers=HEADERS).json()
    assert after['media']['path']==fixture['different']
    assert after['segments']==[] and after['extraction_regions']==[] and after['artifacts']=={}
    assert after['id']==before['id'] and after['name']==before['name']
    expect(page.locator('#sourceMeta')).to_contain_text('160 × 240')
    assert page.locator('#viewSource option').count()==1
    expect(page.locator('#undo')).to_be_disabled()
    expect(page.locator('#progressDetails')).to_be_hidden()
    backup = page.request.get(project_url+'/revisions', headers=HEADERS).json()[0]
    restored = page.request.post(project_url+'/restore',headers=HEADERS,data={
        'expected_revision':after['updated'], 'revision':backup['updated']}).json()
    assert restored['segments'][0]['translated']=='Bản cần giữ trong lịch sử'
    assert restored['media']['path']==fixture['same']
    page.reload(); page.wait_for_load_state('networkidle')
    checks.append('Different dimensions/duration replace correctly, stale outputs disappear, saved edits restore from history')

    for width in (1920,1440,1280,1024,720,400):
        page.set_viewport_size({'width':width,'height':900})
        button=page.locator('#replaceSource')
        expect(button).to_be_visible()
        rect=button.bounding_box()
        assert rect['x']>=0 and rect['x']+rect['width']<=width+1, width
        viewer=page.locator('#viewer').bounding_box()
        scrub=page.locator('#scrub').bounding_box()
        assert scrub['y']+scrub['height'] <= viewer['y']+viewer['height'], width
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), width
        page.screenshot(path=str(OUT/f'layout-{width}.png'),full_page=True)
    checks.append('Source action stays visible without horizontal overflow at 1920/1440/1280/1024/720/400')
    assert not errors, errors
    browser.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False),flush=True)
