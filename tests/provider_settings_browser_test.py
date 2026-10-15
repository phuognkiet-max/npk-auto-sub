"""Focused provider/model settings UI contract using the isolated test double."""
import json
import time
import httpx
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:19864'
TOKEN = 'npk-studio-test'
headers = {'x-studio-token': TOKEN}
checks = []

try:
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel='msedge', headless=True)
        page = browser.new_page(viewport={'width': 1440, 'height': 900})
        requests = []
        page.on('request', lambda request: requests.append(request)
                if any(request.url.endswith(f'/api/keys/{provider}/check')
                       for provider in ('deepseek', 'router9')) else None)
        page.goto(BASE + '/studio#token=' + TOKEN)
        page.wait_for_load_state('networkidle')
        page.locator('[data-area="settings"]').click()
        page.locator('#apiKey').wait_for()

        page.locator('#apiKey').fill('sk-fake-local-test-key-1234')
        assert page.locator('#keyProvider').input_value() == 'deepseek'
        assert page.locator('#connectionModel').input_value() == 'deepseek-flash'
        page.locator('#connectionModel').fill('deepseek-chat')
        page.locator('#saveKey').click()
        page.locator('.key-row').wait_for()
        assert page.locator('#apiKey').input_value() == ''
        assert 'sk-fake-local-test-key' not in page.locator('#keysList').inner_text()

        page.locator('#checkKey').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('tự cập nhật model')")
        assert page.locator('#connectionModel').input_value() == 'deepseek-flash'
        checks.append('Pasted sk key selects DeepSeek and catalog replaces retired model ID')

        requests.clear()
        page.locator('#connectionModel').fill('gemini-3.7-flash')
        page.locator('#testGeneration').click()
        assert requests == []
        assert 'không khớp nhà cung cấp' in page.locator('#connectionStatus').inner_text()
        checks.append('Provider/model mismatch is blocked locally before any generation request')

        page.locator('#connectionModel').fill('deepseek-flash')
        page.locator('#testGeneration').click()
        page.get_by_role('button', name='Gọi thử', exact=True).click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Kết nối kiểm thử')")
        assert len(requests) == 1
        assert 'Ghi nhận phép thử: 1 request (HTTP 200)' in page.locator('#connectionStatus').inner_text()
        assert 'API báo 2 token' in page.locator('#connectionStatus').inner_text()
        sent = json.loads(requests[0].post_data or '{}')
        assert sent.get('mode') == 'generation' and sent.get('model') == 'deepseek-flash'
        assert 'key' not in sent and 'secret' not in sent
        assert 'sk-fake-local-test-key' not in page.locator('body').inner_text()
        checks.append('Confirmed short generation test routes through the mocked DeepSeek provider')

        page.locator('#useModel').click()
        project_id = page.evaluate('localStorage.getItem("npk-current")')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            project = page.request.get(f'{BASE}/api/studio/projects/{project_id}', headers=headers).json()
            if (project['params']['provider'], project['params']['translation_model']) == ('deepseek', 'deepseek-flash'):
                break
            page.wait_for_timeout(100)
        assert (project['params']['provider'], project['params']['translation_model']) == ('deepseek', 'deepseek-flash')
        checks.append('Applying the model stores the matching provider and current model in the project')

        preserved_segments = project['segments']
        preserved_voice = project['params']['voice_id']
        page.locator('#keyProvider').select_option('router9')
        page.locator('#connectionBaseUrl').wait_for(state='visible')
        assert page.locator('#connectionModel').input_value() == ''
        assert page.locator('#connectionBaseUrl').input_value() == 'http://127.0.0.1:20128/v1'
        page.locator('#connectionBaseUrl').fill('http://127.0.0.1:20129/dashboard')
        page.locator('#connectionBaseUrl').blur()
        assert page.locator('#connectionBaseUrl').input_value() == 'http://127.0.0.1:20129/v1'
        page.locator('#apiKey').fill('sk-fake-router-token-5678')
        assert page.locator('#keyProvider').input_value() == 'router9'
        page.locator('#saveKey').click()
        page.locator('.key-row').wait_for()
        assert page.locator('#apiKey').input_value() == ''
        checks.append('Router token remains scoped to 9Router and dashboard URL becomes API base')

        requests.clear()
        page.locator('#checkKey').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Có 3 model')")
        assert len(requests) == 1
        sent = json.loads(requests[0].post_data or '{}')
        assert sent['mode'] == 'catalog' and sent['base_url'] == 'http://127.0.0.1:20129/v1'
        assert not sent.get('model')
        assert page.locator('#connectionModel').input_value() == ''
        assert page.locator('#modelOptions option').evaluate_all('(rows) => rows.map(row=>row.value)') == ['test-router/translator', 'test-router/vision-reader', 'oc/muse-spark-1.3-contributor-free']
        checks.append('Router catalog accepts namespaced IDs and never chooses an arbitrary model')

        page.locator('#connectionBaseUrl').fill('')
        page.locator('#connectionBaseUrl').blur()
        requests.clear()
        page.locator('#checkKey').click()
        assert requests == []
        assert 'Nhập địa chỉ API 9Router' in page.locator('#connectionStatus').inner_text()
        page.locator('#connectionBaseUrl').fill('http://127.0.0.1:20129/v1')
        page.locator('#connectionBaseUrl').blur()
        checks.append('An empty router endpoint never silently sends to the previous or default URL')

        page.locator('#connectionModel').fill('not-in-catalog/model')
        requests.clear()
        page.locator('#testGeneration').click()
        assert requests == []
        assert 'Chưa gửi request thử sinh' in page.locator('#connectionStatus').inner_text()
        checks.append('Router generation requires a model from the current endpoint catalog')

        page.locator('#connectionModel').fill('test-router/translator')
        page.locator('#useModel').click()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            project = page.request.get(f'{BASE}/api/studio/projects/{project_id}', headers=headers).json()
            if project['params'].get('provider') == 'router9':
                break
            page.wait_for_timeout(100)
        assert (project['params']['provider'], project['params']['translation_model'], project['params']['router9_base_url']) == ('router9', 'test-router/translator', 'http://127.0.0.1:20129/v1')
        assert project['segments'] == preserved_segments and project['params']['voice_id'] == preserved_voice
        checks.append('Apply stores router endpoint and chosen model while preserving captions and voice')

        muse = 'oc/muse-spark-1.3-contributor-free'
        page.locator('#connectionModel').fill(muse)
        assert 'chưa lưu' in page.locator('#connectionDraft').inner_text()
        page.locator('#apiKey').fill('sk-fake-router-second-token-5678')
        page.locator('#saveKey').click()
        page.wait_for_function("document.querySelector('#apiKey').value === ''")
        project = page.request.get(f'{BASE}/api/studio/projects/{project_id}', headers=headers).json()
        assert project['params']['translation_model'] == 'test-router/translator'
        assert 'test-router/translator' in page.locator('#savedConnection').inner_text()
        checks.append('Saving a token does not persist the model draft or claim it has been applied')
        page.locator('#checkKey').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Có 3 model')")
        page.locator('#useModel').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Đã lưu model trên máy chủ')")
        project = page.request.get(f'{BASE}/api/studio/projects/{project_id}', headers=headers).json()
        assert project['params']['translation_model'] == muse
        assert muse in page.locator('#savedConnection').inner_text()
        page.reload()
        page.wait_for_load_state('networkidle')
        page.locator('[data-area="settings"]').click()
        page.locator('#connectionModel').wait_for()
        assert page.locator('#connectionModel').input_value() == muse
        assert muse in page.locator('#savedConnection').inner_text()
        checks.append('Explicit model save persists exact Muse route and restores it after reload before catalog fetch')

        def catalog_without_saved_model(route):
            response = route.fetch()
            payload = response.json()
            payload['models'] = ['test-router/translator']
            route.fulfill(response=response, json=payload)

        page.route('**/api/keys/router9/check', catalog_without_saved_model)
        page.locator('#checkKey').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Model đang hiển thị không có trong danh mục')")
        assert page.locator('#connectionModel').input_value() == muse
        assert muse in page.locator('#savedConnection').inner_text()
        page.unroute('**/api/keys/router9/check', catalog_without_saved_model)
        checks.append('Catalog omission does not erase a saved model or substitute an arbitrary fallback')
        page.locator('#checkKey').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Có 3 model')")

        page.evaluate("""() => {
            jobs = [{id:'immutable-luna-job',project_id:P.id,state:'running',kind:'pipeline',
                configuration:{provider:'router9',translation_model:'cx/gpt-6-luna',router9_base_url:'http://127.0.0.1:20129/v1'}}];
            refreshConnectionReceipt();
        }""")
        assert 'cx/gpt-6-luna' in page.locator('#runningConnection').inner_text()
        page.locator('#connectionModel').fill('test-router/vision-reader')
        page.locator('#useModel').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Đã lưu model trên máy chủ: router9 · test-router/vision-reader')")
        project = page.request.get(f'{BASE}/api/studio/projects/{project_id}', headers=headers).json()
        assert project['params']['translation_model'] == 'test-router/vision-reader'
        assert 'test-router/vision-reader' in page.locator('#savedConnection').inner_text()
        page.evaluate('jobs = []')
        checks.append('Explicit model save during a running snapshot persists via the server without a second prompt or blocked change()')

        # Return to Muse before testing the failed save receipt.
        page.locator('#connectionModel').fill(muse)
        page.locator('#useModel').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Đã lưu model trên máy chủ: router9 · oc/muse-spark')")

        def reject_model_save(route):
            route.fulfill(status=409, content_type='application/json', json={'detail':'Concurrent revision test'})

        page.route('**/api/projects/' + project_id, reject_model_save)
        page.locator('#connectionModel').fill('test-router/translator')
        page.locator('#useModel').click()
        page.wait_for_function("document.querySelector('#connectionDraft').textContent.includes('Chưa xác nhận lưu model')")
        assert muse in page.locator('#savedConnection').inner_text()
        project = page.request.get(f'{BASE}/api/studio/projects/{project_id}', headers=headers).json()
        assert project['params']['translation_model'] == muse
        page.unroute('**/api/projects/' + project_id, reject_model_save)
        checks.append('Revision conflict never reports a draft model as saved or overwrites the persisted receipt')
        page.locator('#useModel').click()
        page.wait_for_function("document.querySelector('#connectionStatus').textContent.includes('Đã lưu model trên máy chủ: router9 · test-router/translator')")

        page.locator('#connectionBaseUrl').fill('http://127.0.0.1:20130/v1')
        page.locator('#connectionBaseUrl').blur()
        requests.clear()
        page.locator('#testGeneration').click()
        assert requests == []
        assert 'Kiểm tra kết nối trước' in page.locator('#connectionStatus').inner_text()
        assert 'sk-fake-router-token' not in page.locator('body').inner_text()
        checks.append('Changing router endpoint invalidates the catalog before any generation test')

        requests.clear()
        page.locator('[data-area="setup"]').click()
        page.locator('#setupProvider').wait_for(state='attached')
        assert page.locator('#setupProvider').input_value() == 'router9'
        assert page.locator('#setupRouter9BaseUrl').input_value() == 'http://127.0.0.1:20129/v1'
        page.locator('#setupSubtitleAutomation').select_option('router9')
        page.wait_for_function("document.querySelector('#setupAutomationHint').textContent.includes('9Router hỗ trợ')")
        assert '9Router' in page.locator('#setupGeminiDisclosure').inner_text()
        assert page.locator('#setupModel').input_value() == 'test-router/translator'
        assert requests == []
        checks.append('Guided setup carries router endpoint, explicit model and 9Router subtitle automation without provider calls')
        page.locator('#setupProvider').select_option('gemini', force=True)
        page.wait_for_function("document.querySelector('#setupModel').value === ''")
        page.locator('#setupModel').evaluate("el => {el.value='gemini-test';el.dispatchEvent(new Event('change',{bubbles:true}));}")
        page.locator('#setupProvider').select_option('router9', force=True)
        page.wait_for_function("document.querySelector('#setupModel').value === ''")
        assert requests == []
        checks.append('Switching setup provider clears the previous provider model before any call')
        admission = dict(execution_allowed=False, blocked_code='audit_hold',
                         blocked_reason='Khóa kiểm toán kiểm thử: chưa gửi API.',
                         required_connections=[{'provider': 'router9'}])

        def blocked_preflight(route):
            response = route.fetch()
            payload = response.json()
            payload.update(ok=False, execution_admission=admission)
            payload.setdefault('errors', []).append(admission['blocked_reason'])
            route.fulfill(response=response, json=payload)

        def blocked_continuation(route):
            response = route.fetch()
            payload = response.json()
            payload['execution_admission'] = admission
            route.fulfill(response=response, json=payload)

        page.route('**/api/preflight', blocked_preflight)
        page.route('**/pipeline-continuation', blocked_continuation)
        page.locator('[data-area="settings"]').click()
        page.locator('[data-area="setup"]').click()
        page.locator('#setupAdmission').wait_for(state='visible')
        assert admission['blocked_reason'] in page.locator('#setupAdmission').inner_text()
        assert '9Router' in page.locator('#setupAdmission').inner_text()
        assert page.locator('#setupRun').is_disabled()
        assert requests == []
        page.locator('#setupAdmissionConnection').click()
        page.locator('#keyProvider').wait_for(state='visible')
        checks.append('Admission block is visible before Run, disables processing, and links to connections without provider requests')

        page.route('**/api/test/admission-race', lambda route: route.fulfill(
            status=409, content_type='application/json',
            body=json.dumps({'detail': admission['blocked_reason'], 'execution_admission': admission})))
        race = page.evaluate("""async () => {
          try {await api('/api/test/admission-race', {});} catch(error) {
            return {status:error.status, admission:error.execution_admission};
          }
        }""")
        assert race['status'] == 409 and race['admission'] == admission
        checks.append('A raced 409 preserves the structured admission reason for inline recovery')
        print(json.dumps({'checks': checks, 'external_provider_calls': 0, 'secrets_printed': False}, ensure_ascii=False))
        browser.close()
finally:
    try:
        httpx.post(BASE + '/api/test/stop', headers=headers, json={}, timeout=3)
    except httpx.HTTPError:
        pass
