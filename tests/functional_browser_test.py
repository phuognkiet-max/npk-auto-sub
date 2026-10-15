"""User-visible API/voice/pipeline flows; test server mocks provider only."""
from pathlib import Path
import json
import time
import socket
from playwright.sync_api import sync_playwright, expect

BASE='http://127.0.0.1:18774';TOKEN='npk-editor-test-token'
out=Path(__file__).parent/'functional-results';out.mkdir(exist_ok=True)
checks=[]
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    page=browser.new_page(viewport={'width':1366,'height':768})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog',lambda d:d.accept())
    req=pw.request.new_context(base_url=BASE,extra_http_headers={'x-studio-token':TOKEN})
    def state():return req.get('/api/state').json()
    def done(identifier,timeout=90):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            job=next(j for j in state()['jobs'] if j['id']==identifier)
            if job['state'] in ('done','error','cancelled'):break
            assert job['progress']<1
            page.wait_for_timeout(300)
        assert job['state']=='done',job
        return job
    def check(text):checks.append(text);print('PASS',text,flush=True)
    try:
        page.goto(BASE+'/#token='+TOKEN);page.wait_for_load_state('networkidle')
        page.wait_for_function('document.querySelector("#video").readyState>=2')
        assert page.get_by_role('button',name='API / key',exact=True).count()==1
        page.get_by_role('button',name='Cài đặt',exact=True).click()
        expect(page.locator('#apiKey')).to_be_visible()
        assert page.locator('#settingsTabs button').count()==9
        page.locator('#apiKey').fill('fake-functional-AAAA,fake-functional-BBBB');page.locator('#saveKey').click()
        expect(page.locator('#keyList')).to_contain_text('AAAA')
        expect(page.locator('#apiKey')).to_have_value('')
        page.locator('.key-card').filter(has_text='AAAA').get_by_role('button',name='Dùng key này').click()
        expect(page.locator('#keyStatus')).to_contain_text('AAAA')
        page.locator('#checkCatalog').click();expect(page.locator('#checkResult')).to_contain_text('TEST DOUBLE · danh mục')
        expect(page.locator('#providerModels option')).to_have_count(1)
        page.locator('#checkModel').fill('gemini-test-catalog');page.locator('#checkGeneration').click()
        expect(page.locator('#checkResult')).to_contain_text('TEST DOUBLE · model trả lời')
        page.locator('#useCheckedModel').click();expect(page.locator('#toast')).to_contain_text('Đã chọn model')
        assert state()['projects'][0]['params']['translation_model']=='gemini-test-catalog'
        assert 'fake-functional' not in json.dumps(state())
        page.screenshot(path=str(out/'api-settings.png'))
        check('Visible API tabs; multi-key masked add/select; catalog and paid-probe buttons; apply model (mocked provider)')
        page.locator('#settingsTabs [data-group="voice"]').click()
        voices=page.locator('#settingsFields [data-param="voice_id"]')
        assert voices.locator('option').count()>=25
        choices=voices.locator('option').evaluate_all('(opts)=>opts.map(o=>({id:o.value,name:o.textContent}))')
        page.locator('#settingsFields [data-choose-voice="'+choices[1]['id']+'"]').click()
        page.locator('#applySettings').click();expect(page.locator('#toast')).to_contain_text('Đã lưu cài đặt')
        page.locator('#settingsFields .voice-picker details > summary').click()
        page.locator('#settingsFields [aria-label="Nội dung nghe thử"]').fill('Xin chào bạn.')
        page.locator('#settingsFields [data-listen-voice="'+choices[1]['id']+'"]').click()
        # Job is created via actual controls and processed in a separate worker.
        sample=next(j for j in state()['jobs'] if j['kind']=='voice_preview')
        done(sample['id'])
        expect(page.locator('#sampleStatus')).to_be_visible(timeout=10000)
        page.wait_for_function('document.querySelector("#samplePlayer").readyState>=2')
        assert page.locator('#samplePlayer').evaluate('(a)=>a.duration')>.1
        page.locator('#samplePlayer').evaluate('(a)=>a.play()')
        page.wait_for_function('document.querySelector("#samplePlayer").currentTime>0')
        page.screenshot(path=str(out/'voice-settings.png'))
        check('Installed voice choices, save selected model, real worker synthesis and browser audio playback')
        page.locator('#settingsTabs [data-group="engines"]').click()
        expect(page.locator('#settingsExtra')).to_contain_text('Chưa có · FunASR')
        page.locator('[data-close="settingsDialog"]').click()
        page.locator('[data-tool="voice"]').click()
        expect(page.locator('#parameters [data-param="voice_id"]')).to_have_value(choices[1]['id'])
        page.locator('[data-tool="clean"]').click()
        page.locator('#essentials [data-param="method"]').select_option('blur')
        page.locator('#stageOptions > summary').click()
        expect(page.locator('#parameters [data-param="blur_radius"]')).to_be_visible()
        expect(page.locator('#parameters [data-param="max_side"]')).to_be_hidden()
        page.locator('#essentials [data-param="method"]').select_option('box')
        page.locator('#save').click();expect(page.locator('#saveStatus')).to_have_text('Đã lưu')
        check('Engine availability is honest; light-cover settings only show applicable fields')
        page.locator('#pipelineSettings').click()
        page.locator('#settingsFields [data-param="pipeline_translate"]').uncheck()
        page.locator('#settingsFields [data-param="pipeline_clean"]').check()
        page.get_by_role('button',name='Kiểm tra đầu vào',exact=True).click()
        expect(page.locator('#preflightStatus')).to_contain_text('Đầu vào đạt')
        page.screenshot(path=str(out/'pipeline-settings.png'))
        page.get_by_role('button',name='Bắt đầu quy trình → xuất video',exact=True).click()
        expect(page.locator('#settingsDialog')).not_to_be_visible()
        pipeline=next(j for j in state()['jobs'] if j['kind']=='pipeline')
        result=done(pipeline['id'])
        assert result['result']['steps']==['tts','clean','render']
        assert result['result']['verification']['files_checked']>=4
        page.wait_for_function('(id)=>[...document.querySelector("#viewSource").options].some(o=>o.value===id)',arg=pipeline['id'])
        page.locator('#viewSource').select_option(pipeline['id'])
        page.wait_for_function('document.querySelector("#video").readyState>=2')
        assert abs(page.locator('#video').evaluate('(v)=>v.duration')-8)<.2
        expect(page.locator('#jobSummary')).to_contain_text('100%')
        expect(page.locator('#jobEvents')).to_contain_text('tts')
        page.screenshot(path=str(out/'pipeline-complete.png'))
        check('GUI -> preflight -> real selected-voice TTS -> light cover -> final 8s video -> verified 100% and playback')
        # API can be configured before importing any media.
        page.locator('#apiSettings').click()
        for width,height in [(1920,1080),(1093,614)]:
            page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(150)
            assert page.evaluate('document.body.scrollWidth<=innerWidth')
            assert page.locator('#settingsDialog').bounding_box()['height']<=height
        assert not errors,errors
        (out/'browser-report.json').write_text(json.dumps({'checks':checks,'errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
    except Exception:
        print('ERRORS',errors,'TOAST',page.locator('#toast').inner_text(),flush=True)
        page.screenshot(path=str(out/'functional-failure.png'));raise
    finally:
        browser.close()
        try:
            req.post('/api/test/shutdown')
            for _ in range(30):
                try:
                    with socket.create_connection(('127.0.0.1',18774),timeout=.2):pass
                except OSError:
                    time.sleep(.3);break
                time.sleep(.1)
        finally:req.dispose()
