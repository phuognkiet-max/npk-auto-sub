"""Exercise held-pointer drawing against the isolated studio API (no workers)."""
import atexit
import json
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:19864'
HEADERS = {'x-studio-token': 'npk-studio-test'}
OUT = Path(__file__).parent / 'region-drawing-ui-results'
OUT.mkdir(exist_ok=True)
checks, errors, writes, jobs = [], [], [], []


def stop():
    try:
        httpx.post(BASE + '/api/test/stop', headers=HEADERS, json={}, timeout=3)
    except httpx.HTTPError:
        pass


atexit.register(stop)
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda r: writes.append(r.post_data_json)
            if r.method == 'PUT' and '/api/projects/' in r.url else None)
    page.on('request', lambda r: jobs.append(r.post_data_json)
            if r.method == 'POST' and r.url.endswith('/api/jobs') else None)
    page.goto(BASE + '/studio#token=npk-studio-test')
    page.wait_for_load_state('networkidle')
    expect(page.locator('#projectName')).to_contain_text('Dự án kiểm thử')
    page.screenshot(path=str(OUT / '00-before.png'), full_page=True)
    assert 'Chữ gốc' in page.locator('#nav').inner_text()
    page.locator('[data-area="clean"]').click()
    expect(page.locator('#drawRegion')).to_be_visible()
    expect(page.locator('#drawRegion')).to_contain_text('Thêm vùng')
    identifier = page.evaluate('localStorage.getItem("npk-current")')
    project_url = BASE + '/api/studio/projects/' + identifier
    overlay = page.locator('#regions')
    draft = page.locator('#regionDraft')

    def current():
        response = page.request.get(project_url, headers=HEADERS)
        assert response.ok
        return response.json()

    def activate_clean():
        if 'draw' not in (overlay.get_attribute('class') or '').split():
            page.locator('#drawRegion').click()

    def position(x, y):
        rect = overlay.bounding_box()
        assert rect and rect['width'] > 0 and rect['height'] > 0
        return rect['x'] + x * rect['width'], rect['y'] + y * rect['height']

    def move(x, y):
        page.mouse.move(*position(x, y), steps=4)

    def begin(x, y):
        move(x, y)
        page.mouse.down()
        expect(draft).to_be_attached()

    def assert_bounds(x, y, w, h):
        box, frame = draft.bounding_box(), overlay.bounding_box()
        assert box and frame
        for actual, expected in (
            (box['x'] - frame['x'], x * frame['width']),
            (box['y'] - frame['y'], y * frame['height']),
            (box['width'], w * frame['width']),
            (box['height'], h * frame['height']),
        ):
            assert abs(actual - expected) < 2, (box, frame, (x, y, w, h))

    def release_and_save():
        with page.expect_response(lambda r: r.request.method == 'PUT'
                                  and r.url.endswith('/api/projects/' + identifier)) as saved:
            page.mouse.up()
        assert saved.value.ok, saved.value.text()
        expect(draft).to_have_count(0)
        expect(page.locator('#saved')).to_have_text('Đã lưu')

    original = current()
    activate_clean()
    begin(.2, .2)
    move(.45, .4)
    expect(draft).to_be_visible()
    assert_bounds(.2, .2, .25, .2)
    move(.7, .6)
    assert_bounds(.2, .2, .5, .4)
    assert draft.locator('span').inner_text() == '320 × 144'
    # Redraw happens during playback as well as resize; keep the live draft.
    page.locator('#video').evaluate('(v) => {v.currentTime = 1; v.dispatchEvent(new Event("timeupdate"));}')
    assert_bounds(.2, .2, .5, .4)
    # Deliberately exceed the 650 ms autosave window while the mouse stays held.
    page.wait_for_timeout(850)
    assert current() == original
    assert not writes and not jobs
    assert page.evaluate('P.regions.length') == len(original['regions'])
    page.screenshot(path=str(OUT / '01-held-live-draft.png'), full_page=True)
    release_and_save()
    after = current()
    assert len(writes) == 1
    assert len(after['regions']) == len(original['regions']) + 1
    assert after['extraction_regions'] == original['extraction_regions']
    r = after['regions'][-1]
    for key, value in {'x': 128, 'y': 72, 'w': 320, 'h': 144}.items():
        assert abs(r[key] - value) < 2, r
    assert r['start'] == 0 and r['end'] == original['media']['duration']
    checks.append('Live draft and dimensions update while held, survive timeupdate, and save once only on release')

    activate_clean()
    begin(.8, .8)
    move(.4, .5)
    assert_bounds(.4, .5, .4, .3)
    move(-.08, -.08)
    assert_bounds(0, 0, .8, .8)
    release_and_save()
    r = current()['regions'][-1]
    assert r['x'] == 0 and r['y'] == 0
    checks.append('Reverse drag and captured pointer outside image clamp to image bounds')

    baseline, count = current(), len(writes)
    activate_clean()
    begin(.3, .3)
    move(.6, .5)
    page.keyboard.press('Escape')
    expect(draft).to_have_count(0)
    page.mouse.up()
    assert 'draw' not in (overlay.get_attribute('class') or '').split()
    for interrupt in ('pointercancel', 'lostpointercapture', 'blur', 'navigation'):
        activate_clean()
        begin(.2, .2)
        move(.5, .5)
        if interrupt == 'pointercancel':
            overlay.evaluate('(el) => el.dispatchEvent(new PointerEvent("pointercancel", {pointerId: drag.pointerId}))')
        elif interrupt == 'lostpointercapture':
            overlay.evaluate('(el) => el.releasePointerCapture(drag.pointerId)')
            move(.55, .55)
        elif interrupt == 'blur':
            page.evaluate('window.dispatchEvent(new Event("blur"))')
        else:
            # Keyboard navigation while the mouse is still down must cancel.
            page.locator('[data-area="subtitles"]').press('Enter')
        expect(draft).to_have_count(0)
        page.mouse.up()
        if interrupt == 'navigation':
            page.locator('[data-area="clean"]').click()
    page.wait_for_timeout(850)
    assert current() == baseline and len(writes) == count
    checks.append('Esc, pointercancel, lost capture, focus loss and navigation discard the draft without saving')

    activate_clean()
    move(.35, .35)
    page.mouse.click(*position(.35, .35), button='right')
    expect(draft).to_have_count(0)
    begin(.35, .35)
    page.mouse.move(*position(.351, .351))
    page.mouse.up()
    expect(draft).to_have_count(0)
    page.wait_for_timeout(850)
    assert current() == baseline and len(writes) == count
    checks.append('Right click and a tiny click do not create or save a region')

    begin(.2, .25)
    move(.5, .6)
    page.set_viewport_size({'width': 1280, 'height': 800})
    # ResizeObserver updates #picture asynchronously.
    expect(page.locator('#stage')).to_be_visible()
    page.wait_for_function('Math.abs(document.querySelector("#picture").clientWidth - '
                           'Math.min(document.querySelector("#stage").clientWidth - 2, '
                           '(document.querySelector("#stage").clientHeight - 2) * P.media.width / P.media.height)) < 2')
    assert_bounds(.2, .25, .3, .35)
    move(.65, .65)
    assert_bounds(.2, .25, .45, .4)
    release_and_save()
    r = current()['regions'][-1]
    assert abs(r['x'] - 128) < 2 and abs(r['w'] - 288) < 2, r
    checks.append('Resizing during drag preserves normalized selection and correct source-pixel coordinates')

    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#createSubs')).to_be_visible()
    page.locator('#createSubs').click()
    expect(page.get_by_role('button', name='Vẽ vùng trước', exact=True)).to_be_visible()
    page.get_by_role('button', name='Vẽ vùng trước', exact=True).click()
    baseline = current()
    begin(.1, .65)
    move(.85, .85)
    assert_bounds(.1, .65, .75, .2)
    assert current() == baseline
    page.screenshot(path=str(OUT / '02-ocr-held-draft.png'), full_page=True)
    release_and_save()
    final = current()
    assert final['regions'] == baseline['regions']
    assert len(final['extraction_regions']) == len(baseline['extraction_regions']) + 1
    checks.append('OCR region uses the same live drawing without changing cleanup regions or starting a job')

    page.reload()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#projectName')).to_contain_text('Dự án kiểm thử')
    expect(draft).to_have_count(0)
    assert page.evaluate('P.regions') == final['regions']
    assert page.evaluate('P.extraction_regions') == final['extraction_regions']
    assert not jobs and not errors, (jobs, errors)
    checks.append('Reload retains only committed regions; no worker/API-provider job and no JavaScript errors')
    browser.close()

(OUT / 'browser-results.json').write_text(
    json.dumps({'checks': checks, 'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'checks': checks, 'errors': errors}, ensure_ascii=False), flush=True)
