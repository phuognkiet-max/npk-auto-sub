"""UI regression against editor_server only; no user files, keys or paid services."""
import json
from pathlib import Path
import socket
import time
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:18774'
TOKEN = 'npk-editor-test-token'
out = Path(__file__).parent/'editor-results'
out.mkdir(exist_ok=True)
checks = []

with sync_playwright() as p:
    browser = p.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport=dict(width=1366, height=768))
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    request = p.request.new_context(base_url=BASE, extra_http_headers={'x-studio-token': TOKEN})

    def state():
        response = request.get('/api/state')
        assert response.ok
        return response.json()

    def saved():
        expect(page.locator('#saveStatus')).to_have_text('Đã lưu')
        return state()['projects'][0]

    def check(label):
        checks.append(label)
        print('PASS', label, flush=True)

    try:
        page.goto(BASE+'/#token='+TOKEN)
        page.wait_for_load_state('networkidle')
        page.wait_for_function('document.querySelector("#video").readyState >= 2')
        # Reconnaissance first: the rendered DOM identifies the real controls.
        assert 'NPK AUTO SUB' in page.locator('#topbar').inner_text()
        assert page.get_by_role('button', name='Phát', exact=True).count() == 1
        assert page.locator('#trackHeads .track-head').count() == 6
        page.locator('#play').click()
        page.wait_for_function('document.querySelector("#video").currentTime > .1')
        page.locator('#play').click()
        page.locator('#pixel100').click()
        expect(page.locator('#viewerZoom')).to_have_text('100%')
        page.locator('#fit').click()
        check('Video playback, fit/100%, six tracks')

        page.locator('[data-tool="sub"]').click()
        page.locator('#objectList button').first.click()
        page.locator('#properties').get_by_label('Bản dịch', exact=True).fill('Bản dịch đã sửa trong editor.')
        page.locator('#properties').get_by_label('Bản dịch', exact=True).press('Tab')
        page.locator('#save').click()
        project = saved()
        assert project['segments'][0]['translated'] == 'Bản dịch đã sửa trong editor.'
        page.locator('#duplicateObject').click()
        assert page.locator('#objectList button').count() == 3
        page.locator('#undo').click()
        assert page.locator('#objectList button').count() == 2
        page.locator('#redo').click()
        assert page.locator('#objectList button').count() == 3
        page.locator('#deleteObject').click()
        assert page.locator('#objectList button').count() == 2
        page.locator('#save').click()
        saved()
        check('Cue editing, save, duplicate, undo/redo, delete')

        # Seek into the first cue, then split it via the real toolbar.
        page.locator('#objectList button').first.click()
        page.locator('#scrub').fill('1.2')
        page.locator('#splitObject').click()
        page.locator('#save').click()
        project = saved()
        assert len(project['segments']) == 3
        assert abs(project['segments'][0]['end']-1.2) < .01
        assert abs(project['segments'][1]['start']-1.2) < .01
        page.locator('#undo').click()
        page.locator('#save').click()
        saved()
        check('Playhead split preserves adjacent cue boundaries')

        # Drag a cue endpoint in the canvas, not by mutating app state.
        page.locator('.timeline-body').evaluate('(e)=>e.scrollTop=90')
        canvas_box = page.locator('#timelineCanvas').bounding_box()
        scale = canvas_box['width']/8
        end_x = canvas_box['x'] + 2.7*scale - 2
        row_y = canvas_box['y'] + 24 + 2*48 + 23
        page.mouse.move(end_x, row_y)
        page.mouse.down()
        page.mouse.move(end_x-.3*scale, row_y, steps=5)
        page.mouse.up()
        page.locator('#save').click()
        project = saved()
        assert abs(project['segments'][0]['end']-2.4)<.09, project['segments'][0]
        page.locator('#undo').click()
        page.locator('#save').click()
        saved()
        page.locator('#timelineZoom').fill('4')
        assert page.locator('#timelineExtent').bounding_box()['width'] > canvas_box['width']*3.8
        page.locator('#timelineZoom').fill('1')
        page.locator('.timeline-body').evaluate('(e)=>e.scrollTop=0')
        check('Canvas endpoint dragging snaps to frames; timeline zoom expands time axis')

        page.locator('[data-tool="clean"]').click()
        box = page.locator('#overlay').bounding_box()
        page.mouse.move(box['x']+box['width']*.1, box['y']+box['height']*.5)
        page.mouse.down()
        page.mouse.move(box['x']+box['width']*.3, box['y']+box['height']*.7, steps=6)
        page.mouse.up()
        page.locator('#save').click()
        project = saved()
        assert len(project['regions']) == 2
        mask = project['regions'][-1]
        assert abs(mask['x']-64) <= 2 and abs(mask['w']-128) <= 2
        check('Mask drawing maps viewer pixels back to source coordinates')

        page.get_by_role('button', name='Khóa Vùng xóa', exact=True).click()
        expect(page.locator('#deleteObject')).to_be_disabled()
        expect(page.locator('#properties').get_by_label('X (px)', exact=True)).to_be_disabled()
        page.get_by_role('button', name='Khóa Vùng xóa', exact=True).click()
        page.get_by_role('button', name='Mute Âm gốc / BGM', exact=True).click()
        assert page.locator('#video').evaluate('(e)=>e.muted')
        page.get_by_role('button', name='Mute Âm gốc / BGM', exact=True).click()
        check('Track lock and audition mute')

        page.locator('#settings').click()
        page.locator('#settingsTabs [data-group="general"]').click()
        page.locator('#settingsScope').select_option('defaults')
        page.locator('#settingsFields [data-param="workload_mode"]').select_option('balanced')
        page.locator('#applySettings').click()
        expect(page.locator('#toast')).to_contain_text('Đã lưu cài đặt')
        assert state()['settings']['workload_mode'] == 'balanced'
        assert state()['projects'][0]['params']['workload_mode'] == 'quiet'
        page.locator('#settingsScope').select_option('project')
        page.locator('#settingsTabs [data-group="clean"]').click()
        page.locator('#settingsFields [data-param="dilate"]').fill('6')
        page.locator('#settingsFields [data-param="dilate"]').press('Tab')
        page.locator('#applySettings').click()
        saved()
        assert state()['projects'][0]['params']['dilate'] == 6
        assert state()['settings']['dilate'] == 4
        check('Project settings and future defaults remain independent')

        page.locator('#settingsTabs [data-group="keys"]').click()
        expect(page.locator('#keyStatus')).to_contain_text('ABCD')
        page.locator('#apiKey').fill('fake-editor-test-WXYZ')
        page.locator('#saveKey').click()
        expect(page.locator('#keyList')).to_contain_text('WXYZ')
        page.get_by_role('button',name='Dùng key này',exact=True).click()
        expect(page.locator('#keyStatus')).to_contain_text('WXYZ')
        expect(page.locator('#apiKey')).to_have_value('')
        assert 'fake-editor-test-WXYZ' not in json.dumps(state())
        page.locator('[data-close="settingsDialog"]').click()
        check('Fake credential save/readback masked, secret absent from state')

        page.reload()
        page.wait_for_load_state('networkidle')
        page.wait_for_function('document.querySelector("#video").readyState >= 2')
        assert 'Kiểm thử editor' in page.locator('#projectName').inner_text()
        check('Reload retains authenticated local session and saved project')

        # Small synthetic Telea job only; bounded and no AI models/network.
        project = state()['projects'][0]
        response = request.post('/api/jobs', data=dict(project_id=project['id'], kind='clip', time=0))
        assert response.ok, response.text()
        job_id = response.json()['id']
        seen = []
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            job = next(j for j in state()['jobs'] if j['id']==job_id)
            seen.append((job['state'], job['progress']))
            assert job['state']=='done' or job['progress']<1
            if job['state'] in ('done', 'error', 'cancelled'):
                break
            page.wait_for_timeout(250)
        assert job['state']=='done', job
        assert job['result']['verification']['files_checked']==1
        assert abs(job['result']['duration']-3)<.12
        video_result = request.get('/media/job/'+job_id+'/clean.mp4')
        assert video_result.ok and len(video_result.body())>1000
        expect(page.locator('#jobSummary')).to_contain_text('100%', timeout=5000)
        page.locator('#jobToggle').click()
        expect(page.locator('#jobEvents')).to_contain_text('metadata')
        check('Real 3s Telea worker -> verify -> done 100%, artifact downloadable')

        # Cancellation of a fresh synthetic job must remain cancelled.
        response = request.post('/api/jobs', data=dict(project_id=project['id'], kind='clip', time=0))
        cancel_id = response.json()['id']
        assert request.post('/api/jobs/'+cancel_id+'/cancel').ok
        page.wait_for_timeout(700)
        assert next(j for j in state()['jobs'] if j['id']==cancel_id)['state']=='cancelled'
        check('Cancelled job cannot be overwritten by a late event')

        # Exercise the existing export pipeline with a synthetic 8-second video.
        project = state()['projects'][0]
        response = request.put('/api/projects/'+project['id'], data=dict(params=dict(use_clean=False), expected_revision=project['updated']))
        assert response.ok
        response = request.post('/api/jobs', data=dict(project_id=project['id'], kind='render', time=0))
        assert response.ok
        render_id = response.json()['id']
        deadline = time.monotonic()+60
        while time.monotonic()<deadline:
            render_job = next(j for j in state()['jobs'] if j['id']==render_id)
            if render_job['state'] in ('done', 'error', 'cancelled'):
                break
            page.wait_for_timeout(250)
        assert render_job['state']=='done', render_job
        assert render_job['result']['verification']['files_checked']==3
        assert request.get('/media/job/'+render_id+'/final.mp4').ok
        # Select the new result in the actual viewer, which must play at unit gain.
        page.wait_for_function('(id)=>[...document.querySelector("#viewSource").options].some(o=>o.value===id)', arg=render_id)
        page.locator('#viewSource').select_option(render_id)
        page.wait_for_function('document.querySelector("#video").readyState >= 2')
        assert abs(page.locator('#video').evaluate('(e)=>e.duration')-8)<.12
        assert page.locator('#video').evaluate('(e)=>e.volume')==1
        check('8s H.264/AAC export with edited subtitles, verified and played in viewer')
        page.locator('#viewSource').select_option('source')

        for width,height in ((1920,1080),(1366,768),(1050,720),(1093,614)):
            page.set_viewport_size(dict(width=width,height=height))
            page.wait_for_timeout(180)
            for expanded in (True, False):
                hidden = page.locator('#jobDetails').is_hidden()
                if hidden == expanded:
                    page.locator('#jobToggle').click()
                box = page.locator('footer').bounding_box()
                assert box['y']>=0 and box['y']+box['height']<=height+1, (width,height,expanded,box)
                assert page.evaluate('document.body.scrollWidth <= innerWidth')
                assert page.evaluate('document.body.scrollHeight <= innerHeight')
                if not expanded:
                    viewer = page.locator('#viewer').bounding_box()
                    fit = page.locator('#fit').bounding_box()
                    assert fit['y']+fit['height'] <= viewer['y']+viewer['height']+1, (width,height,fit,viewer)
            page.screenshot(path=str(out/f'editor-{width}x{height}.png'), full_page=True)
        check('Four laptop/desktop sizes: footer and page stay in viewport, jobs open/closed')
        page.locator('#newProject').click()
        page.locator('#projectTitle').fill('Dự án nhập bằng editor')
        page.locator('#sourcePath').fill(str(Path(__file__).parent/'fixtures/demo.mp4'))
        page.locator('#importForm button[type=submit]').click()
        expect(page.locator('#projectName')).to_have_text('Dự án nhập bằng editor')
        page.wait_for_function('document.querySelector("#video").readyState >= 2')
        assert len(state()['projects'])==2
        check('New-project form imports a video and selects the new project')
        assert not errors, errors
        print(json.dumps({'passed':len(checks),'checks':checks,'browser_errors':errors,'worker_observations':seen},ensure_ascii=False,indent=2))
    except Exception:
        print('BROWSER ERRORS', errors, flush=True)
        print('VISIBLE TOAST', page.locator('#toast').inner_text(), flush=True)
        page.screenshot(path=str(out/'failure.png'), full_page=True)
        raise
    finally:
        browser.close()
        try:
            request.post('/api/test/shutdown')
            # Let uvicorn finish its finally/temporary cleanup before the helper exits.
            for _ in range(30):
                try:
                    with socket.create_connection(('127.0.0.1', 18774), timeout=.2):
                        pass
                except OSError:
                    time.sleep(.3)
                    break
                time.sleep(.1)
        except Exception:
            pass  # Do not obscure the original test failure if server has exited.
        request.dispose()
