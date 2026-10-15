"""Progress drawer regression: real UI, simulated jobs; no AI workers or provider calls."""
import atexit
import json
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:19864'
HEADERS = {'x-studio-token': 'npk-studio-test'}
OUT = Path(__file__).parent / 'progress-results'
OUT.mkdir(exist_ok=True)
checks, errors, tasks, detail_requests = [], [], [], []


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
    state = page.request.get(BASE + '/api/studio/state', headers=HEADERS).json()

    def serve_state(route):
        route.fulfill(json={**state, 'jobs': tasks})

    def enqueue(route):
        body = route.request.post_data_json
        task = dict(id=f'progress-test-{len(tasks)}', project_id=body['project_id'],
                    kind=body['kind'], state='queued', progress=0, message='Đang chờ xử lý.',
                    events=[], result={}, configuration={})
        tasks.insert(0, task)
        route.fulfill(json=task)

    def detail(route):
        identifier = route.request.url.rsplit('/', 1)[-1]
        detail_requests.append(identifier)
        route.fulfill(json=next(task for task in tasks if task['id'] == identifier))

    page.route('**/api/studio/state*', serve_state)
    page.route('**/api/preflight', lambda route: route.fulfill(json={'ok': True, 'errors': []}))
    page.route('**/api/jobs', enqueue)
    page.route('**/api/studio/jobs/*', detail)
    page.goto(BASE + '/studio#token=npk-studio-test')
    page.wait_for_load_state('networkidle')
    page.get_by_role('button', name='Nghe lời thoại', exact=True).wait_for()
    panel = page.locator('#progressDetails')
    toggle = page.locator('#progressToggle')
    expect(panel).to_be_hidden()
    # Capture even a transient automatic expansion, not just the final state.
    page.evaluate('''() => {
      window.progressOpens = 0;
      new MutationObserver(records => {
        for (const record of records) {
          if (record.oldValue !== null) window.progressOpens++;
        }
      }).observe(document.querySelector('#progressDetails'), {
        attributes: true, attributeFilter: ['hidden'], attributeOldValue: true
      });
    }''')

    def start_task():
        page.get_by_role('button', name='Nghe lời thoại', exact=True).click()
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')):
            page.get_by_role('button', name='Bắt đầu nhận dạng', exact=True).click()
        expect(page.locator('#progressText')).to_contain_text('Đang chờ')

    def advance(status, progress):
        tasks[0].update(state=status, progress=progress, message='Trạng thái thử: ' + status)
        page.wait_for_function('!pollBusy')
        page.evaluate('poll()')

    start_task()
    expect(panel).to_be_hidden()
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    assert not detail_requests, detail_requests
    advance('running', .37)
    expect(page.locator('#progressText')).to_contain_text('37%')
    expect(panel).to_be_hidden()
    assert page.evaluate('window.progressOpens') == 0
    checks.append('Starting and processing a task update the compact bar without opening or fetching details')
    page.screenshot(path=str(OUT / '01-compact-running.png'), full_page=True)

    toggle.click()
    expect(panel).to_be_visible()
    expect(toggle).to_have_attribute('aria-expanded', 'true')
    expect(page.locator('#jobDetails')).to_contain_text('Trạng thái thử: running')
    advance('done', 1)
    expect(page.locator('#jobDetails h3')).to_contain_text('Đã xử lý xong')
    expect(panel).to_be_visible()
    checks.append('Manual expansion works and visible details update through completion')

    page.locator('#closeProgress').click()
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    opens = page.evaluate('window.progressOpens')
    requests = len(detail_requests)
    for status in ('done', 'error', 'cancelled', 'review'):
        start_task()
        advance('running', .5)
        advance(status, 1 if status in ('done', 'review') else .5)
        expect(panel).to_be_hidden()
        expect(toggle).to_have_attribute('aria-expanded', 'false')
    assert page.evaluate('window.progressOpens') == opens
    assert len(detail_requests) == requests
    checks.append('Closing stays respected for later jobs, success, failure, cancellation and review')

    # Native button keyboard interaction is retained.
    toggle.focus()
    page.keyboard.press('Enter')
    expect(panel).to_be_visible()
    expect(page.locator('#jobDetails h3')).to_contain_text('Cần xem lại phụ đề')
    # Reduce the overlay to leave the task entry point visible on this viewport.
    page.set_viewport_size({'width': 1920, 'height': 1080})
    start_task()
    expect(panel).to_be_visible()
    expect(page.locator('#jobDetails h3')).to_contain_text('Đang chờ')
    advance('error', .2)
    expect(page.locator('#jobDetails h3')).to_contain_text('Chưa hoàn tất')
    toggle.click()
    expect(panel).to_be_hidden()
    checks.append('Keyboard/click toggles work; an explicitly opened panel stays open and follows the new job')

    tasks[0].update(state='running', progress=.62)
    page.reload()
    page.wait_for_load_state('networkidle')
    expect(page.locator('#progressText')).to_contain_text('62%')
    expect(panel).to_be_hidden()
    expect(toggle).to_have_attribute('aria-expanded', 'false')
    checks.append('Reload during active processing starts compact, without auto-expansion')
    assert not errors, errors
    browser.close()

(OUT / 'browser-results.json').write_text(
    json.dumps({'checks': checks, 'errors': errors, 'jobs': 'simulated'}, ensure_ascii=False, indent=2),
    encoding='utf-8',
)
print(json.dumps({'checks': checks, 'errors': errors}, ensure_ascii=False), flush=True)
