"""Mouse/keyboard acceptance for the region editor, with isolated DATA and no jobs.

Run with the webapp-testing with_server.py helper and this file's --serve mode.
The fixture uses local demo.mp4, a fake processed artifact and a second project;
it never opens user projects, starts workers or reads credentials.
"""
import copy
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tests/region-editor-ui-results'
PORT = 19874
TOKEN = 'npk-region-editor-isolated-fixture'
sys.path.insert(0, str(ROOT))

DEFAULT_REGIONS = [
    {'x': 80.125, 'y': 60.375, 'w': 150.625, 'h': 70.875, 'start': 0., 'end': 8.},
    {'x': 320., 'y': 210., 'w': 130., 'h': 65., 'start': 0., 'end': 8.},
]


def serve():
    OUT.mkdir(exist_ok=True)
    cleanup_file = OUT / '.cleanup.json'
    cleanup_file.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix='npk-region-browser-', dir=ROOT / 'tests') as temporary:
        os.environ['VIETDUB_DATA_DIR'] = temporary
        os.environ['VIETDUB_TOKEN'] = TOKEN
        import credentials
        credentials.keyring.get_password = lambda *args: None
        import server
        import uvicorn
        from core import create_project, put, get, DATA
        project = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Region editor fixture')
        project['params'].update(method='box', cover_opacity=.85, use_clean=True,
                                 pipeline_translate=False, pipeline_tts=False)
        project['segments'] = [{'start': 0., 'end': 8., 'text': 'Fixture subtitle', 'translated': ''}]
        project['regions'] = copy.deepcopy(DEFAULT_REGIONS)
        job_id = uuid.uuid4().hex
        job_folder = DATA / 'jobs' / job_id
        job_folder.mkdir()
        shutil.copyfile(ROOT / 'tests/fixtures/demo.mp4', job_folder / 'clean.mp4')
        job = {'id': job_id, 'project_id': project['id'], 'kind': 'clean', 'state': 'done',
               'created': time.time(), 'ended': time.time(), 'result': {'files': ['clean.mp4']}}
        put('job', job_id, job)
        project['artifacts']['clean'] = {'job_id': job_id, 'files': ['clean.mp4']}
        put('project', project['id'], project)
        second = create_project(str(ROOT / 'tests/fixtures/demo.mp4'), 'Other project fixture')
        second['regions'] = [{'x': 20., 'y': 20., 'w': 60., 'h': 40., 'start': 0., 'end': 8.}]
        put('project', second['id'], second)
        info = {'project': project['id'], 'other_project': second['id'], 'width': 640, 'height': 360}
        http = uvicorn.Server(uvicorn.Config(server.app, host='127.0.0.1', port=PORT,
                                            access_log=False, log_level='warning', timeout_graceful_shutdown=2))

        @server.app.get('/api/test/info')
        def fixture_info():
            return info

        @server.app.post('/api/test/reset')
        def reset(body: dict):
            row = get('project', project['id'])
            row['regions'] = copy.deepcopy(body.get('regions', DEFAULT_REGIONS))
            row.pop('extraction_regions', None)
            if 'extraction_regions' in body:
                row['extraction_regions'] = copy.deepcopy(body['extraction_regions'])
            row['updated'] = max(time.time(), row['updated'] + .001)
            put('project', row['id'], row)
            return row

        @server.app.post('/api/test/stop')
        def stop():
            http.should_exit = True
            return {}

        timer = threading.Timer(420, lambda: setattr(http, 'should_exit', True))
        timer.daemon = True
        timer.start()
        try:
            http.run()
        finally:
            timer.cancel()
            import gc
            gc.collect()
    cleanup_file.write_text(json.dumps({'data_removed': not Path(temporary).exists(),
                                        'workers_started': False, 'api_generation_requests': 0}), encoding='utf-8')


