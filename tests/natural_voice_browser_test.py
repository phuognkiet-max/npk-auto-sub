"""Natural speech controls against an isolated real API and Edge DOM; no inference/API spend."""
import atexit
import json
from pathlib import Path

import httpx
from playwright.sync_api import expect, sync_playwright

BASE = 'http://127.0.0.1:19864'
HEADERS = {'x-studio-token': 'npk-studio-test'}
OUT = Path(__file__).resolve().parent / 'natural-voice-ui-results'
OUT.mkdir(exist_ok=True)
checks, errors = [], []


def stop_server():
    try:
        httpx.post(BASE + '/api/test/stop', headers=HEADERS, json={}, timeout=3)
    except httpx.HTTPError:
        pass


atexit.register(stop_server)
with sync_playwright() as pw:
    browser = pw.chromium.launch(channel='msedge', headless=True)
    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(BASE + '/studio#token=npk-studio-test')
        page.wait_for_load_state('networkidle')
        expect(page.get_by_role('textbox', name='Bản dịch câu 1', exact=True)).to_be_visible()
        # Reconnaissance first: inspect the actual rendered page before choosing controls.
        page.screenshot(path=str(OUT / '00-reconnaissance.png'), full_page=True)
        reconnaissance = page.locator('body').inner_text()[:4500]
        print('INITIAL DOM', reconnaissance[:1200], flush=True)
        identifier = page.evaluate('localStorage.getItem("npk-current")')
        project_url = BASE + '/api/studio/projects/' + identifier

        def current():
            response = page.request.get(project_url, headers=HEADERS)
            assert response.ok, response.text()
            return response.json()

        def saved():
            page.wait_for_function('!dirty && !saving && document.querySelector("#saved").textContent === "Đã lưu"')

        def reload():
            page.reload()
            page.wait_for_load_state('networkidle')
            expect(page.locator('#cueRows .cue')).to_have_count(2)

        def open_delivery(index=0):
            details = page.locator(f'.cue[data-cue="{index}"] details.cue-speech')
            if not details.evaluate('(el) => el.open'):
                details.locator('summary').click()
            return details

        def speech_number(key, index=0):
            return page.locator(f'.cue-speech-number[data-i="{index}"][data-part="{key}"]')

        original = current()
        assert original['params']['tts_timing_mode'] == 'natural', original['params']
        page.locator('[data-area="voice"]').click()
        expect(page.locator('#voiceTimingMode')).to_have_value('natural')
        expect(page.locator('#voiceTimingHint')).to_contain_text('1,15')
        page.screenshot(path=str(OUT / '01-natural-mode.png'), full_page=True)
        checks.append('New projects use natural mode and explain the 1.15x fitting limit')
        page.locator('#editSpokenCues').click()
        open_delivery()
        script = 'Chào bạn, mình bắt đầu nhé!'
        page.get_by_role('textbox', name='Lời để đọc câu 1', exact=True).fill(script)
        speech_number('speech_rate').fill('1.05')
        speech_number('pause_before_ms').fill('120')
        speech_number('pause_after_ms').fill('200')
        saved()
        stored = current()
        assert stored['segments'][0]['spoken_text'] == script
        assert stored['segments'][0]['speech_rate'] == 1.05
        assert stored['segments'][0]['pause_before_ms'] == 120
        assert stored['segments'][0]['pause_after_ms'] == 200
        assert stored['segments'][0]['translated'] == original['segments'][0]['translated']
        assert stored['segments'][0]['text'] == original['segments'][0]['text']
        reload()
        expect(page.get_by_role('textbox', name='Lời để đọc câu 1', exact=True)).to_have_value(script)
        expect(speech_number('speech_rate')).to_have_value('1.05')
        expect(speech_number('pause_before_ms')).to_have_value('120')
        expect(speech_number('pause_after_ms')).to_have_value('200')
        page.screenshot(path=str(OUT / '02-cue-delivery.png'), full_page=True)
        checks.append('Spoken script, per-cue speed and pauses persist independently of subtitle text through reload')

        # Both UI and server reject pauses consuming the whole cue budget.
        speech_number('pause_before_ms').fill('2000')
        expect(speech_number('pause_before_ms')).to_have_attribute('aria-invalid', 'true')
        assert current()['segments'][0]['pause_before_ms'] == 120
        invalid_rows = current()['segments']
        invalid_rows[0].update(pause_before_ms=1000, pause_after_ms=1000)
        response = page.request.put(BASE + '/api/projects/' + identifier, headers=HEADERS,
                                    data={'expected_revision': current()['updated'], 'segments': invalid_rows})
        assert response.status in (400, 422), response.text()
        assert current()['segments'] == stored['segments']
        speech_number('pause_before_ms').fill('120')
        checks.append('Invalid pause budgets are rejected by UI and real API without corrupting stored cues')

        # Do not duplicate a custom spoken script when splitting the subtitle.
        page.locator('.cue-select[data-i="0"]').check()
        page.locator('#video').evaluate('(video) => { video.currentTime = 1.5; }')
        page.locator('#splitCue').click()
        expect(page.locator('#notice')).to_contain_text('lời đọc riêng')
        expect(page.locator('#cueRows .cue')).to_have_count(2)
        assert len(current()['segments']) == 2
        page.locator('.cue-select[data-i="0"]').uncheck()
        checks.append('Splitting a cue with a custom spoken script is safely blocked with actionable guidance')

        page.get_by_role('textbox', name='Lời để đọc câu 1', exact=True).fill('')
        for key in ('speech_rate', 'pause_before_ms', 'pause_after_ms'):
            speech_number(key).fill('')
        saved()
        cleared = current()['segments'][0]
        assert not cleared.get('spoken_text')
        assert 'speech_rate' not in cleared
        assert 'pause_before_ms' not in cleared
        assert 'pause_after_ms' not in cleared
        assert cleared['translated'] == original['segments'][0]['translated']
        checks.append('Clearing overrides restores translated-text/default-speed fallback without changing subtitles')

        page.locator('[data-area="voice"]').click()
        page.locator('#voiceTimingMode').select_option('legacy')
        saved()
        assert current()['params']['tts_timing_mode'] == 'legacy'
        expect(page.locator('#voiceTimingHint')).to_contain_text('Cách cũ')
        page.locator('#voiceTimingMode').select_option('natural')
        saved()
        assert current()['params']['tts_timing_mode'] == 'natural'
        checks.append('Natural/legacy mode changes save through the real API')
        page.locator('#editSpokenCues').click()

        # Stop the scheduler so queued -> done transitions are explicitly observed by poll().
        page.evaluate('clearTimeout(pollTimer)')
        page.wait_for_function('!pollBusy')

        def queued_job():
            response = page.request.post(BASE + '/api/jobs', headers=HEADERS,
                                         data={'kind': 'subtitle_preview', 'project_id': identifier})
            assert response.ok, response.text()
            job = response.json()
            page.evaluate('poll()')
            assert page.evaluate('(id) => jobs.some(j => j.id === id && j.state === "queued")', job['id'])
            return job

        def finish(job):
            response = page.request.post(BASE + '/api/test/recognition-state', headers=HEADERS,
                                         data={'id': job['id'], 'state': 'done', 'progress': 1})
            assert response.ok, response.text()

        end = page.get_by_role('spinbutton', name='Kết thúc câu 1', exact=True)
        job = queued_job()
        end.fill('')
        end.focus()
        finish(job)
        page.evaluate('poll()')
        expect(end).to_have_value('')
        assert end.evaluate('(el) => document.activeElement === el')
        assert page.evaluate('pendingCompletedJob?.id') == job['id']
        assert current()['segments'][0]['end'] == original['segments'][0]['end']
        # Invalid finite numeric drafts are protected too, without saving an invalid cue.
        end.fill('0.1')
        page.evaluate('poll()')
        expect(end).to_have_value('0.1')
        assert current()['segments'][0]['end'] == original['segments'][0]['end']
        page.screenshot(path=str(OUT / '03-focused-draft-protected.png'), full_page=True)
        page.locator('#exportTop').focus()
        page.evaluate('poll()')
        assert float(end.input_value()) == original['segments'][0]['end']
        assert page.evaluate('pendingCompletedJob') is None
        checks.append('Empty/invalid focused time drafts survive job completion; refresh resumes after focus leaves the panel editor')

        # Focus can move into an editor while the project refresh request is already in flight.
        job = queued_job()
        finish(job)
        page.locator('#exportTop').focus()
        intercepted = []

        def focus_during_refresh(route):
            response = route.fetch()
            end.focus()
            end.fill('')
            intercepted.append(True)
            route.fulfill(response=response)

        page.route(project_url, focus_during_refresh)
        page.evaluate('window.__refreshProbe = poll(); undefined')
        page.wait_for_function('!pollBusy')
        page.unroute(project_url, focus_during_refresh)
        assert intercepted
        expect(end).to_have_value('')
        assert end.evaluate('(el) => document.activeElement === el')
        assert page.evaluate('pendingCompletedJob?.id') == job['id']
        page.locator('#exportTop').focus()
        page.evaluate('poll()')
        assert float(end.input_value()) == original['segments'][0]['end']
        checks.append('The post-await guard preserves a draft focused during an in-flight project refresh')

        assert not errors, errors
        report = {'ok': True, 'checks': checks, 'page_errors': errors,
                  'reconnaissance': reconnaissance, 'scope': 'isolated temporary DATA and fake keyring; no workers, inference or provider calls'}
        (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        (OUT / 'failure.png').unlink(missing_ok=True)
        print('PASS', json.dumps(checks, ensure_ascii=False), flush=True)
    except Exception as error:
        page.screenshot(path=str(OUT / 'failure.png'), full_page=True)
        (OUT / 'report.json').write_text(json.dumps({'ok': False, 'checks': checks, 'page_errors': errors,
                                                   'error': str(error)}, ensure_ascii=False, indent=2), encoding='utf-8')
        raise
    finally:
        browser.close()
        stop_server()
