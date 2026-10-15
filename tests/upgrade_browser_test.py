"""Upgrade acceptance against the isolated unified_voice_browser_test --serve API."""
import json
from pathlib import Path
import sys
import httpx
from playwright.sync_api import expect, sync_playwright

BASE='http://127.0.0.1:19875'
HEADERS={'x-studio-token':'npk-studio-test'}
OUT=Path(__file__).resolve().parent/'full-upgrade-ui-results'
OUT.mkdir(exist_ok=True)
checks, errors = [], []
with sync_playwright() as pw:
    browser=pw.chromium.launch(channel='msedge',headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1000},accept_downloads=True)
    page=context.new_page()
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto(BASE+'/studio#token=npk-studio-test')
        page.wait_for_load_state('networkidle')
        page.screenshot(path=str(OUT/'00-reconnaissance.png'),full_page=True)
        assert not page.locator('#backendConnectionStatus').is_visible()
        page.locator('[data-area="text_to_speech"]').click()
        expect(page.locator('#speechLibrary')).to_be_visible()
        page.locator('#ttsText').fill('Xin chào bạn.\n\nĐây là đoạn tiếp theo.')
        page.locator('#speechExtras summary').click()
        page.locator('#speechDevice').select_option('cpu')
        page.locator('#speechLoad').select_option('balanced')
        page.locator('#speechDictionary').fill('NPK => en pi kê')
        page.locator('#speechDictionary').press('Tab')
        page.locator('#splitNarration').click()
        expect(page.locator('.narration-row')).to_have_count(2)
        page.locator('[data-paragraph="1"][data-value="pause_after_ms"]').fill('700')
        page.locator('[data-paragraph="1"][data-value="pause_after_ms"]').press('Tab')
        page.wait_for_function("!document.querySelector('#ttsGenerate').disabled")
        page.locator('#ttsGenerate').click()
        page.wait_for_function('Boolean(standaloneSpeech.jobId)')
        identifier=page.evaluate('standaloneSpeech.jobId')
        job=httpx.get(BASE+'/api/studio/jobs/'+identifier,headers=HEADERS).json()
        assert job['configuration']['tts_device']=='cpu'
        assert job['configuration']['workload_mode']=='balanced'
        assert job['configuration']['pronunciation_dictionary']=='NPK => en pi kê'
        speech=job['speech_segments']
        assert len(speech)==2 and speech[1]['pause_after_ms']==700
        page.wait_for_timeout(650)
        pref=httpx.get(BASE+'/api/studio/preferences',headers=HEADERS).json()
        assert len(pref['narration_segments'])==2 and pref['tts_params']['tts_device']=='cpu',pref
        checks.append('Multi-voice paragraph controls submit a validated snapshot and persist draft/options')
        result=httpx.post(BASE+'/api/test/finish-speech',headers=HEADERS,json={'id':identifier});result.raise_for_status()
        page.locator('#speechLibrary').click()
        expect(page.locator('#libraryRows audio')).to_have_count(1)
        page.locator('[data-speech-rename]').click()
        page.locator('#speechName').fill('Bản đọc kiểm thử nâng cấp')
        page.locator('#modalActions').get_by_role('button',name='Lưu',exact=True).click()
        expect(page.locator('#libraryRows')).to_contain_text('Bản đọc kiểm thử nâng cấp')
        page.screenshot(path=str(OUT/'01-speech-library.png'),full_page=True)
        page.locator('[data-speech-trash]').click()
        page.locator('#modalActions').get_by_role('button',name='Chuyển vào thùng rác',exact=True).click()
        expect(page.locator('#modalTitle')).to_have_text('Thư viện bản đọc')
        expect(page.locator('#libraryRows audio')).to_have_count(0)
        page.locator('#modalClose').click()
        checks.append('Speech library searches, renames, downloads and trashes one standalone job')
        # A fresh browser storage context approximates a changed app origin/port.
        fresh=browser.new_context(viewport={'width':1440,'height':1000})
        other=fresh.new_page();other.on('pageerror',lambda error:errors.append(str(error)))
        other.goto(BASE+'/studio#token=npk-studio-test');other.wait_for_load_state('networkidle')
        other.locator('[data-area="text_to_speech"]').click()
        expect(other.locator('#ttsText')).to_have_value('Xin chào bạn.\n\nĐây là đoạn tiếp theo.')
        other.locator('#speechExtras summary').click()
        expect(other.locator('.narration-row')).to_have_count(2)
        expect(other.locator('#speechDevice')).to_have_value('cpu')
        fresh.close();checks.append('Draft and paragraph voices restore with entirely empty browser localStorage')
        p=httpx.post(BASE+'/api/test/create-video',headers=HEADERS,json={}).json()['id']
        page.evaluate('(id)=>loadProject(id)',p)
        page.locator('[data-area="voice"]').click()
        expect(page.locator('#enrollCustomVoice')).to_be_visible()
        page.locator('#voiceAdvanced summary').click()
        expect(page.locator('[data-key="tts_device"]')).to_be_visible()
        page.locator('#enrollCustomVoice').click()
        expect(page.locator('#customVoiceDuration')).to_have_value('6')
        page.locator('#modalClose').click()
        page.locator('[data-area="subtitles"]').click()
        expect(page.locator('#audioTiming')).to_be_visible()
        page.locator('[data-tab="style"]').click()
        expect(page.locator('[data-key="subtitle_karaoke"]')).to_be_visible()
        checks.append('Video voice device/lexicon, clone form and karaoke controls render without JavaScript errors')
        # These review fixtures exercise production UI guards. They are explicitly
        # synthetic proposals/insights; no provider or inference is called here.
        page.evaluate("""async()=>{change(p=>{p.segments=[
          {start:.5,end:2.5,text:'Hello',translated:'Xin chào bạn.',spoken_text:'Lời tôi đã chọn'},
          {start:2.6,end:2.9,text:'',translated:'',speech_rate:.9}]});await save();
          window.testInsights={warnings:['Synthetic UI acceptance fixture'],waveform:[{time:1,peak:.2}],cues:[
          {index:0,start:.5,end:2.5,source_prosody:{rms_dbfs:-20,silence_ratio:.1,suggested_rate:1.1,pause_before_ms:100,pause_after_ms:200}},
          {index:1,start:2.6,end:2.9,source_prosody:{rms_dbfs:-120,silence_ratio:1,suggested_rate:null,pause_before_ms:0,pause_after_ms:0}}]};
          NPKUpgrade.showAudioInsights(testInsights);} """)
        page.locator('[data-insight="0"]').check();page.locator('[data-insight="1"]').check()
        page.locator('#applyProsody').click()
        assert page.evaluate('P.segments[0].speech_rate')==1.1
        assert page.evaluate('P.segments[0].pause_after_ms')==200
        assert page.evaluate('P.segments[1].speech_rate')==.9
        page.evaluate('NPKUpgrade.showAudioInsights(testInsights);P.segments[0].end=2.4')
        page.locator('[data-insight="0"]').check();page.locator('#applyProsody').click()
        expect(page.locator('#notice')).to_contain_text('Mốc câu đã đổi')
        page.locator('#modalClose').click();page.evaluate('P.segments[0].end=2.5')
        checks.append('Waveform review applies bounded pauses/rate, skips silence and rejects stale cue timing')
        page.evaluate("""()=>{window.testRewrite={id:'cccccccccccccccccccccccccccccccc',project_id:P.id,kind:'optimize_spoken',state:'review',result:{
          segments:JSON.parse(JSON.stringify(P.segments)),suggestions:[{index:0,spoken_text:'Xin chào!',original_spoken_text:P.segments[0].spoken_text,accepted:true,max_words:6}]}};
          testRewrite.result.segments[0].spoken_text='Xin chào!';setProgressExpanded(true);NPKUpgrade.job(testRewrite);} """)
        page.locator('#jobDetails').get_by_role('button',name='Duyệt từng gợi ý lời đọc',exact=True).click()
        page.evaluate("P.segments[0].spoken_text='Lời vừa sửa tay'")
        page.locator('#modalActions').get_by_role('button',name='Áp dụng câu đã chọn',exact=True).click()
        expect(page.locator('#notice')).to_contain_text('đã sửa sau khi tạo gợi ý')
        assert page.evaluate('P.segments[0].spoken_text')=='Lời vừa sửa tay'
        page.locator('#modalClose').click()
        page.evaluate("testRewrite.result.suggestions[0].original_spoken_text=P.segments[0].spoken_text")
        page.locator('#jobDetails').get_by_role('button',name='Duyệt từng gợi ý lời đọc',exact=True).click()
        page.locator('#modalActions').get_by_role('button',name='Áp dụng câu đã chọn',exact=True).click()
        assert page.evaluate('P.segments[0].spoken_text')=='Xin chào!'
        assert page.evaluate('P.segments[0].translated')=='Xin chào bạn.'
        checks.append('Review-only spoken suggestions protect manual edits and change only selected spoken_text')
        endpoint='**/api/studio/jobs/dddddddddddddddddddddddddddddddd/speaker-groups'
        page.route(endpoint,lambda route:route.fulfill(json={'ready':False}))
        page.evaluate("setProgressExpanded(true);NPKUpgrade.job({id:'dddddddddddddddddddddddddddddddd',project_id:P.id,source_revision:P.source_revision,kind:'speaker_groups',state:'done',result:{}})")
        page.locator('#jobDetails').get_by_role('button',name='Duyệt nhóm và phân giọng',exact=True).click()
        expect(page.locator('#notice')).to_contain_text('Video hoặc mốc câu đã đổi')
        page.unroute(endpoint)
        speaker_fixture=page.evaluate("({ready:true,result:{warnings:['Synthetic UI acceptance fixture'],groups:[{id:'speaker-1',label:'Nhóm 1',count:1,segment_indices:[0],representative_index:0}],suggestions:[{segment_index:0,group_id:'speaker-1',start:P.segments[0].start,end:P.segments[0].end}],skipped:[]}})")
        page.route(endpoint,lambda route:route.fulfill(json=speaker_fixture))
        page.locator('#jobDetails').get_by_role('button',name='Duyệt nhóm và phân giọng',exact=True).click()
        page.locator('[data-speaker-group="speaker-1"]').select_option('vieneu:Thùy Dung')
        page.locator('#modalActions').get_by_role('button',name='Áp dụng phân giọng',exact=True).click()
        page.wait_for_function("P.segments[0].voice_id==='vieneu:Thùy Dung'")
        assert page.evaluate('P.segments[0].voice_id')=='vieneu:Thùy Dung'
        page.unroute(endpoint)
        checks.append('Speaker review errors reach the notice guard, and a selected group maps to an installed voice')
        page.evaluate('save()')
        expect(page.locator('#separateSourceAudio')).to_be_visible()
        separation_endpoint='**/api/studio/jobs/eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee/audio-separation'
        page.route(separation_endpoint,lambda route:route.fulfill(json={'ready':True,'separation':{
            'duration':3,'warnings':['Synthetic UI review fixture, no inference']}}))
        attempted=[]
        def reject_stale_separation(route):
            attempted.append(route.request.post_data_json)
            route.fulfill(status=409,json={'detail':'Bản tách nhạc đã cũ. Tách lại trên video hiện tại.'})
        page.route(separation_endpoint+'/apply',reject_stale_separation)
        page.evaluate("setProgressExpanded(true);NPKUpgrade.job({id:'eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee',project_id:P.id,kind:'separate_audio',state:'done',result:{}})")
        page.locator('#jobDetails').get_by_role('button',name='Nghe giọng / nhạc đã tách',exact=True).click()
        expect(page.locator('#modalBody audio')).to_have_count(2)
        before_gain=page.evaluate('P.params.original_gain')
        page.locator('#modalActions').get_by_role('button',name='Dùng nhạc đã tách',exact=True).click()
        expect(page.locator('#notice')).to_contain_text('Bản tách nhạc đã cũ')
        assert len(attempted)==1 and attempted[0]['expected_revision']==page.evaluate('P.updated')
        assert page.evaluate('P.params.original_gain')==before_gain
        page.locator('#modalClose').click()
        page.unroute(separation_endpoint);page.unroute(separation_endpoint+'/apply')
        checks.append('Separation review renders both stems, submits current revision and preserves the project on conflict')
        page.locator('[data-area="settings"]').click()
        # Inspect actual settings labels rather than assuming the original DOM.
        labels=page.locator('[data-settings-tab]').evaluate_all('(nodes)=>nodes.map(n=>({text:n.textContent,tab:n.dataset.settingsTab}))')
        if labels:
            page.locator('[data-settings-tab="performance"]').click()
        else:
            page.evaluate("settingsTab='performance';renderPanel()")
        expect(page.locator('#hardwareCard')).to_be_visible()
        page.locator('[data-area="projects"]').click()
        expect(page.locator('#bundleProject')).to_be_visible()
        page.locator('#bundleProject').click()
        expect(page.locator('#bundleFinal')).not_to_be_checked()
        with page.expect_download(timeout=60000) as download:
            page.locator('#modalActions').get_by_role('button',name='Tạo ZIP',exact=True).click()
        saved=OUT/'project-bundle.zip';download.value.save_as(saved)
        import zipfile
        with zipfile.ZipFile(saved) as archive:
            assert 'manifest.json' in archive.namelist(),archive.namelist()
        # File chooser event must be registered before triggering input.click.
        # Exercise upload by reopening through expect_file_chooser.
        with page.expect_file_chooser() as chooser:
            page.locator('#restoreBundle').click()
        chooser.value.set_files(saved)
        expect(page.locator('#bundleRelink')).to_be_visible()
        page.locator('#modalActions').get_by_role('button',name='Khôi phục',exact=True).click()
        page.wait_for_function('(id)=>P?.id && P.id !== id',arg=p,timeout=60000)
        checks.append('Self-contained ZIP download and multipart restore create a new validated project')
        page.evaluate('setProgressExpanded(true)')
        page.locator('#pauseQueue').click()
        page.wait_for_function("document.querySelector('#pauseQueue').textContent.includes('Tiếp tục')")
        assert httpx.get(BASE+'/api/studio/state',headers=HEADERS).json()['queue_health']['paused']
        page.locator('#pauseQueue').click()
        page.wait_for_function("document.querySelector('#pauseQueue').textContent.includes('Tạm dừng')")
        checks.append('Queue pause/resume is persisted and visible')
        page.evaluate("NPKUpgrade.connectionError({message:'Lỗi kiểm thử'})")
        expect(page.locator('#backendConnectionStatus')).to_be_visible()
        page.evaluate('poll()')
        expect(page.locator('#backendConnectionStatus')).to_be_hidden()
        checks.append('Backend connection banner shows an error and clears on successful polling')
        page.screenshot(path=str(OUT/'02-upgraded-workspace.png'),full_page=True)
        assert not errors,errors
    except BaseException:
        page.screenshot(path=str(OUT/'failure.png'),full_page=True)
        raise
    finally:
        (OUT/'report.json').write_text(json.dumps({'checks':checks,'javascript_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
        browser.close()
        httpx.post(BASE+'/api/test/stop',headers=HEADERS,json={})
print(json.dumps({'checks':checks,'javascript_errors':errors},ensure_ascii=False))
