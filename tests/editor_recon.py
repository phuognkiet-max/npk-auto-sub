from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path(__file__).parent/'editor-results'
out.mkdir(exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    try:
        page = browser.new_page(viewport=dict(width=1366, height=768))
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:18774/#token=npk-editor-test-token')
        page.wait_for_load_state('networkidle')
        page.wait_for_function('document.querySelector("#video").readyState >= 2')
        print(page.locator('body').inner_text())
        print('ERRORS', errors)
        print('LAYOUT', page.evaluate('({w:innerWidth,h:innerHeight,scrollW:document.body.scrollWidth,scrollH:document.body.scrollHeight, viewport:document.querySelector("#viewport").getBoundingClientRect().toJSON(),footer:document.querySelector("footer").getBoundingClientRect().toJSON()})'))
        page.screenshot(path=str(out/'editor-1366-recon.png'), full_page=True)
    finally:
        browser.close()
