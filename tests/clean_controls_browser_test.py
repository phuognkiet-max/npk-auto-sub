"""Real UI, queue API, preview images and lightweight video on an isolated fixture."""
import atexit
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

BASE='http://127.0.0.1:19864'
HEADERS={'x-studio-token':'npk-studio-test'}
OUT=Path(__file__).parent/'clean-controls-ui-results'
OUT.mkdir(exist_ok=True)
checks, errors, queued=[],[],[]

def stop():
    try: httpx.post(BASE+'/api/test/stop',headers=HEADERS,json={},timeout=3)
    except httpx.HTTPError: pass

atexit.register(stop)
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:queued.append(r.post_data_json) if r.method=='POST' and r.url.endswith('/api/jobs') else None)
    page.goto(BASE+'/studio#token=npk-studio-test');page.wait_for_load_state('networkidle')
    expect(page.locator('#projectName')).to_contain_text('Dự án kiểm thử')
    assert 'Chữ gốc' in page.locator('#nav').inner_text()
    identifier=page.evaluate('localStorage.getItem("npk-current")')
    project_url=BASE+'/api/studio/projects/'+identifier
    original=page.request.get(project_url,headers=HEADERS).json()
    assert page.request.put(BASE+'/api/projects/'+identifier,headers=HEADERS,data={
        'expected_revision':original['updated'],'regions':[dict(x=100,y=265,w=420,h=75,start=0,end=8)]}).ok
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('[data-area="clean"]').click()
    expect(page.get_by_role('slider',name='Làm mờ',exact=True)).to_be_visible()
    expect(page.locator('#caption')).to_be_hidden()

    def slider(name, value):
        control=page.get_by_role('slider',name=name,exact=True)
        control.fill(str(value));control.dispatch_event('input');control.dispatch_event('change')
        expect(page.locator('#saved')).to_have_text('Đã lưu')

    def start(button):
        with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as response:
            button.click()
        assert response.value.ok,response.value.text()
        return response.value.json()

    def complete(job):
        response=page.request.post(BASE+'/api/test/complete-clean',headers=HEADERS,data={'id':job['id']},timeout=60000)
        assert response.ok,response.text()
        page.wait_for_function('!pollBusy');page.evaluate('poll()')

    slider('Làm mờ',32)
    slider('Độ đậm',.8)
    slider('Mềm viền',8)
    assert not queued
    p=page.request.get(project_url,headers=HEADERS).json()
    assert p['params']['blur_radius']==32 and p['params']['cover_opacity']==.8 and p['params']['edge_feather']==8
    expect(page.locator('#cleanReadyStatus')).to_contain_text('Chưa có video đã xử lý')
    checks.append('Three visible sliders persist numeric parameters; dragging never starts heavy work automatically')
    page.screenshot(path=str(OUT/'01-sliders.png'),full_page=True)

    job=start(page.locator('#previewFrame'));assert job['kind']=='preview'
    complete(job)
    expect(page.locator('#frameComparisonImage')).to_be_visible()
    page.wait_for_function('document.querySelector("#frameComparisonImage")?.naturalWidth > 0')
    expect(page.locator('#frameCompareConfig')).to_contain_text('khớp thông số')
    expect(page.locator('#progressDetails')).to_be_hidden()
    page.locator('#frameBefore').click()
    expect(page.locator('#frameComparisonImage')).to_have_attribute('src',__import__('re').compile('before.jpg'))
    page.locator('#frameAfter').click()
    expect(page.locator('#frameComparisonImage')).to_have_attribute('src',__import__('re').compile('after.jpg'))
    page.locator('#frameEnlarge').click();expect(page.locator('#largeCleanFrame')).to_be_visible()
    page.locator('#modalClose').click()
    page.screenshot(path=str(OUT/'02-real-preview.png'),full_page=True)
    checks.append('Real one-frame output appears with before/after and enlarge; progress details do not auto-open')

    slider('Độ đậm',1)
    expect(page.locator('#frameCompareConfig')).to_contain_text('đã đổi')
    assert len(queued)==1
    page.locator('.param[data-key="method"]').select_option('stretch')
    expect(page.get_by_role('slider',name='Dải nền',exact=True)).to_be_visible()
    expect(page.get_by_role('slider',name='Làm mờ',exact=True)).to_have_count(0)
    slider('Khoảng cách',10)
    checks.append('Changing parameters invalidates old previews; method-specific controls replace unrelated controls')

    job=start(page.locator('#previewClean'));assert job['kind']=='clip'
    complete(job)
    expect(page.locator('#compareAfter')).to_be_visible()
    expect(page.locator('#compareConfig')).to_contain_text('Khớp thông số')
    checks.append('Three-second preview uses a real encoded clip and reports matching settings')

    page.locator('#cleanAll').click()
    job=start(page.get_by_role('button',name='Bắt đầu',exact=True));assert job['kind']=='clean'
    complete(job)
    expect(page.locator('#viewSource')).to_have_value('clean')
    expect(page.locator('#video')).to_have_attribute('src',__import__('re').compile(job['id']+'/clean.mp4'))
    expect(page.locator('#cleanReadyStatus')).to_contain_text('khớp thông số')
    page.wait_for_function('document.querySelector("#video").readyState >= 2')
    page.screenshot(path=str(OUT/'03-completed-clean-selected.png'),full_page=True)
    page.locator('#showOriginalVideo').click();expect(page.locator('#viewSource')).to_have_value('source')
    page.locator('#showCleanVideo').click();expect(page.locator('#viewSource')).to_have_value('clean')
    checks.append('Completion selects processed video, not the source; explicit before/after buttons switch real files')

    # Completion during an edit must be deferred, not lost or allowed to overwrite.
    job=start(page.locator('#previewFrame'))
    page.evaluate('change(p => p.params.stretch_height = 20); clearTimeout(saveTimer)')
    complete(job)
    assert page.evaluate('P.params.stretch_height')==20
    assert page.evaluate('pendingCompletedJob?.id')==job['id']
    page.evaluate('save()');page.wait_for_function('!pollBusy');page.evaluate('poll()')
    expect(page.locator('#frameCompareConfig')).to_contain_text('đã đổi')
    assert page.evaluate('P.params.stretch_height')==20
    assert page.evaluate('pendingCompletedJob') is None
    checks.append('Completion during unsaved edits is deferred and later shown as stale without overwriting edits')

    page.locator('.param[data-key="method"]').select_option('telea')
    expect(page.get_by_role('slider',name='Bán kính',exact=True)).to_be_visible()
    expect(page.locator('#panel')).not_to_contain_text('xử lý từng khung')
    page.locator('#cleanHelp').click()
    expect(page.locator('#modalBody')).to_contain_text('xử lý từng khung')
    expect(page.locator('#modalBody')).to_contain_text('có thể chậm')
    expect(page.locator('#modalBody')).to_contain_text('nhấp nháy')
    page.locator('#modalClose').click()
    page.get_by_text('Tùy chỉnh nâng cao',exact=True).click()
    mask_choice=page.locator('.param[data-key="mask_mode"]')
    expect(mask_choice).to_be_visible()
    expect(mask_choice.locator('..')).to_contain_text('Phạm vi phục hồi')
    mask_choice.select_option('glyph')
    expect(page.locator('#saved')).to_have_text('Đã lưu')
    assert page.request.get(project_url,headers=HEADERS).json()['params']['mask_mode']=='glyph'
    for width in (1920,1280,1024,720,400):
        page.set_viewport_size({'width':width,'height':1000})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
    assert not errors,errors
    checks.append('Inpainting exposes radius and expandable mask controls; quality limitations remain in help; layout fits 1920 through 400 pixels without horizontal overflow')
    browser.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False),flush=True)
