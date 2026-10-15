"""Exercise project context menu / cleanup with real API, isolated fixture data."""
import atexit
import json
from pathlib import Path
import httpx
from playwright.sync_api import sync_playwright

BASE='http://127.0.0.1:19864'
HEADERS={'x-studio-token':'npk-studio-test'}
OUT=Path(__file__).parent/'storage-results'
OUT.mkdir(exist_ok=True)
checks=[]
errors=[]

def stop():
    try: httpx.post(BASE+'/api/test/stop',headers=HEADERS,json={},timeout=3)
    except httpx.HTTPError: pass
atexit.register(stop)

with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1440,'height':900})
    page.on('pageerror',lambda e: errors.append(str(e)))
    native_dialogs=[]
    page.on('dialog',lambda dialog:(native_dialogs.append(dialog.message),dialog.dismiss()))
    request=page.request
    fixture=request.post(BASE+'/api/test/storage-fixture',headers=HEADERS,data={}).json()
    page.goto(BASE+'/studio#token=npk-studio-test')
    page.wait_for_load_state('networkidle')
    page.locator('[data-area="projects"]').click()
    card=page.locator(f'[data-project-card="{fixture["id"]}"]')
    card.click(button='right')
    page.get_by_role('menu',name='Thao tác dự án').wait_for()
    assert page.get_by_role('menuitem').count()==7
    page.screenshot(path=str(OUT/'01-context-menu.png'),full_page=True)
    page.keyboard.press('Escape')
    assert page.locator('#projectContextMenu').count()==0
    assert card.locator('.project-more').evaluate('(e)=>e===document.activeElement')
    card.locator('.project-more').focus()
    page.keyboard.press('Shift+F10')
    page.keyboard.press('End')
    assert page.evaluate('document.activeElement.textContent')=='Xóa dự án'
    page.keyboard.press('Escape')
    checks.append('Right-click, ellipsis, Shift+F10, arrow navigation and Escape focus work')
    card.locator('.project-more').click()
    page.get_by_role('menuitem',name='Đổi tên',exact=True).click()
    new_name='Video thử — quản lý dung lượng'
    page.locator('#projectNewName').fill(new_name)
    page.get_by_role('button',name='Lưu tên',exact=True).click()
    card.get_by_text(new_name,exact=True).wait_for()
    assert request.get(BASE+'/api/studio/projects/'+fixture['id'],headers=HEADERS).json()['name']==new_name
    checks.append('Rename via menu persists in API without switching the current project')
    card.locator('.project-more').click()
    page.get_by_role('menuitem',name='Dung lượng / dọn lượt xử lý cũ',exact=True).click()
    page.get_by_role('dialog').wait_for()
    assert '1 mục có thể' in page.get_by_role('dialog').inner_text()
    assert 'Giữ lại 2 lượt' in page.get_by_role('dialog').inner_text()
    page.screenshot(path=str(OUT/'02-cleanup-confirmation.png'),full_page=True)
    page.get_by_role('button',name='Hủy',exact=True).click()
    plan=request.get(BASE+'/api/studio/storage/plan?action=cleanup&project_id='+fixture['id'],headers=HEADERS).json()
    assert len(plan['targets'])==1
    checks.append('Cleanup preview protects active artifact/final and cancel does not remove files')
    card.locator('.project-more').click()
    page.get_by_role('menuitem',name='Dung lượng / dọn lượt xử lý cũ',exact=True).click()
    page.get_by_role('button',name='Chuyển vào thùng rác',exact=True).click()
    page.locator('#trashEntries [data-trash-entry]').wait_for()
    first=page.locator('[data-trash-entry]').first
    first.locator('[data-restore-trash]').click()
    page.get_by_role('dialog').get_by_role('button',name='Khôi phục',exact=True).click()
    page.wait_for_function('document.querySelectorAll("[data-trash-entry]").length===0')
    restored=request.get(BASE+'/api/studio/storage/plan?action=cleanup&project_id='+fixture['id'],headers=HEADERS).json()
    assert len(restored['targets'])==1
    checks.append('Cleanup and restore roundtrip through UI and real filesystem journal')
    page.locator('[data-area="projects"]').click()
    card.locator('[data-open-project]').click()
    page.get_by_role('textbox',name='Bản dịch câu 1',exact=True).wait_for()
    page.locator('[data-area="projects"]').click()
    card.locator('.project-more').click()
    # Any HTML or native confirmation here is a regression, even if transient.
    page.evaluate('''() => {
      window.storageDialogs = [];
      const original = HTMLDialogElement.prototype.showModal;
      HTMLDialogElement.prototype.showModal = function(...args) {
        window.storageDialogs.push(this.textContent);
        return original.apply(this,args);
      };
    }''')
    with page.expect_response(lambda response: response.url.endswith('/api/studio/storage/execute') and response.request.method=='POST') as deleted:
        page.get_by_role('menuitem',name='Xóa dự án',exact=True).click()
    assert deleted.value.ok
    delete_body=deleted.value.request.post_data_json
    assert 'name' not in delete_body and 'confirmed' not in delete_body
    page.locator('#trashEntries [data-trash-entry]').wait_for()
    assert page.get_by_role('dialog').count()==0
    assert page.locator('#deleteProjectName').count()==0
    assert request.get(BASE+'/api/studio/projects/'+fixture['id'],headers=HEADERS).status==404
    assert page.locator('#projectsTop').inner_text()=='Chọn dự án'
    assert page.locator('#video').get_attribute('src') is None
    page.screenshot(path=str(OUT/'03-storage-trash.png'),full_page=True)
    entry=page.locator('[data-trash-entry]').first
    purge_requests=[]
    page.on('request',lambda request: purge_requests.append(request.post_data_json)
            if request.method=='POST' and request.url.endswith('/purge') else None)
    # Two synchronous clicks emulate rapid repeated input while the request runs.
    with page.expect_response(lambda response: response.url.endswith('/purge') and response.request.method=='POST') as purged:
        entry.locator('[data-purge-trash]').evaluate('(button)=>{button.click();button.click()}')
    assert purged.value.ok
    page.wait_for_function('document.querySelectorAll("[data-trash-entry]").length===0')
    assert len(purge_requests)==1
    assert 'name' not in purge_requests[0] and 'confirmed' not in purge_requests[0]
    assert page.evaluate('window.storageDialogs')==[]
    assert native_dialogs==[]
    assert page.locator('#purgeTrashName').count()==0
    # The final video is deliberately retained, even after permanent project removal.
    result=request.get(BASE+'/media/job/'+fixture['jobs'][2]+'/final.mp4',headers=HEADERS)
    assert result.ok and result.body()==b'generated-test-data' * 128
    final_job=request.get(BASE+'/api/studio/jobs/'+fixture['jobs'][2],headers=HEADERS).json()
    assert not final_job.get('storage_removed')
    checks.append('Single-click project deletion and permanent purge have no name prompt or dialog; repeated clicks send one purge; finals stay protected')
    for width in (1920,1280,1024,720,400):
        page.set_viewport_size({'width':width,'height':900})
        page.locator('[data-area="projects"]').click()
        page.locator('.project-more').first.click()
        menu=page.locator('#projectContextMenu').bounding_box()
        assert menu['x']>=0 and menu['x']+menu['width']<=width+1
        page.keyboard.press('Escape')
        page.locator('#manageStorage').click()
        page.locator('#trashEntries').wait_for()
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
        page.screenshot(path=str(OUT/f'layout-{width}.png'),full_page=True)
    checks.append('Storage layout and context menu stay within viewport at 1920/1280/1024/720/400')
    assert not errors,errors
    report={'checks':checks,'errors':errors}
    (OUT/'browser-results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False),flush=True)
    browser.close()