def browser():
    import httpx
    from playwright.sync_api import sync_playwright, expect
    OUT.mkdir(exist_ok=True)
    base = f'http://127.0.0.1:{PORT}'
    checks, errors, writes, screenshots = [], [], [], []

    @contextmanager
    def diagnostic_playwright():
        with sync_playwright() as pw:
            try:
                yield pw
            except Exception:
                if 'page' in locals_for_diagnostics and not locals_for_diagnostics['page'].is_closed():
                    active = locals_for_diagnostics['page']
                    active.screenshot(path=str(OUT / 'failure.png'), full_page=True)
                    (OUT / 'failure-state.json').write_text(json.dumps({
                        'checks': checks, 'javascript_errors': errors,
                        'state': active.evaluate('({area,regions:P?.regions,extraction:P?.extraction_regions,undo:undo.length,dirty,drag:Boolean(drag)})'),
                        'menu': active.evaluate('({selected:selectedRegionIndices(),menu:document.querySelector("#regionContextMenu")?.outerHTML,focused:document.activeElement?.outerHTML?.slice(0,1800)})'),
                        'panel': active.locator('#panel').inner_text(),
                    }, ensure_ascii=False, indent=2), encoding='utf-8')
                raise

    locals_for_diagnostics = {}
    with httpx.Client(base_url=base, headers={'x-studio-token': TOKEN}, trust_env=False, timeout=20) as client:
        info = client.get('/api/test/info').json()
        try:
            with diagnostic_playwright() as pw:
                app = pw.chromium.launch(channel='msedge', headless=True)
                page = app.new_page(viewport={'width': 1440, 'height': 1050})
                locals_for_diagnostics['page'] = page
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: writes.append(request.url)
                        if request.method == 'PUT' and '/api/projects/' in request.url else None)
                page.goto(base + '/studio#token=' + TOKEN)
                page.wait_for_load_state('networkidle')
                page.wait_for_function('Boolean(P?.id)')

                def geometry():
                    return page.evaluate('structuredClone(P.regions)')

                def wait_saved():
                    page.wait_for_function('!dirty && !saving && !drag', timeout=15000)

                def reset(regions=None, extraction=None, mode='clean'):
                    if page.evaluate('dirty'):
                        page.evaluate('save()')
                    payload = {'regions': copy.deepcopy(DEFAULT_REGIONS if regions is None else regions)}
                    if extraction is not None:
                        payload['extraction_regions'] = extraction
                    client.post('/api/test/reset', json=payload).raise_for_status()
                    page.evaluate('(id)=>loadProject(id)', info['project'])
                    page.evaluate('(area)=>setArea(area)', mode)
                    page.wait_for_function('video.readyState >= 2 && !video.seeking')
                    page.evaluate('video.pause(); video.currentTime=0;drawRegions()')
                    page.wait_for_function('video.readyState >= 2 && !video.seeking')
                    expect(page.locator('#regions')).to_be_visible()
                    return geometry()

                def overlay_point(x, y):
                    box = element_box(page.locator('#regions'))
                    assert box and box['width'] > 100 and box['height'] > 50, box
                    return box['x'] + x / 640 * box['width'], box['y'] + y / 360 * box['height']

                def region_center(index):
                    box = element_box(page.locator(f'.region[data-region-index="{index}"]'))
                    assert box, index
                    return box['x'] + box['width'] / 2, box['y'] + box['height'] / 2

                def element_box(locator):
                    return locator.evaluate('(el)=>{const r=el.getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height}}')

                def mouse_drag(start, finish, before, *, cancel=None, shift=False, shift_before=False):
                    wait_saved()
                    count = len(writes)
                    history = page.evaluate('undo.length')
                    page.mouse.move(*start)
                    if shift and shift_before:
                        page.keyboard.down('Shift')
                    page.mouse.down()
                    if shift and not shift_before:
                        page.keyboard.down('Shift')
                    page.mouse.move(*finish, steps=8)
                    page.wait_for_timeout(800)
                    assert geometry() == before, 'Dragging mutated persistent project before pointerup'
                    stored = client.get('/api/studio/projects/' + info['project']).json()
                    assert stored['regions'] == before
                    assert len(writes) == count, 'Dragging auto-saved before pointerup'
                    assert page.evaluate('undo.length') == history, 'Dragging polluted undo history'
                    if cancel == 'escape':
                        page.keyboard.press('Escape')
                    elif cancel == 'pointercancel':
                        page.evaluate('document.querySelector("#regions").dispatchEvent(new PointerEvent("pointercancel", {pointerId:drag.pointerId,bubbles:true}))')
                    page.mouse.up()
                    if shift:
                        page.keyboard.up('Shift')
                    if cancel:
                        assert geometry() == before
                        assert page.evaluate('undo.length') == history
                        assert len(writes) == count
                    else:
                        assert page.evaluate('undo.length') == history + 1, 'A drag must commit one undo entry'
                        wait_saved()
                        assert len(writes) == count + 1, 'A drag must persist one commit'

                original = reset([])
                page.locator('#drawRegion').click()
                mouse_drag(overlay_point(65, 65), overlay_point(215, 135), original)
                drawn = geometry()
                assert len(drawn) == 1
                for key, value in {'x': 65, 'y': 65, 'w': 150, 'h': 70}.items():
                    assert abs(drawn[0][key] - value) < 2, (key, drawn)
                assert drawn[0]['start'] == 0 and drawn[0]['end'] == 8
                page.keyboard.press('Control+z')
                assert geometry() == []
                page.keyboard.press('Control+y')
                assert geometry() == drawn
                wait_saved()
                checks.append('Drawing is transient while held, persists once at release and has exactly one undo/redo step')

                original = reset([DEFAULT_REGIONS[0]])
                start = region_center(0)
                scale = element_box(page.locator('#regions'))['width'] / 640
                mouse_drag(start, (start[0] + 60 * scale, start[1] + 45 * scale), original)
                moved = geometry()[0]
                assert abs(moved['x'] - original[0]['x'] - 60) < 2
                assert abs(moved['y'] - original[0]['y'] - 45) < 2
                assert moved['w'] == original[0]['w'] and moved['h'] == original[0]['h']
                page.locator('#undo').click()
                assert geometry() == original
                page.locator('#redo').click()
                assert geometry()[0] == moved
                wait_saved()
                checks.append('Existing regions move with the mouse without changing size, with one commit and undo/redo')

                for handle in ('n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'):
                    original = reset([{'x': 150., 'y': 100., 'w': 180., 'h': 100., 'start': 0., 'end': 8.}])
                    page.mouse.click(*region_center(0))
                    control = page.locator(f'.region.selected .region-handle[data-handle="{handle}"]')
                    expect(control).to_be_visible()
                    box = element_box(control)
                    start = (box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
                    scale = element_box(page.locator('#regions'))['width'] / 640
                    dx = -25 if 'w' in handle else 25 if 'e' in handle else 0
                    dy = -20 if 'n' in handle else 20 if 's' in handle else 0
                    mouse_drag(start, (start[0] + dx * scale, start[1] + dy * scale), original)
                    resized = geometry()[0]
                    assert resized != original[0], handle
                    assert abs(resized['w'] - (180 + (25 if dx else 0))) < 2, (handle, resized)
                    assert abs(resized['h'] - (100 + (20 if dy else 0))) < 2, (handle, resized)
                    assert 0 <= resized['x'] < resized['x'] + resized['w'] <= 640
                    assert 0 <= resized['y'] < resized['y'] + resized['h'] <= 360
                checks.append('All eight edge/corner handles resize in their expected axes and remain inside the video')

                original = reset([{'x': 150., 'y': 100., 'w': 180., 'h': 100., 'start': 0., 'end': 8.}])
                page.mouse.click(*region_center(0))
                box = element_box(page.locator('.region.selected .region-handle[data-handle="se"]'))
                start = (box['x'] + box['width']/2, box['y'] + box['height']/2)
                mouse_drag(start, (start[0] + 70, start[1] + 15), original, shift=True)
                aspect = geometry()[0]
                assert abs(aspect['w'] / aspect['h'] - 1.8) < .01
                original = reset([{'x': 150., 'y': 100., 'w': 180., 'h': 100., 'start': 0., 'end': 8.}])
                page.mouse.click(*region_center(0))
                box = element_box(page.locator('.region.selected .region-handle[data-handle="se"]'))
                start = (box['x'] + box['width']/2, box['y'] + box['height']/2)
                mouse_drag(start, (start[0]+70, start[1]+15), original, shift=True, shift_before=True)
                assert abs(geometry()[0]['w']/geometry()[0]['h']-1.8) < .01
                checks.append('Holding Shift before or during a corner resize preserves the original aspect ratio')

                original = reset([DEFAULT_REGIONS[0]])
                start = region_center(0)
                mouse_drag(start, (start[0] + 5000, start[1] + 5000), original)
                bounded = geometry()[0]
                assert abs(bounded['x'] + bounded['w'] - 640) < .01
                assert abs(bounded['y'] + bounded['h'] - 360) < .01
                original = reset([DEFAULT_REGIONS[0]])
                start = region_center(0)
                mouse_drag(start, (start[0] + 35, start[1] + 20), original, cancel='escape')
                mouse_drag(start, (start[0] + 35, start[1] + 20), original, cancel='pointercancel')
                checks.append('Movement clamps at the frame edges; Escape and pointercancel discard transient edits without saving')

                original = reset([DEFAULT_REGIONS[0]])
                page.mouse.click(*region_center(0), button='right')
                expect(page.locator('#regionContextMenu')).to_be_visible()
                original_menu_geometry = geometry()
                page.keyboard.press('ArrowDown')
                assert geometry() == original_menu_geometry
                assert page.evaluate('Boolean(document.activeElement.closest("#regionContextMenu"))')
                page.locator('#regionContextMenu [data-region-command="duplicate"]').click()
                assert len(geometry()) == 2 and geometry()[0] == original[0]
                wait_saved()
                duplicate = page.evaluate('selectedRegionIndices()[0]')
                page.mouse.click(*region_center(duplicate), button='right')
                page.locator('#regionContextMenu [data-region-command="delete"]').click()
                assert geometry() == original
                wait_saved()
                page.mouse.click(*region_center(0), button='right')
                expect(page.locator('#regionContextMenu')).to_be_visible()
                page.keyboard.press('Control+d')
                assert len(geometry()) == 2 and geometry()[0] == original[0]
                wait_saved()
                duplicate = page.evaluate('selectedRegionIndices()[0]')
                page.mouse.click(*region_center(duplicate), button='right')
                expect(page.locator('#regionContextMenu')).to_be_visible()
                page.keyboard.press('Delete')
                assert geometry() == original
                wait_saved()
                checks.append('Context-menu arrows only move focus; Duplicate/Delete buttons and keyboard commands preserve the original')

                regions = [
                    {'x': 80., 'y': 60., 'w': 150., 'h': 70., 'start': 1., 'end': 5.},
                    {'x': 300., 'y': 180., 'w': 100., 'h': 60., 'start': 3., 'end': 7.},
                ]
                reset(regions)
                page.evaluate('video.currentTime=4;drawRegions()')
                page.wait_for_function('!video.seeking && video.readyState >= 2')
                page.mouse.click(*region_center(0))
                page.keyboard.down('Control')
                page.mouse.click(*region_center(1))
                page.keyboard.up('Control')
                assert page.evaluate('selectedRegionIndices()') == [0, 1]
                page.mouse.click(*region_center(1), button='right')
                page.locator('#regionContextMenu [data-region-command="merge"]').click()
                expect(page.locator('#modal')).to_be_visible()
                expect(page.locator('#modalTitle')).to_contain_text('khác thời gian')
                page.locator('#modalActions button').filter(has_text='Gộp vùng').click()
                assert geometry() == [{'x': 80., 'y': 60., 'w': 320., 'h': 180., 'start': 1., 'end': 7.}]
                wait_saved()
                page.locator('#undo').click()
                assert geometry() == regions
                wait_saved()
                checks.append('Ctrl-click selects multiple regions; Merge uses geometric and time unions and can be undone once')

                page.mouse.click(*region_center(0))
                page.keyboard.down('Control')
                page.mouse.click(*region_center(1))
                page.keyboard.up('Control')
                start = region_center(1)
                scale = element_box(page.locator('#regions'))['width'] / 640
                mouse_drag(start, (start[0]+20*scale, start[1]+10*scale), regions)
                grouped = geometry()
                assert all(abs(row['x']-before['x']-20)<2 and abs(row['y']-before['y']-10)<2
                           and row['w']==before['w'] and row['h']==before['h']
                           and row['start']==before['start'] and row['end']==before['end']
                           for row,before in zip(grouped,regions))
                checks.append('Dragging a multiple selection moves the whole group together, retaining sizes and timing')

                reset([{'x': 80., 'y': 60., 'w': 150., 'h': 70., 'start': 1., 'end': 7.}])
                for command, at in (('startHere', 3.), ('endHere', 6.), ('fullTime', 4.)):
                    page.evaluate('(at)=>{video.currentTime=at;drawRegions()}', at)
                    page.wait_for_function('!video.seeking && video.readyState >= 2')
                    page.mouse.click(*region_center(0), button='right')
                    page.locator(f'#regionContextMenu [data-region-command="{command}"]').click()
                    wait_saved()
                    if command == 'startHere':
                        assert geometry()[0]['start'] == at and geometry()[0]['end'] == 7
                    elif command == 'endHere':
                        assert geometry()[0]['start'] == 3 and geometry()[0]['end'] == at
                    else:
                        assert geometry()[0]['start'] == 0 and geometry()[0]['end'] == 8
                checks.append('Context-menu timing commands set start/end at the playhead and restore whole-video coverage')

                original = reset([DEFAULT_REGIONS[0]])
                page.mouse.click(*region_center(0))
                position = page.evaluate('video.currentTime')
                page.keyboard.press('ArrowRight')
                assert abs(geometry()[0]['x'] - original[0]['x'] - 1) < .01
                assert page.evaluate('video.currentTime') == position
                page.keyboard.press('Shift+ArrowDown')
                assert abs(geometry()[0]['y'] - original[0]['y'] - 10) < .01
                wait_saved()
                before_input = geometry()
                inspector_input = page.locator('#regionInspector input').first
                expect(inspector_input).to_be_visible()
                inspector_input.focus()
                page.keyboard.press('Delete')
                assert geometry() == before_input, 'Delete inside an input deleted the region'
                page.locator('#next').click()
                page.keyboard.press('ArrowRight')
                assert geometry() == before_input, 'Viewer navigation moved a region outside editor focus'
                assert page.evaluate('video.currentTime') > position
                page.wait_for_function('!video.seeking && video.readyState >= 2')
                page.mouse.click(*region_center(0))
                page.wait_for_function('document.activeElement===document.querySelector("#regions")')
                page.keyboard.press('Delete')
                assert geometry() == []
                wait_saved()
                checks.append('Arrow keys move focused regions, Shift moves ten pixels, input Delete is safe, region Delete works and viewer arrows continue seeking')

                original = reset([DEFAULT_REGIONS[0]])
                start = region_center(0)
                page.mouse.move(*start)
                page.mouse.down()
                page.mouse.move(start[0] + 45, start[1] + 25, steps=6)
                page.locator('#viewSource').select_option('clean')
                page.mouse.up()
                assert geometry() == original and page.evaluate('drag === null')
                reset([DEFAULT_REGIONS[0]])
                start = region_center(0)
                page.mouse.move(*start)
                page.mouse.down()
                page.mouse.move(start[0] + 45, start[1] + 25, steps=6)
                page.evaluate('(id)=>loadProject(id)', info['other_project'])
                page.mouse.up()
                assert page.evaluate('drag === null')
                assert client.get('/api/studio/projects/' + info['project']).json()['regions'] == original
                assert page.evaluate('P.id') == info['other_project']
                checks.append('Changing source view or switching projects cancels a held drag without changing either project')

                clean = copy.deepcopy(DEFAULT_REGIONS)
                extraction = [{'x': 40., 'y': 270., 'w': 300., 'h': 50., 'start': 0., 'end': 8.}]
                reset(clean, extraction, mode='subtitles')
                start = region_center(0)
                page.mouse.move(*start)
                page.mouse.down()
                page.mouse.move(start[0] + 25, start[1] - 12, steps=6)
                assert geometry() == clean and page.evaluate('P.extraction_regions') == extraction
                page.mouse.up()
                wait_saved()
                assert geometry() == clean
                changed_reading = page.evaluate('structuredClone(P.extraction_regions)')
                assert changed_reading != extraction
                stored = client.get('/api/studio/projects/' + info['project']).json()
                assert stored['regions'] == clean and stored['extraction_regions'] == changed_reading
                checks.append('The subtitle reading editor saves extraction_regions independently and never changes cleaning regions')

                precise = {**DEFAULT_REGIONS[0], 'end': 7.991995}
                reset([precise])
                page.mouse.click(*region_center(0))
                assert page.evaluate('cleanLiveSnapshot().regions[0].end') == precise['end']
                wait_saved()
                count, history = len(writes), page.evaluate('undo.length')
                end_input = page.locator('#regionInspector .region-input[data-key="end"]')
                end_input.fill('0')
                end_input.press('Tab')
                expect(end_input).to_have_value('7.99')
                assert geometry() == [precise]
                assert page.evaluate('cleanLiveSnapshot().regions[0].end') == precise['end']
                assert len(writes) == count and page.evaluate('undo.length') == history
                checks.append('Rounded fields retain precise stored timing; rejected end times restore the field without changing preview, save or history')

                reset([DEFAULT_REGIONS[0]])
                page.mouse.click(*region_center(0))
                page.locator('#regionCoordinates summary').click()
                width_input = page.locator('#regionInspector .region-input[data-key="w"]')
                width_input.fill('180')
                assert geometry()[0]['w'] == DEFAULT_REGIONS[0]['w']
                start = region_center(0)
                page.mouse.move(*start)
                page.mouse.down()
                assert geometry()[0]['w'] == 180, 'Focusing the overlay must commit the numeric edit before the drag snapshot'
                page.mouse.move(start[0]+35, start[1]+15, steps=8)
                page.mouse.up()
                wait_saved()
                assert geometry()[0]['w'] == 180 and geometry()[0]['x'] > DEFAULT_REGIONS[0]['x']+20
                assert page.evaluate('undo.length') == 2
                reset([DEFAULT_REGIONS[0]])
                page.mouse.click(*region_center(0))
                page.locator('#regionCoordinates summary').click()
                page.locator('#regionInspector .region-input[data-key="w"]').fill('185')
                page.mouse.click(*overlay_point(550, 40))
                assert geometry()[0]['w'] == 185
                assert page.evaluate('selectedRegionIndices()') == []
                wait_saved()
                checks.append('An uncommitted width edit is retained when immediately dragging or clicking blank video, and the drag has a separate undo step')

                reset([DEFAULT_REGIONS[0]])
                page.mouse.click(*region_center(0))
                page.keyboard.press('Shift+ArrowRight')
                wait_saved()
                persisted = geometry()
                page.reload()
                page.wait_for_load_state('networkidle')
                page.wait_for_function('Boolean(P?.id) && !dirty')
                assert page.evaluate('P.id') == info['project'] and geometry() == persisted
                page.locator('#nav [data-area="clean"]').click()
                page.wait_for_function('!video.seeking && video.readyState >= 2')
                page.mouse.click(*region_center(0))
                assert page.evaluate('selectedRegionIndices()') == [0]
                expect(page.locator('.region.selected .region-handle')).to_have_count(8)
                assert all(len(value.split('.')[-1]) <= 2 for value in
                           page.locator('#regionInspector input[type="number"]').evaluate_all('(nodes)=>nodes.map(n=>n.value)')
                           if '.' in value), 'Inspector exposes raw floating-point tails'
                assert 'Kéo vùng hoặc thanh chỉnh để xem hiệu ứng ngay trên video' not in page.locator('#panel').inner_text()
                assert 'Chưa có video đã xử lý. Bấm Xem ảnh tại vị trí này' not in page.locator('#panel').inner_text()
                assert page.locator('#drawRegion').get_attribute('title') or page.locator('#drawRegion').get_attribute('aria-label')
                for width, height in ((1920, 1080), (1440, 900), (1280, 800)):
                    page.set_viewport_size({'width': width, 'height': height})
                    page.wait_for_timeout(250)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                    panel_box = page.locator('#panel').bounding_box()
                    overlay_box = page.locator('#regions').bounding_box()
                    assert panel_box and overlay_box and panel_box['width'] >= 250 and overlay_box['width'] >= 150
                    path = OUT / f'editor-{width}.png'
                    page.screenshot(path=str(path), full_page=False)
                    screenshots.append(str(path))
                page.set_viewport_size({'width': 1440, 'height': 900})
                page.wait_for_timeout(250)
                page.mouse.click(*region_center(0))
                expect(page.locator('.region.selected .region-handle')).to_have_count(8)
                path = OUT / 'editor-selected-1440.png'
                page.screenshot(path=str(path), full_page=False)
                screenshots.append(str(path))
                page.mouse.click(*region_center(0), button='right')
                expect(page.locator('#regionContextMenu')).to_be_visible()
                path = OUT / 'editor-context-menu-1440.png'
                page.screenshot(path=str(path), full_page=False)
                screenshots.append(str(path))
                checks.append('Autosave survives reload; rounded inspector values, compact notes and accessible tools fit 1920/1440/1280 widths')
                assert not errors, errors
                app.close()
        finally:
            client.post('/api/test/stop')
            for _ in range(300):
                if (OUT / '.cleanup.json').is_file():
                    break
                time.sleep(.1)
    cleanup = json.loads((OUT / '.cleanup.json').read_text(encoding='utf-8'))
    (OUT / '.cleanup.json').unlink()
    assert cleanup['data_removed'] and not cleanup['workers_started'] and cleanup['api_generation_requests'] == 0
    report = {'ok': True, 'checks': checks, 'javascript_errors': errors, 'cleanup': cleanup,
              'screenshots': screenshots, 'fixture': True}
    (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (OUT / 'failure.png').unlink(missing_ok=True)
    (OUT / 'failure-state.json').unlink(missing_ok=True)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    serve() if '--serve' in sys.argv else browser()
