"""Real API + real DOM UI regression, with no paid processing."""
import json
import atexit
import httpx
from pathlib import Path
from playwright.sync_api import sync_playwright
out=Path(__file__).resolve().parent/'studio-results'
out.mkdir(exist_ok=True)
errors=[];checks=[]
def stop_server():
    try:httpx.post('http://127.0.0.1:19864/api/test/stop',headers={'x-studio-token':'npk-studio-test'},json={},timeout=3)
    except httpx.HTTPError:pass
atexit.register(stop_server)
with sync_playwright() as p:
    browser=p.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':900})
    page.on('pageerror',lambda e:(errors.append(str(e)),print('PAGE ERROR',str(e),flush=True)))
    page.on('console',lambda msg:print('CONSOLE',msg.type,msg.text,flush=True) if msg.type=='error' else None)
    page.goto('http://127.0.0.1:19864/studio#token=npk-studio-test')
    page.wait_for_load_state('networkidle')
    page.wait_for_function("document.querySelector('.brand-mark')?.naturalWidth === 256")
    icon_url=page.locator('link[rel="icon"]').get_attribute('href')
    assert icon_url=='/static/npk-app-icon.ico'
    assert page.request.get('http://127.0.0.1:19864'+icon_url).ok
    checks.append('NPK brand icon loads in the header and the Windows favicon is served')
    page.screenshot(path=str(out/'00-loaded.png'),full_page=True)
    print('INITIAL',page.locator('body').inner_text()[:1800],flush=True)
    page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).wait_for()
    page.screenshot(path=str(out/'01-subtitles.png'),full_page=True)
    page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).fill('Xin chào, bạn khỏe không?')
    page.wait_for_function("document.querySelector('#saved').textContent==='Đã lưu'")
    page.reload();page.wait_for_load_state('networkidle')
    assert page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).input_value()=='Xin chào, bạn khỏe không?'
    checks.append('Subtitle editing persisted through real API and reload')
    page.locator('#followVideo').check()
    editing=page.get_by_role('textbox',name='Bản dịch câu 1',exact=True)
    editing.focus()
    page.locator('#video').evaluate('(v)=>{v.currentTime=3.5;v.dispatchEvent(new Event("timeupdate"))}')
    assert editing.evaluate('(el)=>document.activeElement===el')
    checks.append('Follow-video highlighting does not steal focus while editing')
    page.get_by_role('button',name='Nghe lời thoại',exact=True).click()
    assert page.locator('#extractMethod').input_value()=='asr'
    assert page.locator('[data-extract-for="ocr"]').first.is_hidden()
    assert page.get_by_role('button',name='Vẽ vùng trước',exact=True).is_hidden()
    assert 'Chữ chỉ xuất hiện trên hình' in page.locator('#extractionRangeHint').inner_text()
    page.locator('#extractMethod').select_option('ocr')
    assert page.get_by_role('button',name='Vẽ vùng trước',exact=True).is_visible()
    page.get_by_role('button',name='Hủy',exact=True).click()
    checks.append('Visible speech/image entry points route correctly and hide unrelated settings')
    page.locator('#createSubs').click()
    page.get_by_text('Vùng đọc phụ đề',exact=True).click()
    page.get_by_role('spinbutton',name='Kết thúc (giây) vùng đọc 1',exact=True).fill('6')
    page.get_by_role('spinbutton',name='Kết thúc (giây) vùng đọc 1',exact=True).press('Tab')
    page.wait_for_function("document.querySelector('#saved').textContent==='Đã lưu'")
    page.get_by_role('button',name='Hủy',exact=True).click()
    page.locator('#createSubs').click()
    page.get_by_text('Vùng đọc phụ đề',exact=True).click()
    assert page.get_by_role('spinbutton',name='Kết thúc (giây) vùng đọc 1',exact=True).input_value()=='6'
    assert 'chỉ hoạt động' in page.locator('#extractionRangeHint').inner_text()
    page.locator('#extractStart').fill('2')
    page.locator('#extractEnd').fill('4')
    page.locator('#readWholeVideo').click()
    assert page.locator('#extractStart').input_value()=='0'
    assert page.locator('#extractEnd').input_value()=='8'
    assert page.get_by_role('spinbutton',name='Kết thúc (giây) vùng đọc 1',exact=True).input_value()=='6'
    page.get_by_role('button',name='Hủy',exact=True).click()
    checks.append('Independent reading-region time controls persist without running extraction')
    # UI -> authenticated queue API; real OCR itself is covered by the silent clip test.
    project_id=page.evaluate('localStorage.getItem("npk-current")')
    headers={'x-studio-token':'npk-studio-test'}
    before_project=page.request.get(f'http://127.0.0.1:19864/api/studio/projects/{project_id}',headers=headers).json()
    page.locator('#createSubs').click()
    with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as queued:
        page.get_by_role('button',name='Bắt đầu nhận dạng',exact=True).click()
    job=queued.value.json()
    assert queued.value.ok and job['kind']=='ocr',job
    page.request.post(f'http://127.0.0.1:19864/api/jobs/{job["id"]}/cancel',headers=headers,data={})
    after_project=page.request.get(f'http://127.0.0.1:19864/api/studio/projects/{project_id}',headers=headers).json()
    assert after_project['regions']==before_project['regions']
    assert after_project['segments']==before_project['segments']
    for key in ('method','use_clean','dilate','context','max_side','asr_device','asr_model'):
        assert after_project['params'][key]==before_project['params'][key],key
    assert after_project['params']['extraction_end']==8
    page.locator('#progressDetails').wait_for(state='hidden')
    assert page.locator('#progressToggle').get_attribute('aria-expanded')=='false'
    page.get_by_role('checkbox',name='Chọn câu 1',exact=True).wait_for()
    checks.append('Read-image button enqueues OCR through real API without replacing cues early or changing clean/ASR settings')
    page.get_by_role('checkbox',name='Chọn câu 1',exact=True).check()
    # A failed catalog request must not erase the saved (possibly blank) font.
    page.route('**/api/studio/fonts',lambda route:route.abort())
    page.get_by_role('button',name='Kiểu chữ',exact=True).click()
    page.locator('#retryFonts').wait_for(state='visible')
    assert page.locator('#fontPick').input_value()==''
    page.unroute('**/api/studio/fonts')
    page.locator('#retryFonts').click()
    page.wait_for_function("document.querySelector('#fontPick').options.length>4 && !document.querySelector('#fontPick').disabled")
    assert 'Chưa có phông' in page.locator('#fontStatus').inner_text()
    assert page.locator('#fontCheck').count()==0
    page.locator('#fontPick').select_option('Segoe UI')
    assert 'phông đã cài' in page.locator('#fontStatus').inner_text()
    page.locator('#styleScope').select_option('selected')
    page.locator('#fontPick').select_option('Tahoma')
    page.locator('#video').evaluate('(v)=>{v.currentTime=1}')
    page.locator('[data-key="font_size"]').fill('60');page.locator('[data-key="font_size"]').press('Tab')
    page.wait_for_function("document.querySelector('#caption').style.fontSize.length>0")
    page.screenshot(path=str(out/'02-style.png'),full_page=True)
    assert 'Tahoma' in page.locator('#caption').evaluate('(el)=>el.style.fontFamily')
    page.locator('#styleScope').select_option('all')
    assert page.locator('#fontPick').input_value()=='Segoe UI'
    page.locator('#styleScope').select_option('selected')
    assert page.locator('#fontPick').input_value()=='Tahoma'
    checks.append('Installed font picker handles blank value/retry and separates global/per-cue font with live preview')
    checks.append('Subtitle appearance controls affect preview')
    page.get_by_role('button',name='Kiểm tra',exact=True).click()
    page.get_by_role('button',name='Tôi đã xem và xác nhận',exact=True).wait_for()
    page.screenshot(path=str(out/'03-review.png'),full_page=True)
    page.locator('#reviewReason').fill('Đã kiểm tra toàn bộ clip thử nghiệm.')
    page.get_by_role('button',name='Tôi đã xem và xác nhận',exact=True).click()
    page.wait_for_function("document.querySelector('#subBody').textContent.includes('Bạn đã xác nhận')")
    checks.append('Coverage review requires an explicit persisted reason')
    page.locator('[data-area="voice"]').click()
    page.locator('[data-audition]').first.wait_for()
    assert page.locator('.voice-card').count()>=12
    before=page.locator('.voice-card.selected .choose').get_attribute('data-voice')
    target=page.locator('[data-audition]').nth(1)
    target.click()
    assert page.locator('.voice-card.selected .choose').get_attribute('data-voice')==before
    page.screenshot(path=str(out/'04-voices.png'),full_page=True)
    checks.append('One-click audition enqueues without changing selected voice')
    page.locator('[data-area="settings"]').click()
    page.locator('#apiKey').fill('sk-fake-local-test-key-1234')
    assert page.locator('#keyProvider').input_value()=='deepseek'
    assert page.locator('#connectionModel').input_value()=='deepseek-flash'
    page.locator('#connectionModel').fill('deepseek-chat')
    page.locator('#saveKey').click();page.locator('.key-row').wait_for()
    assert not page.locator('#apiKey').input_value()
    assert 'sk-fake-local-test-key' not in page.locator('#keysList').inner_text()
    page.locator('#checkKey').click()
    page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('tự cập nhật model')")
    assert page.locator('#connectionModel').input_value()=='deepseek-flash'
    sent_checks=[]
    page.on('request',lambda request:sent_checks.append(request) if request.url.endswith('/api/keys/deepseek/check') else None)
    page.locator('#connectionModel').fill('gemini-3.7-flash')
    page.locator('#testGeneration').click()
    assert len(sent_checks)==0
    assert 'không khớp nhà cung cấp' in page.locator('#connectionStatus').inner_text()
    page.locator('#connectionModel').fill('deepseek-flash')
    page.locator('#testGeneration').click()
    page.get_by_role('button',name='Gọi thử',exact=True).click()
    page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Kết nối kiểm thử')")
    assert len(sent_checks)==1
    assert 'sk-fake-local-test-key' not in page.locator('body').inner_text()
    page.locator('#useModel').click()
    project_id=page.evaluate('localStorage.getItem("npk-current")')
    chosen=page.request.get(f'http://127.0.0.1:19864/api/studio/projects/{project_id}',headers={'x-studio-token':'npk-studio-test'}).json()
    assert (chosen['params']['provider'],chosen['params']['translation_model'])==('deepseek','deepseek-flash')
    page.screenshot(path=str(out/'05-connections.png'),full_page=True)
    checks.append('DeepSeek key prefix selects provider/model; catalog refreshes retired ID; mismatch is blocked before request; confirmed generation and applying model use mocked provider only')
    page.locator('#exportTop').click()
    page.locator('#exportCheck .step').first.wait_for()
    page.screenshot(path=str(out/'06-export.png'),full_page=True)
    page.locator('#startExport').click()
    page.get_by_role('dialog').wait_for()
    assert page.locator('#modalTitle').inner_text()=='Xuất video thành phẩm?'
    assert '127.0.0.1' not in page.locator('#modal').inner_text()
    page.get_by_role('button',name='Hủy',exact=True).click()
    checks.append('Branded export confirmation, no native origin dialog')
    page.locator('[data-area="projects"]').click()
    page.get_by_text('Dự án đang mở',exact=True).click()
    page.locator('#duplicateProject').click()
    page.wait_for_function("document.querySelectorAll('[data-open-project]').length===2")
    checks.append('Project duplication with independent ID')
    with page.expect_download() as download:
        page.locator('#backupProject').click()
    backup=out/'project-backup-test.json'
    download.value.save_as(str(backup))
    document=json.loads(backup.read_text(encoding='utf-8'))
    assert document['format']=='npk-project'
    assert 'fake-local-test-key' not in json.dumps(document)
    assert document['segments'][0]['subtitle_style']['font_size']==60
    assert document['segments'][0]['subtitle_style']['font']=='Tahoma'
    assert document['params']['font']=='Segoe UI'
    with page.expect_file_chooser() as chooser:
        page.locator('#restoreProjectFile').click()
    chooser.value.set_files(str(backup))
    page.locator('#backupSource').fill(str(Path(__file__).resolve().parent/'fixtures/demo.mp4'))
    page.get_by_role('button',name='Khôi phục',exact=True).click()
    page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).wait_for()
    assert page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).input_value()=='Xin chào, bạn khỏe không?'
    checks.append('Portable project download/restoration preserves edits and per-cue style; excludes key')
    for w,h in [(1920,1080),(1280,720),(1024,768),(720,720),(400,900)]:
        page.set_viewport_size({'width':w,'height':h})
        page.locator('[data-area="subtitles"]').click()
        page.get_by_role('button',name='Nội dung',exact=True).click()
        page.wait_for_timeout(150)
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),f'overflow {w}'
        page.screenshot(path=str(out/f'layout-{w}.png'),full_page=True)
    checks.append('No horizontal overflow at 1920/1280/1024/720/400')
    page.set_viewport_size({'width':1440,'height':900})
    item=page.request.post('http://127.0.0.1:19864/api/test/partial',headers={'x-studio-token':'npk-studio-test'},data={}).json()
    page.evaluate('(id)=>localStorage.setItem("npk-current",id)',item['id'])
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('#subtitleSourceStatus.warning').wait_for()
    assert '00:14.22' in page.locator('#subtitleSourceStatus').inner_text()
    assert '02:00.00' in page.locator('#subtitleSourceStatus').inner_text()
    page.locator('#inspectSubtitleGap').click()
    page.locator('#translateSubs').click()
    assert '7 câu' in page.locator('#translationSourceNotice').inner_text()
    assert 'không tự đọc thêm chữ' in page.locator('#translationSourceNotice').inner_text()
    page.screenshot(path=str(out/'07-partial-translation.png'),full_page=True)
    page.locator('#readFramesBeforeTranslate').click()
    assert page.locator('#extractMethod').input_value()=='ocr'
    assert page.locator('#extractEnd').input_value()=='120'
    page.get_by_role('button',name='Hủy',exact=True).click()
    checks.append('7-cue/120-second fixture shows missing-range warning and redirects translation to full-video OCR')
    measurements=[]
    import time
    for count in (1000,10000):
        item=page.request.post('http://127.0.0.1:19864/api/test/bulk',headers={'x-studio-token':'npk-studio-test'},data={'count':count}).json()
        page.evaluate('(id)=>localStorage.setItem("npk-current",id)',item['id'])
        start=time.perf_counter();page.reload()
        page.wait_for_function(f'document.querySelector("#projectName").textContent.includes("{count} câu")')
        page.get_by_role('button',name='Nội dung',exact=True).click()
        page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).wait_for()
        elapsed=time.perf_counter()-start
        assert page.locator('.cue').count()==40
        response=page.request.get('http://127.0.0.1:19864/api/studio/state',headers={'x-studio-token':'npk-studio-test'})
        assert len(response.body())<50000
        measurements.append({'cue_count':count,'open_seconds_including_browser_navigation':elapsed,'rendered_rows':40,'summary_bytes':len(response.body())})
    checks.append('1000/10000 cue projects render 40 rows; polling summary stays under 50KB')
    (out/'performance.json').write_text(json.dumps(measurements,indent=2),encoding='utf-8')
    assert not errors,errors
    browser.close()
(out/'browser-results.json').write_text(json.dumps({'checks':checks,'errors':errors,'scope':'isolated real API; provider mocked; workers not executed'},indent=2,ensure_ascii=False),encoding='utf-8')
print(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False),flush=True)
