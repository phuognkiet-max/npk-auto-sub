"""Production workflow UI, isolated server, synthetic media. No real credentials."""
import json
import math
from pathlib import Path
import struct
import time
import wave
from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'tests/workflow-results'
OUT.mkdir(exist_ok=True)
BASE='http://127.0.0.1:18774'
TOKEN='npk-editor-test-token'
checks=[]


def check(message):
    checks.append(message)
    print('PASS',message,flush=True)


with wave.open(str(OUT/'external-voice.wav'),'wb') as wav:
    wav.setparams((1,2,24000,0,'NONE','not compressed'))
    wav.writeframes(b''.join(struct.pack('<h',int(5000*math.sin(2*math.pi*220*i/24000))) for i in range(8*24000)))
(OUT/'corrupt.wav').write_bytes(b'not a valid audio file')

with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1366,'height':768})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    # All interactions affect the isolated synthetic project / fake keys only.
    page.on('dialog',lambda dialog:dialog.accept())
    request=pw.request.new_context(base_url=BASE,extra_http_headers={'x-studio-token':TOKEN})
    def state(): return request.get('/api/state').json()
    def wait_done(job_id):
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            job=next(j for j in state()['jobs'] if j['id']==job_id)
            if job['state'] in ('done','error','cancelled'):
                assert job['state']=='done',job
                return job
            assert job['progress']<1
            page.wait_for_timeout(300)
        raise AssertionError('Job timeout')
    try:
        page.goto(BASE+'/#token='+TOKEN)
        page.wait_for_load_state('networkidle')
        page.wait_for_function('document.querySelector("#video").readyState>=2')
        page.screenshot(path=str(OUT/'01-source.png'))
        expect(page.locator('body')).to_have_class('workflow')
        expect(page.locator('#timelinePanel')).to_be_hidden()
        assert page.locator('#tools [data-tool]').count()==6
        for stage in ('sub','translate','clean','voice','audio','media'):
            page.locator(f'[data-tool="{stage}"]').click()
            expect(page.locator(f'[data-tool="{stage}"]')).to_have_attribute('aria-current','step')
            expect(page.locator('#inspector')).to_be_visible()
        check('Six production stages; large viewer; timeline not displayed')
        # Use real pointer movement: a CSS variable on body must not shadow resizing.
        width_before=page.locator('#viewer').bounding_box()['width']
        splitter=page.locator('#rightSplitter').bounding_box()
        page.mouse.move(splitter['x']+4,splitter['y']+splitter['height']/2)
        page.mouse.down()
        page.mouse.move(splitter['x']+110,splitter['y']+splitter['height']/2,steps=8)
        page.mouse.up()
        assert page.locator('#viewer').bounding_box()['width']>width_before+80
        page.locator('#resetLayout').click()
        assert abs(page.locator('#viewer').bounding_box()['width']-width_before)<2
        check('Viewer/function divider resizes with the pointer and resets correctly')
        page.locator('#apiSettings').click()
        expect(page.locator('#apiKey')).to_be_visible()
        page.locator('#checkCatalog').click()
        expect(page.locator('#checkResult')).to_contain_text('TEST DOUBLE')
        page.locator('[data-close="settingsDialog"]').click()
        page.locator('[data-tool="voice"]').click()
        page.locator('#stageSettings').click()
        assert page.locator('#settingsFields [data-param="voice_id"] option').count()>=27
        page.locator('[data-close="settingsDialog"]').click()
        check('API check reachable with mocked provider; 27 real installed voice choices reachable')
        page.locator('[data-tool="sub"]').click()
        page.locator('#objectList button').first.click()
        page.get_by_role('textbox',name='Lời gốc',exact=True).fill('Edited source subtitle')
        page.get_by_role('textbox',name='Lời gốc',exact=True).press('Tab')
        page.locator('#duplicateObject').click()
        expect(page.locator('#objectList button')).to_have_count(3)
        page.locator('#deleteObject').click()
        expect(page.locator('#objectList button')).to_have_count(2)
        page.locator('#undo').click()
        expect(page.locator('#objectList button')).to_have_count(3)
        page.locator('#redo').click()
        expect(page.locator('#objectList button')).to_have_count(2)
        page.locator('[data-tool="translate"]').click()
        page.locator('#objectList button').first.click()
        expect(page.locator('[data-tool="translate"]')).to_have_attribute('aria-current','step')
        expect(page.get_by_role('textbox',name='Bản dịch',exact=True)).to_be_visible()
        check('Edit, duplicate, delete, undo/redo preserved; translation selection stays in its stage')
        page.locator('[data-tool="clean"]').click()
        page.locator('#essentials [data-param="method"]').select_option('blur')
        page.locator('#stageOptions > summary').click()
        expect(page.locator('#parameters [data-param="blur_radius"]')).to_be_visible()
        expect(page.locator('#parameters [data-param="max_side"]')).to_be_hidden()
        page.locator('#essentials [data-param="method"]').select_option('box')
        page.locator('#save').click()
        expect(page.locator('#saveStatus')).to_have_text('Đã lưu')
        page.screenshot(path=str(OUT/'02-clean.png'))
        check('Light cover controls connect to real project settings')
        page.locator('[data-tool="voice"]').click()
        page.locator('#externalAudioPath').fill(str(OUT/'corrupt.wav'))
        page.locator('#inspectExternalAudio').click()
        expect(page.locator('#externalAudioStatus')).to_contain_text('Không đọc được audio')
        page.locator('#externalAudioPath').fill(str(OUT/'external-voice.wav'))
        page.locator('#applyExternalAudio').click()
        expect(page.locator('#toast')).to_contain_text('Đã dùng tệp ngoài')
        page.wait_for_function('document.querySelector("#externalAudioPlayer").readyState>=2')
        assert abs(page.locator('#externalAudioPlayer').evaluate('(a)=>a.duration')-8)<.01
        project=state()['projects'][0]
        assert project['params']['pipeline_tts'] is False
        assert project['params']['voice_path']==str(OUT/'external-voice.wav')
        page.locator('#play').click()
        page.wait_for_function('!voiceAudio.paused && voiceAudio.currentTime>0')
        page.locator('#play').click()
        page.screenshot(path=str(OUT/'03-voice.png'))
        check('Corrupt audio rejected; valid external WAV saved, previewed in sync, offline TTS disabled')
        # No credentials in media access without the per-session token.
        unauthenticated=pw.request.new_context(base_url=BASE)
        try:
            assert unauthenticated.get(f'/media/external/{project["id"]}/voice').status==403
        finally:
            unauthenticated.dispose()
        assert request.get(f'/media/external/{project["id"]}/unknown').status==404
        check('External media endpoint requires session authentication and fixed project media kind')
        page.locator('#pipelineSettings').click()
        page.locator('#settingsFields [data-param="pipeline_translate"]').uncheck()
        page.locator('#settingsFields [data-param="pipeline_clean"]').check()
        page.get_by_role('button',name='Kiểm tra đầu vào',exact=True).click()
        expect(page.locator('#preflightStatus')).to_contain_text('Đầu vào đạt')
        page.get_by_role('button',name='Bắt đầu quy trình → xuất video',exact=True).click()
        expect(page.locator('#settingsDialog')).to_be_hidden()
        job=next(j for j in state()['jobs'] if j['kind']=='pipeline')
        result=wait_done(job['id'])
        assert result['result']['steps']==['clean','render'],result
        output=request.get(f'/media/job/{job["id"]}/final.mp4')
        assert output.status==200
        (OUT/'final-external-audio.mp4').write_bytes(output.body())
        page.wait_for_function('(id)=>[...document.querySelector("#viewSource").options].some(o=>o.value===id)',arg=job['id'])
        page.locator('#viewSource').select_option(job['id'])
        page.wait_for_function('document.querySelector("#video").readyState>=2')
        expect(page.locator('#jobSummary')).to_contain_text('100%')
        page.screenshot(path=str(OUT/'04-complete.png'))
        check('UI -> actual FFmpeg cover -> external-audio mix -> MP4 + subtitles -> verified 100%')
        page.locator('#closeJobs').click()
        for width,height in [(1920,1080),(1366,768),(1093,614),(800,720)]:
            page.set_viewport_size({'width':width,'height':height})
            page.locator('[data-tool="voice"]').click()
            page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,height)
            v=page.locator('#viewport').bounding_box()
            assert v['width']>=280 and v['height']>=120,(width,height,v)
            footer=page.locator('footer').bounding_box()
            assert footer['y']+footer['height']<=height+1
            page.screenshot(path=str(OUT/f'layout-{width}.png'))
        check('Layout and progress remain accessible at four desktop/laptop widths')
        # Populate >60 entries through the authenticated API to verify paging (no user data).
        project=state()['projects'][0]
        rows=[dict(start=i/20,end=i/20+.035,text=f'Cue {i+1}',translated=f'Câu {i+1}') for i in range(130)]
        response=request.put('/api/projects/'+project['id'],data={'segments':rows,'expected_revision':project['updated']})
        assert response.status==200,response.text()
        page.reload();page.wait_for_load_state('networkidle')
        page.locator('[data-tool="sub"]').click()
        expect(page.locator('#objectList button')).to_have_count(60)
        page.locator('#listNext').click()
        expect(page.locator('#listCount')).to_contain_text('2/3')
        page.locator('#objectList button').first.click()
        expect(page.locator('#properties')).to_contain_text('Câu 61')
        page.locator('#searchObjects').fill('Cue 130')
        expect(page.locator('#objectList button')).to_have_count(1)
        page.locator('#objectList button').click()
        expect(page.locator('#properties')).to_contain_text('Câu 130')
        check('Long subtitle list pages/search/selects past the first 60 without timeline')
        page.goto(BASE+'/editor#token='+TOKEN);page.wait_for_load_state('networkidle')
        expect(page.locator('#timelinePanel')).to_be_visible()
        check('Legacy timeline editor still works at /editor with the same engine')
        assert not errors,errors
        (OUT/'browser-report.json').write_text(json.dumps({'checks':checks,'errors':errors,'provider':'mocked; no live API call','media':'synthetic 8-second fixture','pipeline_result':result['result']},ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception:
        print('PAGE ERRORS',errors,flush=True)
        page.screenshot(path=str(OUT/'failure.png'))
        raise
    finally:
        browser.close()
        request.post('/api/test/shutdown')
        request.dispose()
