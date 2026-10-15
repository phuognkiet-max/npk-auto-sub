"""0.6 interaction audit. Isolated fixture, real local voice engine, fake API keys."""
import json
from pathlib import Path
import time
from playwright.sync_api import sync_playwright, expect

OUT=Path(__file__).parent/'usability-results';OUT.mkdir(exist_ok=True)
BASE='http://127.0.0.1:18774';TOKEN='npk-editor-test-token'
checks=[];surfaces=[]
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1366,'height':768})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    dialog_answers=[]
    page.on('dialog',lambda d:d.accept() if not dialog_answers or dialog_answers.pop(0) else d.dismiss())
    req=pw.request.new_context(base_url=BASE,extra_http_headers={'x-studio-token':TOKEN})
    def state():return req.get('/api/state').json()
    def check(message):checks.append(message);print('PASS',message,flush=True)
    try:
        page.goto(BASE+'/#token='+TOKEN);page.wait_for_load_state('networkidle')
        page.wait_for_function('document.querySelector("#video").readyState>=2')
        for stage in ['media','sub','translate','clean','voice','audio']:
            page.locator(f'[data-tool="{stage}"]').click()
            expect(page.locator('#stagePanel')).to_be_visible()
            page.screenshot(path=str(OUT/(stage+'.png')))
            surfaces.append({'stage':stage,'buttons':page.locator('#stagePanel button:visible').all_text_contents()})
        assert not errors,errors
        expect(page.locator('#topbar #export')).to_be_hidden()
        expect(page.locator('#topbar #voiceSettings')).to_be_hidden()
        check('All six stages render, header removes duplicate voice/export entries')
        # Stage settings are always available, but uncommon options do not obscure cues.
        page.locator('[data-tool="clean"]').click()
        expect(page.locator('#essentials [data-param="method"]')).to_be_visible()
        expect(page.locator('#stageOptions')).not_to_have_attribute('open','')
        assert page.locator('#actions button.primary').count()==1
        assert page.locator('#actions button.primary').inner_text()=='Xem thử 3 giây'
        page.locator('#stageOptions > summary').click()
        expect(page.locator('.extra-actions')).to_be_visible()
        page.locator('#essentials [data-param="method"]').select_option('blur')
        expect(page.locator('#parameters [data-param="blur_radius"]')).to_be_visible()
        assert page.locator('#stagePanel [data-param="method"]').count()==1
        check('Basic/advanced parameters remain connected; only one primary clean-preview action')
        # Voice audition must be independent from choosing the project voice.
        page.locator('[data-tool="voice"]').click()
        icons=page.locator('#essentials [data-listen-voice]')
        assert icons.count()==27
        selected_before=page.evaluate('project.params.voice_id')
        voice_id=icons.nth(1).get_attribute('data-listen-voice')
        play_button=icons.nth(1)
        box=play_button.bounding_box();assert box['width']>=40 and box['height']>=40
        play_button.click()
        page.wait_for_function('!samplePlayer.paused && samplePlayer.currentTime>0',timeout=90000)
        assert page.evaluate('project.params.voice_id')==selected_before
        expect(page.locator('#settingsDialog')).to_be_hidden()
        job_count=len([j for j in state()['jobs'] if j['kind']=='voice_preview'])
        page.locator('#stopVoiceSample').click()
        play_button.click()
        page.wait_for_function('!samplePlayer.paused && samplePlayer.currentTime>0')
        assert len([j for j in state()['jobs'] if j['kind']=='voice_preview'])==job_count
        page.locator('#stopVoiceSample').click()
        check('27 speaker icons: click -> real audio, no modal, no voice selection change; replay reuses sample')
        # Space on the focused speaker must play the speaker, not the video.
        play_button.focus();page.keyboard.press('Space')
        page.wait_for_function('!samplePlayer.paused')
        assert page.locator('#video').evaluate('(v)=>v.paused')
        page.locator('#stopVoiceSample').click()
        page.locator('#essentials .voice-picker input[type=search]').fill('not-a-voice')
        expect(page.locator('#essentials .voice-picker')).to_contain_text('Không có giọng phù hợp')
        assert page.locator('#essentials .voice-row:visible').count()==0
        page.locator('#essentials .voice-picker input[type=search]').fill('')
        page.locator(f'#essentials [data-choose-voice="{voice_id}"]').click()
        assert page.evaluate('project.params.voice_id')==voice_id
        check('Speaker keyboard operation, search empty state and explicit voice selection work')
        # Drafts survive tabs and scopes and do not write the project until Apply.
        page.locator('#stageSettings').click()
        field=page.locator('#settingsFields [data-param="voice_speed"]')
        original=state()['projects'][0]['params']['voice_speed']
        field.fill('1.15');field.press('Tab')
        page.locator('#settingsTabs [data-group="clean"]').click()
        page.locator('#settingsTabs [data-group="voice"]').click()
        expect(field).to_have_value('1.15')
        assert state()['projects'][0]['params']['voice_speed']==original
        page.locator('#settingsScope').select_option('defaults')
        page.locator('#settingsScope').select_option('project')
        expect(field).to_have_value('1.15')
        dialog_answers.append(False)
        page.locator('[data-close="settingsDialog"]').click()
        expect(page.locator('#settingsDialog')).to_be_visible()
        page.locator('#applySettings').click()
        expect(page.locator('#toast')).to_contain_text('Đã lưu cài đặt')
        assert state()['projects'][0]['params']['voice_speed']==1.15
        page.locator('[data-close="settingsDialog"]').click()
        check('Settings drafts survive tabs/scopes; discard can be cancelled; Apply persists requested values')
        # API UI remains reachable. This never contacts the real provider.
        page.locator('#apiSettings').click()
        page.locator('#checkCatalog').click()
        expect(page.locator('#checkResult')).to_contain_text('TEST DOUBLE')
        page.locator('[data-close="settingsDialog"]').click()
        check('API/key shortcut and mocked catalog check remain available')
        page.locator('#settings').click()
        for group in ['keys','voice','translate','extract','clean','audio','general','engines','pipeline']:
            page.locator('#settingsTabs [data-group="'+group+'"]').click()
            expect(page.locator('#settingsTabs [data-group="'+group+'"]')).to_have_attribute('aria-pressed','true')
            if group not in ('keys','engines'):
                expect(page.locator('#applySettings')).to_be_in_viewport()
                page.locator('.settings-body').evaluate('(e)=>e.scrollTop=e.scrollHeight')
                expect(page.locator('#applySettings')).to_be_in_viewport()
                page.locator('.settings-body').evaluate('(e)=>e.scrollTop=0')
            page.screenshot(path=str(OUT/('settings-'+group+'.png')))
        page.locator('[data-close="settingsDialog"]').click()
        check('All nine settings groups inspected in the production dialog')
        page.route('**/api/state',lambda route:route.fulfill(status=503,json={'detail':'Simulated local engine outage'}))
        page.evaluate('refresh()')
        expect(page.locator('#connectionStatus')).to_be_visible()
        page.unroute('**/api/state');page.evaluate('refresh()')
        expect(page.locator('#connectionStatus')).to_be_hidden()
        check('Persistent local-engine failure status appears and clears on recovery')
        page.locator('#rightSplitter').focus();page.keyboard.press('Home')
        before=page.locator('#viewer').bounding_box()['width']
        page.keyboard.press('ArrowRight')
        assert page.locator('#viewer').bounding_box()['width']>before+40
        page.locator('#resetLayout').click()
        check('Viewer divider has keyboard controls; focusable buttons do not hijack playback shortcuts')
        # Real offline pipeline regression, no online translation.
        page.locator('#pipelineSettings').click()
        page.locator('#settingsFields [data-param="pipeline_translate"]').uncheck()
        page.locator('#settingsFields [data-param="pipeline_clean"]').check()
        page.get_by_role('button',name='Bắt đầu quy trình → xuất video',exact=True).click()
        expect(page.locator('#settingsDialog')).to_be_hidden()
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            job=next(j for j in state()['jobs'] if j['kind']=='pipeline')
            if job['state'] in ('done','error','cancelled'):break
            page.wait_for_timeout(300)
        assert job['state']=='done',job
        assert job['result']['steps']==['tts','clean','render']
        assert job['result']['verification']['files_checked']>=4
        check('Revised UI -> real Piper -> FFmpeg blur -> verified MP4 + SRT/ASS')
        page.locator('#closeJobs').click()
        for w,h in [(1920,1080),(1366,768),(1093,614),(800,720)]:
            page.set_viewport_size({'width':w,'height':h})
            page.locator('[data-tool="voice"]').click()
            page.wait_for_timeout(150)
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            footer=page.locator('footer').bounding_box()
            assert footer['y']+footer['height']<=h+1
            nav=page.locator('#stageNavigation').bounding_box()
            assert nav['y']+nav['height']<=footer['y']+1
            page.screenshot(path=str(OUT/f'layout-{w}.png'))
        check('Four viewport sizes: no horizontal overflow, no toast covering navigation/progress')
        assert not errors,errors
        (OUT/'report.json').write_text(json.dumps({'checks':checks,'errors':errors,'surfaces':surfaces,'provider':'mocked','media':'8-second synthetic fixture'},ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception:
        print('JS ERRORS',errors,flush=True)
        page.screenshot(path=str(OUT/'failure.png'));raise
    finally:
        browser.close();req.post('/api/test/shutdown');req.dispose()
