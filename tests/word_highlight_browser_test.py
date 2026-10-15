"""Real studio word-color preview: matched audio times, gaps and honest fallback.

Run via webapp-testing's with_server.py and this file's --serve mode.
Temporary DATA, no queue worker, keys, synthesis or native application changes.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "tests/word-highlight-ui-results"
PORT = 19877
TOKEN = "npk-word-highlight-isolated-fixture"
sys.path.insert(0, str(ROOT))


def serve():
    OUT.mkdir(exist_ok=True)
    cleanup = OUT / "fixture-cleanup.json"
    cleanup.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="npk-word-highlight-browser-", dir=ROOT / "tests") as temporary:
        os.environ["VIETDUB_DATA_DIR"] = temporary
        os.environ["VIETDUB_TOKEN"] = TOKEN
        import credentials
        credentials.keyring.get_password = lambda *args: None
        from core import create_project, get, put
        import server
        import uvicorn
        project = create_project(str(ROOT / "tests/fixtures/demo.mp4"), "Word highlight fixture")
        project["params"].update(pipeline_translate=False, pipeline_tts=False,
                                 burn_subtitles=True, subtitle_preset="highlight", subtitle_flow="wrap")
        project["segments"] = [{"start": .1, "end": 2.8, "text": "你好朋友", "translated": "Xin chào bạn!"},
                               {"start": 3., "end": 7.9, "text": "中文", "translated": "Phụ đề dài vẫn đầy đủ."}]
        put("project", project["id"], project)
        http = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT,
            access_log=False, log_level="warning", timeout_graceful_shutdown=2))

        @server.app.post("/api/test/stop")
        def stop():
            http.should_exit = True
            return {}

        @server.app.get("/api/test/timing-key")
        def timing_key():
            from speech_word_alignment import caption_timing_key
            return {"key": caption_timing_key(get("project", project["id"]))}

        timer = threading.Timer(120, lambda: setattr(http, "should_exit", True))
        timer.daemon = True
        timer.start()
        try:
            http.run()
        finally:
            timer.cancel()
    cleanup.write_text(json.dumps({"data_removed": not Path(temporary).exists(),
                                  "workers_started": False}), encoding="utf-8")


def browser():
    import httpx
    from playwright.sync_api import sync_playwright, expect
    OUT.mkdir(exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    checks, errors, jobs = [], [], []
    timings = [{"word": "Xin", "start": .2, "end": .6},
               {"word": "chào", "start": .9, "end": 1.4},
               {"word": "bạn!", "start": 1.6, "end": 2.3}]
    try:
        with sync_playwright() as pw:
            app = pw.chromium.launch(channel="msedge", headless=True)
            page = app.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: jobs.append(request.url)
                if request.method == "POST" and request.url.endswith("/api/jobs") else None)
            try:
                page.goto(base + "/studio#token=" + TOKEN)
                page.wait_for_load_state("networkidle")
                expect(page.locator("#projectName")).to_have_text("Word highlight fixture")
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                page.locator('[data-area="setup"]').click()
                page.locator('[data-setup-preset="highlight"]').click()
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                page.evaluate("async () => await save()")
                key = page.request.get(base + "/api/test/timing-key", headers={"x-studio-token": TOKEN}).json()["key"]
                page.evaluate("args => { P.segments[0].caption_words=args.words; P.segments[0].word_timing_source='aligned'; "
                              "P.segments[0].caption_timing_key=args.key; P.segments[0].words=[{word:'中文',start:.1,end:2.8}]; drawCaption(); }", {"key": key, "words": timings})

                def seek_to(seconds):
                    page.evaluate("time => seek(time)", seconds)
                    page.wait_for_function('time => Math.abs(document.querySelector("#video").currentTime-time)<.02', arg=seconds)
                    page.evaluate("drawCaption()")

                for seconds, word in ((.3, "Xin"), (1., "chào"), (1.8, "bạn!")):
                    seek_to(seconds)
                    expect(page.locator("#caption")).to_have_text("Xin chào bạn!")
                    expect(page.locator(".caption-preview-highlight")).to_have_text(word)
                    assert page.locator(".caption-preview-highlight").evaluate("el => getComputedStyle(el).color") == "rgb(255, 216, 77)"
                    expect(page.locator("#captionPreviewStatus")).to_have_text("Xem trước · Căn theo âm thanh")
                    page.screenshot(path=str(OUT / f"highlight-{word.rstrip('!')}.png"))
                checks.append("Highlight follows matched dubbed-word times through all three words, preserving every visible word")
                seek_to(.75)
                expect(page.locator(".caption-preview-highlight")).to_have_count(0)
                expect(page.locator("#caption")).to_have_text("Xin chào bạn!")
                checks.append("A real speech pause removes yellow emphasis instead of keeping the first or last word yellow")

                page.evaluate("() => { P.segments[0].caption_words=P.segments[0].caption_words.map(word=>({...word,word:'khác'})); drawCaption(); }")
                seek_to(.3)
                first = page.locator(".caption-preview-highlight").text_content()
                seek_to(2.5)
                last = page.locator(".caption-preview-highlight").text_content()
                assert first == "Xin" and last == "bạn!", (first, last)
                expect(page.locator("#captionPreviewStatus")).to_have_text("Xem trước · Nhịp từ ước lượng")
                checks.append("Unmatched speech/source-language times fall back to a moving estimate with an explicit estimate badge")

                page.evaluate("timings => { P.segments[0].caption_words=timings; P.segments[0].word_timing_source='estimated'; drawCaption(); }", timings)
                expect(page.locator("#captionPreviewStatus")).to_have_text("Xem trước · Nhịp từ ước lượng")
                page.evaluate("timings => { P.segments[0].caption_words=[]; P.segments[0].words=timings; P.segments[0].word_timing_source='aligned'; drawCaption(); }", timings)
                expect(page.locator("#captionPreviewStatus")).to_have_text("Xem trước · Nhịp từ ước lượng")
                checks.append("Estimated markers remain honest; an empty caption_words field prevents stale source words from being treated as aligned")

                page.locator('[data-setup-preset="karaoke"]').click()
                page.evaluate("async () => await save()")
                page.evaluate("timings => { P.segments[0].caption_words=timings; P.segments[0].word_timing_source='aligned'; drawCaption(); }", timings)
                seek_to(1.15)
                words = page.locator(".caption-preview-karaoke")
                assert words.count() == 3
                progress = words.evaluate_all("items => items.map(el=>parseFloat(el.style.getPropertyValue('--caption-word-progress')))")
                assert progress[0] == 100 and 45 < progress[1] < 55 and progress[2] == 0, progress
                expect(page.locator("#caption")).to_have_text("Xin chào bạn!")
                checks.append("Karaoke keeps completed words filled and sweeps only the currently spoken word")

                page.locator('[data-setup-preset="highlight"]').click()
                page.evaluate("async () => await save()")
                page.evaluate("() => { P.segments[0].caption_words=[{word:'Xin',start:.2,end:.22},"
                              "{word:'chào',start:.22,end:.34},{word:'bạn!',start:.34,end:2.3}];"
                              "P.segments[0].word_timing_source='aligned'; drawCaption(); }")
                seek_to(.15)
                page.evaluate("() => { window.__captionFrames=[]; window.__captionObserver=new MutationObserver(()=>"
                              "__captionFrames.push([...document.querySelectorAll('.caption-preview-highlight')].map(el=>el.textContent).join('')));"
                              "__captionObserver.observe(document.querySelector('#caption'),{childList:true,subtree:true}); document.querySelector('#video').play(); }")
                page.wait_for_function('document.querySelector("#video").currentTime > .55')
                page.evaluate("() => { document.querySelector('#video').pause(); __captionObserver.disconnect(); }")
                frames = page.evaluate("__captionFrames")
                assert "chào" in frames and "bạn!" in frames, frames
                checks.append("During playback the animation loop catches a 120 ms word instead of waiting for sparse timeupdate events")

                page.evaluate("async () => await save()")
                key = page.request.get(base + "/api/test/timing-key", headers={"x-studio-token": TOKEN}).json()["key"]
                page.evaluate("args => { P.segments[0].caption_words=args.words; P.segments[0].word_timing_source='aligned'; "
                              "P.segments[0].caption_timing_key=args.key; change(p=>p.params.voice_speed=1); }", {"key": key, "words": timings})
                page.evaluate("async () => await save()")
                page.locator('[data-area="subtitles"]').click()
                page.locator('[data-area="setup"]').click()
                expect(page.locator("#setupWordTimingHint")).to_contain_text("Căn theo âm thanh:")
                expect(page.locator('[data-setup-preset="highlight"] small')).to_have_text("Đổi màu từ đang đọc · nền tương phản")
                seek_to(1.)
                expect(page.locator("#captionPreviewStatus")).to_have_text("Xem trước · Căn theo âm thanh")
                page.locator("#setupVoiceSpeed").fill("1.25")
                page.locator("#setupVoiceSpeed").dispatch_event("change")
                page.evaluate("async () => await save()")
                assert page.evaluate("P.segments[0].caption_words") == []
                assert page.evaluate("P.segments[0].word_timing_source") == "estimated"
                assert page.evaluate("P.segments[0].words") == timings
                expect(page.locator("#captionPreviewStatus")).to_have_text("Xem trước · Nhịp từ ước lượng")
                checks.append("Changing the voice rate and saving replaces local segments with the server's pruned timing, immediately removing the aligned badge")
                assert not errors, errors
                assert not jobs, jobs
                checks.append("Setup explains timing readiness without generation jobs or JavaScript errors")
            finally:
                app.close()
    finally:
        try:
            httpx.post(base + "/api/test/stop", headers={"x-studio-token": TOKEN}, json={}, timeout=3, trust_env=False)
        except httpx.HTTPError:
            pass
        deadline = time.monotonic() + 5
        while not (OUT / "fixture-cleanup.json").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        (OUT / "report.json").write_text(json.dumps({"checks": checks, "errors": errors,
            "generation_jobs": jobs}, ensure_ascii=False, indent=2), encoding="utf-8")
    assert json.loads((OUT / "fixture-cleanup.json").read_text(encoding="utf-8"))["data_removed"]
    print(json.dumps({"checks": checks, "errors": errors, "generation_jobs": jobs}, ensure_ascii=False))


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else browser()
