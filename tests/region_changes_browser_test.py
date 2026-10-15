"""Region edits must update the paused viewer without a seek/resize/redraw trigger."""
import atexit
import json
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:19864'
HEADERS = {'x-studio-token': 'npk-studio-test'}
OUT = Path(__file__).parent / 'region-changes-ui-results'
OUT.mkdir(exist_ok=True)
checks, errors, jobs = [], [], []


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
    page.on('request', lambda r: jobs.append(r.post_data_json)
            if r.method == 'POST' and r.url.endswith('/api/jobs') else None)
    page.goto(BASE + '/studio#token=npk-studio-test')
    page.wait_for_load_state('networkidle')
    expect(page.locator('#projectName')).to_contain_text('Dự án kiểm thử')
    assert 'Chữ gốc' in page.locator('#nav').inner_text()
    identifier = page.evaluate('localStorage.getItem("npk-current")')
    project_url = BASE + '/api/studio/projects/' + identifier

    def current():
        response = page.request.get(project_url, headers=HEADERS)
        assert response.ok
        return response.json()

    original = current()
    seeded = [
        dict(x=40, y=100, w=100, h=50, start=0, end=8),
        dict(x=260, y=200, w=120, h=60, start=0, end=8),
    ]
    result = page.request.put(BASE + '/api/projects/' + identifier, headers=HEADERS,
                              data={'expected_revision': original['updated'], 'regions': seeded})
    assert result.ok, result.text()
    page.reload()
    page.wait_for_load_state('networkidle')
    page.locator('[data-area="clean"]').click()
    expect(page.locator('#regions .region')).to_have_count(2)
    page.wait_for_function('document.querySelector("#video").readyState >= 2')
    assert page.locator('#video').evaluate('(v) => v.paused && v.currentTime === 0')
    page.screenshot(path=str(OUT / '01-before-delete.png'), full_page=True)

    def click_snapshot(button):
        # Inspect in the SAME event turn. No timeupdate, resize, polling, save or
        # delayed assertion can accidentally refresh the overlay and mask a bug.
        return button.evaluate('''b => {
            b.click();
            return {
                boxes: [...document.querySelectorAll('#regions .region:not(.region-draft)')]
                    .map(el => ({label: el.textContent, left: el.style.left, width: el.style.width})),
                clean: P.regions, reading: readingRegions(),
                list: document.querySelectorAll('#regionList [data-select-region]').length,
                empty: document.querySelector('#regionList')?.textContent.includes('Chưa có vùng xử lý'),
                paused: video.paused, time: video.currentTime,
            };
        }''')

    def saved():
        expect(page.locator('#saved')).to_have_text('Đã lưu')

    page.locator('#regionList [data-select-region="0"]').click()
    delete_region = page.locator('#panel .clean-region-actions [data-region-command="delete"]')
    state = click_snapshot(delete_region)
    assert len(state['boxes']) == 1, f'Overlay was not refreshed on delete: {state}'
    assert state['boxes'][0]['label'] == '1' and float(state['boxes'][0]['left'].strip('%')) == 40.625
    assert state['clean'] == seeded[1:] and state['list'] == 1
    assert state['paused'] and state['time'] == 0
    saved()
    assert current()['regions'] == seeded[1:]
    checks.append('Delete removes the correct box immediately and renumbers remaining regions while video stays paused')

    state = click_snapshot(page.locator('#undo'))
    assert len(state['boxes']) == 2 and state['clean'] == seeded and state['list'] == 2
    state = click_snapshot(page.locator('#redo'))
    assert len(state['boxes']) == 1 and state['clean'] == seeded[1:] and state['list'] == 1
    saved()
    checks.append('Undo and redo synchronize the region list and paused overlay in the same event turn')

    page.locator('#regionList [data-select-region="0"]').click()
    page.locator('#regionCoordinates summary').click()
    width = page.locator('.region-input[data-key="w"]')
    width.fill('150')
    width.press('Tab')
    assert page.locator('#regions .region').evaluate('el => parseFloat(el.style.width)') == 150 / 640 * 100
    start = page.locator('.region-input[data-key="start"]')
    start.fill('2')
    start.press('Tab')
    expect(page.locator('#regions .region')).to_have_count(0)
    start.fill('0')
    start.press('Tab')
    expect(page.locator('#regions .region')).to_have_count(1)
    assert page.locator('#video').evaluate('(v) => v.paused && v.currentTime === 0')
    saved()
    checks.append('Geometry and time-range edits refresh the paused overlay without playback or redrawing a region')

    state = click_snapshot(delete_region)
    assert not state['boxes'] and not state['clean'] and state['list'] == 0 and state['empty']
    saved()
    page.screenshot(path=str(OUT / '02-last-region-deleted.png'), full_page=True)
    page.reload()
    page.wait_for_load_state('networkidle')
    page.locator('[data-area="clean"]').click()
    expect(page.locator('#regions .region')).to_have_count(0)
    expect(page.locator('#regionList')).to_contain_text('Chưa có vùng xử lý')
    assert current()['regions'] == []
    checks.append('Deleting the last region immediately clears all boxes; empty state and deletion persist after reload')

    page.locator('[data-area="subtitles"]').click()
    expect(page.locator('#regions .region')).to_have_count(1)
    page.locator('#createSubs').click()
    page.get_by_text('Vùng đọc phụ đề', exact=True).click()
    state = click_snapshot(page.locator('[data-remove-reading="0"]'))
    assert not state['boxes'] and not state['reading'] and not state['clean']
    page.locator('#modalClose').click()
    state = click_snapshot(page.locator('#undo'))
    assert len(state['boxes']) == 1 and state['reading'] == original['extraction_regions']
    assert state['clean'] == []
    state = click_snapshot(page.locator('#redo'))
    assert not state['boxes'] and not state['reading'] and not state['clean']
    saved()
    final = current()
    assert final['extraction_regions'] == [] and final['regions'] == []
    assert final['segments'] == original['segments'] and final['media'] == original['media']
    assert final['params'] == original['params']
    assert not jobs and not errors, (jobs, errors)
    checks.append('Reading-region deletion/undo/redo remain separate; no subtitle, video, settings or processing-job changes')
    browser.close()

(OUT / 'browser-results.json').write_text(
    json.dumps({'checks': checks, 'errors': errors}, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps({'checks': checks, 'errors': errors}, ensure_ascii=False), flush=True)
