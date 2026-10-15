"""Real authenticated queue/state API, simulated worker progress in isolated test data."""
import atexit
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright, expect

BASE='http://127.0.0.1:19864'
HEADERS={'x-studio-token':'npk-studio-test'}
OUT=Path(__file__).parent/'recognition-ui-results'
OUT.mkdir(exist_ok=True)
checks, errors, queued=[] , [], []


def stop():
    try: httpx.post(BASE+'/api/test/stop',headers=HEADERS,json={},timeout=3)
    except httpx.HTTPError: pass


atexit.register(stop)
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':900})
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('request',lambda request:queued.append(request.post_data_json)
            if request.method=='POST' and request.url.endswith('/api/jobs') else None)
    page.goto(BASE+'/studio#token=npk-studio-test')
    page.wait_for_load_state('networkidle')
    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#listenSubs')).to_be_visible()
    identifier=page.evaluate('localStorage.getItem("npk-current")')
    project_url=BASE+'/api/studio/projects/'+identifier
    original=page.request.get(project_url,headers=HEADERS).json()
    response=page.request.put(BASE+'/api/projects/'+identifier,headers=HEADERS,data={
        'expected_revision':original['updated'],'segments':[]})
    assert response.ok
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#createSubs')).to_have_attribute('aria-pressed','false')
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','false')
    page.locator('#createSubs').click()
    expect(page.locator('#recognitionStartTitle')).to_have_text('Quét video để tìm phụ đề trên hình')
    expect(page.locator('#recognitionAdvanced')).not_to_have_attribute('open','')
    expect(page.locator('#extractMethod')).to_be_hidden()
    expect(page.locator('#modalActions button.primary')).to_have_text('Quét video và tạo phụ đề')
    page.locator('#recognitionAdvanced > summary').click()
    expect(page.locator('#extractMethod')).to_have_value('ocr')
    expect(page.locator('#extractStart')).to_have_value('0')
    assert float(page.locator('#extractEnd').input_value()) == float(original['media']['duration'])
    page.get_by_role('button',name='Hủy',exact=True).click()
    checks.append('OCR opens as a one-action whole-video scan; method/range/ROI controls stay in advanced settings')
    page.locator('#listenSubs').click()
    page.locator('#extractMethod').select_option('ocr')
    page.get_by_role('button',name='Hủy',exact=True).click()
    expect(page.locator('#createSubs')).to_have_attribute('aria-pressed','false')
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','false')
    checks.append('No hard-coded OCR highlight; cancelling a dialog does not pretend a method ran')

    page.route('**/api/preflight', lambda route:route.fulfill(json={'ok':False,'errors':['Không thể nhận dạng trong phép thử']}))
    page.locator('#listenSubs').click()
    page.locator('#modalActions button.primary').click()
    expect(page.locator('#notice')).to_contain_text('Không thể nhận dạng trong phép thử')
    expect(page.locator('#listenSubs')).to_be_enabled()
    expect(page.locator('#createSubs')).to_be_enabled()
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','false')
    expect(page.locator('#recognitionStatus')).to_be_hidden()
    assert not queued
    page.unroute('**/api/preflight')
    checks.append('Rejected preflight restores controls without a fake running state or queued job')

    def start(method):
        page.locator('#listenSubs' if method=='asr' else '#createSubs').click()
        with page.expect_response(lambda r:r.url.endswith('/api/jobs') and r.request.method=='POST') as result:
            page.locator('#modalActions button.primary').click()
        assert result.value.ok, result.value.text()
        expect(page.locator('#recognitionTitle')).to_contain_text('Đang chờ')
        return result.value.json()

    def advance(job, state, progress=.4):
        response=page.request.post(BASE+'/api/test/recognition-state',headers=HEADERS,data={
            'id':job['id'],'state':state,'progress':progress})
        assert response.ok
        page.wait_for_function('!pollBusy');page.evaluate('poll()')

    job=start('asr')
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','true')
    expect(page.locator('#createSubs')).to_have_attribute('aria-pressed','false')
    expect(page.locator('#listenSubs')).to_be_disabled()
    expect(page.locator('#createSubs')).to_be_disabled()
    expect(page.locator('#emptyCueRows')).to_be_hidden()
    expect(page.locator('#progressDetails')).to_be_hidden()
    assert page.locator('#regions .region').count()==0
    advance(job,'running',.41)
    expect(page.locator('#recognitionTitle')).to_have_text('Nghe lời thoại · Đang xử lý · 41%')
    expect(page.locator('#progressText')).to_contain_text('41%')
    page.locator('#listenSubs').evaluate('(b)=>{b.click();b.click()}')
    assert len(queued)==1
    checks.append('Real ASR queue/running state highlights ASR only, disables duplicate starts, replaces misleading empty text')
    page.screenshot(path=str(OUT/'01-asr-running.png'),full_page=True)

    page.locator('[data-area="projects"]').click()
    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','true')
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#recognitionTitle')).to_contain_text('41%')
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','true')
    expect(page.locator('#progressDetails')).to_be_hidden()
    checks.append('Navigation/reload preserve real method and do not auto-open progress details')
    page.locator('#recognitionDetails').click()
    expect(page.locator('#progressDetails')).to_be_visible()
    page.locator('#closeProgress').click()
    advance(job,'error')
    expect(page.locator('#recognitionTitle')).to_contain_text('Đã dừng do lỗi')
    expect(page.locator('#listenSubs')).to_be_enabled()
    expect(page.locator('#progressDetails')).to_be_hidden()
    checks.append('Error is explicit and enables a deliberate retry; details open only on click')

    job=start('ocr')
    expect(page.locator('#createSubs')).to_have_attribute('aria-pressed','true')
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','false')
    advance(job,'cancelled')
    expect(page.locator('#recognitionTitle')).to_contain_text('Đã hủy')
    expect(page.locator('#createSubs')).to_be_enabled()
    job=start('ocr')
    advance(job,'done',1)
    expect(page.locator('#recognitionRecovery')).to_be_visible()
    expect(page.locator('#recognitionHint')).to_contain_text('chưa tìm thấy câu phụ đề')
    page.locator('#trySpeechRecognition').click()
    expect(page.locator('#extractMethod')).to_be_visible()
    expect(page.locator('#extractMethod')).to_have_value('asr')
    expect(page.locator('#modalActions button.primary')).to_have_text('Nghe lời thoại và tạo phụ đề')
    page.get_by_role('button',name='Hủy',exact=True).click()
    checks.append('A completed OCR scan with no cues offers direct speech-recognition and manual-region recovery actions')
    job=start('asr')
    advance(job,'done',1)
    expect(page.locator('#recognitionTitle')).to_contain_text('Đã xử lý xong')
    expect(page.locator('#recognitionHint')).to_contain_text('Bảng hiện tại chưa có câu')
    expect(page.locator('#listenSubs')).to_be_enabled()
    checks.append('Switch to OCR, cancel, retry ASR and finish empty each show the correct method/outcome')

    for width in (1920,1280,1024,720,400):
        page.set_viewport_size({'width':width,'height':900})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        page.screenshot(path=str(OUT/f'layout-{width}.png'),full_page=True)

    current=page.request.get(project_url,headers=HEADERS).json()
    replaced=page.request.post(project_url+'/replace-source',headers=HEADERS,data={
        'expected_revision':current['updated'],'path':current['media']['path'],'mode':'reset'})
    assert replaced.ok
    page.reload();page.wait_for_load_state('networkidle')
    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#recognitionStatus')).to_be_hidden()
    expect(page.locator('#listenSubs')).to_have_attribute('aria-pressed','false')
    expect(page.locator('#createSubs')).to_have_attribute('aria-pressed','false')
    checks.append('Replacing video clears old-source method; compact layout fits 1920/1280/1024/720/400')
    assert not errors, errors
    browser.close()
(OUT/'browser-results.json').write_text(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False),flush=True)
