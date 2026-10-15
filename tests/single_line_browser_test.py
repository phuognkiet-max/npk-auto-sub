"""One-line display acceptance against a temporary real API and headless Edge.

No user data, queue worker, synthesis, inference models, GPU or provider calls.
Run through webapp-testing's with_server.py using this file's --serve mode.
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/single-line-browser"
PORT = 19881
TOKEN = "npk-single-line-isolated-fixture"
TEXT = "Á! Đau, đau quá! Em cũng có mặt mà! Sao mẹ chỉ đánh mỗi con? Xem hai đứa yêu nhau chưa kìa!"
sys.path.insert(0, str(ROOT))


def serve():
    OUT.mkdir(parents=True, exist_ok=True)
    cleanup = OUT / "fixture-cleanup.json"
    cleanup.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="npk-single-line-browser-", dir=ROOT / "tests") as temporary:
        os.environ["VIETDUB_DATA_DIR"] = temporary
        os.environ["VIETDUB_TOKEN"] = TOKEN
        import credentials
        credentials.keyring.get_password = lambda *args: None
        from core import create_project, get, put
        from speech_word_alignment import caption_timing_key
        import server
        import uvicorn
        project = create_project(str(ROOT / "tests/fixtures/demo.mp4"), "Single line fixture")
        project["params"].update(use_clean=False, pipeline_clean=False,
            pipeline_translate=False, pipeline_tts=False, pipeline_extract="existing",
            voice_gain=0, original_gain=0, burn_subtitles=True, subtitle_preset="clean",
            font_size=32, subtitle_flow="single", subtitle_chunk_words=6)
        timings = [{"word": word, "start": round(.2 + i * .21, 3),
                    "end": round(.36 + i * .21, 3)} for i, word in enumerate(TEXT.split())]
        project["segments"] = [{"start": .1, "end": 7.9, "text": "中文原文", "translated": TEXT,
            "caption_words": timings, "word_timing_source": "aligned"}]
        project["segments"][0]["caption_timing_key"] = caption_timing_key(project)
        put("project", project["id"], project)
        http = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT,
            access_log=False, log_level="warning", timeout_graceful_shutdown=2))
        latency = {"layout": 0.}

        @server.app.middleware("http")
        async def delay_layout(request, call_next):
            delay = latency["layout"] if request.url.path.endswith("/caption-layout") else 0
            response = await call_next(request)
            if delay:
                await asyncio.sleep(delay)
            return response

        @server.app.post("/api/test/layout-delay")
        def layout_delay(body: dict):
            latency["layout"] = float(body["seconds"])
            return {}

        @server.app.get("/api/test/info")
        def info():
            return get("project", project["id"])

        @server.app.get("/api/test/timing-key")
        def timing_key():
            return {"key": caption_timing_key(get("project", project["id"]))}

        @server.app.post("/api/test/stop")
        def stop():
            http.should_exit = True
            return {}

        timer = threading.Timer(180, lambda: setattr(http, "should_exit", True))
        timer.daemon = True
        timer.start()
        try:
            http.run()
        finally:
            timer.cancel()
    cleanup.write_text(json.dumps({"data_removed": not Path(temporary).exists(),
        "queue_workers_started": False, "inference_models_loaded": False,
        "gpu_work": False}), encoding="utf-8")


def browser():
    import httpx
    from playwright.sync_api import sync_playwright, expect
    OUT.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    headers = {"x-studio-token": TOKEN}
    checks, errors, jobs, requests = [], [], [], []
    try:
        with sync_playwright() as pw:
            app = pw.chromium.launch(channel="msedge", headless=True)
            page = app.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: jobs.append(request.url)
                if request.method == "POST" and request.url.endswith("/api/jobs") else
                requests.append(request.post_data_json) if request.method == "POST" and
                request.url.endswith("/caption-layout") else None)
            try:
                page.goto(base + "/studio#token=" + TOKEN)
                page.wait_for_load_state("networkidle")
                expect(page.locator("#projectName")).to_have_text("Single line fixture")
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                page.locator('[data-area="setup"]').click()
                expect(page.locator("#setupCaptionFlow")).to_have_value("single")
                expect(page.locator("#setupChunkWords")).to_have_value("6")
                checks.append("New one-line controls are visible in guided setup with six words per phrase")

                def plan():
                    page.evaluate("() => { previewStyle(); requestCaptionLayout(); }")
                    page.wait_for_function("captionLayoutCache.plan && captionLayoutCache.key === captionLayoutFingerprint()")
                    return page.evaluate("captionLayoutCache.plan")

                def seek_to(seconds):
                    page.evaluate("time => { document.querySelector('#video').pause(); seek(time); }", seconds)
                    page.wait_for_function("time => Math.abs(document.querySelector('#video').currentTime-time)<.02", arg=seconds)
                    page.evaluate("drawCaption()")

                original = page.request.get(base + "/api/test/info", headers=headers).json()
                draft = {"expected_revision": original["updated"], "params": {"subtitle_flow": "single", "subtitle_chunk_words": 4},
                         "segments": original["segments"]}
                direct = page.request.post(base + f"/api/studio/projects/{original['id']}/caption-layout", headers=headers, data=draft)
                assert direct.status == 200, direct.text()
                assert " ".join(chunk["text"] for chunk in direct.json()["cues"][0]["chunks"]) == TEXT
                assert page.request.get(base + "/api/test/info", headers=headers).json() == original
                draft["expected_revision"] = -1
                assert page.request.post(base + f"/api/studio/projects/{original['id']}/caption-layout", headers=headers, data=draft).status == 409
                for invalid in (1, 13, 3.5):
                    draft["expected_revision"] = original["updated"]
                    draft["params"]["subtitle_chunk_words"] = invalid
                    assert page.request.post(base + f"/api/studio/projects/{original['id']}/caption-layout", headers=headers, data=draft).status == 400
                checks.append("Draft layout preserves every word without database changes and rejects stale revisions or invalid phrase limits")

                page.locator("#setupChunkWords").fill("13")
                page.locator("#setupChunkWords").dispatch_event("change")
                expect(page.locator("#setupChunkWords")).to_have_value("6")
                assert page.evaluate("P.params.subtitle_chunk_words") == 6
                page.locator("#setupChunkWords").fill("4")
                page.locator("#setupChunkWords").dispatch_event("change")
                page.evaluate("async () => await save()")
                checks.append("Setup rejects an invalid limit locally and saves a valid four-word phrase limit")

                # Install the known-transcript fixture after all initialization
                # and validation have settled, just as completed TTS does.
                key = page.request.get(base + "/api/test/timing-key", headers=headers).json()["key"]
                timings = [{"word": word, "start": round(.2 + i * .21, 3),
                            "end": round(.36 + i * .21, 3)} for i, word in enumerate(TEXT.split())]
                page.evaluate("args => change(p=>Object.assign(p.segments[0],{caption_words:args.words,word_timing_source:'aligned',caption_timing_key:args.key}))", {"key": key, "words": timings})
                page.evaluate("async () => await save()")
                assert page.evaluate("P.segments[0].word_timing_source") == "aligned"

                preset_pages = {}
                for preset in ("clean", "pop", "highlight", "karaoke"):
                    page.locator(f'[data-setup-preset="{preset}"]').click()
                    page.evaluate("async () => await save()")
                    exact = plan()
                    chunks = exact["cues"][0]["chunks"]
                    assert exact["cues"][0]["source"] == "aligned"
                    preset_pages[preset] = chunks
                    assert len(chunks) >= 3
                    assert " ".join(chunk["text"] for chunk in chunks) == TEXT
                    for chunk in chunks:
                        instant = max(chunk["start"] + .02, chunk["caption_words"][0]["start"] + .03)
                        seek_to(instant)
                        expect(page.locator("#caption")).to_have_text(chunk["text"])
                        assert page.locator("#caption").get_attribute("data-page-start") == str(chunk["word_start"])
                        assert page.locator("#caption").evaluate("el=>getComputedStyle(el).whiteSpace") == "nowrap"
                        if preset == "highlight":
                            expect(page.locator(".caption-preview-highlight")).to_have_text(chunk["caption_words"][0]["word"])
                        elif preset == "karaoke":
                            assert page.locator(".caption-preview-karaoke").count() == len(chunk["caption_words"])
                    seek_to(chunks[1]["start"] + .04)
                    page.screenshot(path=str(OUT / f"{preset}-page-2.png"))
                    checks.append(f"{preset.title()} preview matches every exact export page during paused seeks, retaining the complete cue")

                    # Observe real playback around a phrase transition. The same
                    # listener must advance plain Clean/Pop as well as word effects.
                    seek_to(chunks[0]["start"] + .02)
                    count_before = len(requests)
                    page.evaluate("() => { window.__captionTexts=[]; window.__captionObserver=new MutationObserver(()=>__captionTexts.push(document.querySelector('#caption').textContent)); __captionObserver.observe(document.querySelector('#caption'),{childList:true,subtree:true}); document.querySelector('#video').play(); }")
                    page.wait_for_function("end => document.querySelector('#video').currentTime > end", arg=chunks[1]["start"] + .1)
                    page.evaluate("() => { document.querySelector('#video').pause(); __captionObserver.disconnect(); }")
                    assert chunks[1]["text"] in page.evaluate("__captionTexts"), (preset, page.evaluate("__captionTexts"))
                    assert len(requests) == count_before, (preset, len(requests), count_before)
                    checks.append(f"{preset.title()} playback advances phrases without posting a layout request per frame")

                    if preset == "highlight":
                        seek_to(timings[0]["end"] + .02)
                        expect(page.locator(".caption-preview-highlight")).to_have_count(0)
                        expect(page.locator("#captionPreviewStatus")).to_contain_text("Căn theo âm thanh")
                        checks.append("Measured one-line Highlight removes emphasis during acoustic pauses and clearly reports aligned timing")

                page.set_viewport_size({"width": 900, "height": 780})
                seek_to(preset_pages["karaoke"][1]["start"] + .04)
                geometry = page.locator("#caption").evaluate("el => {const r=document.createRange();r.selectNodeContents(el); const b=r.getBoundingClientRect();return {height:b.height,font:parseFloat(getComputedStyle(el).fontSize),width:b.width,available:el.clientWidth};}")
                assert geometry["height"] <= geometry["font"] * 1.6, geometry
                assert geometry["width"] <= geometry["available"], geometry
                checks.append("Resizing the viewer preserves one line and scales measured caption geometry within the frame")
                page.set_viewport_size({"width": 1440, "height": 1000})

                page.locator("#setupAdvancedStyle").click()
                expect(page.locator('#subBody .param[data-key="subtitle_flow"]')).to_have_value("single")
                expect(page.locator('#subBody .param[data-key="subtitle_chunk_words"]')).to_have_value("4")
                page.locator('#subBody .param[data-key="subtitle_flow"]').select_option("wrap")
                expect(page.locator('#subBody .param[data-key="subtitle_chunk_words"]')).to_be_hidden()
                page.locator('#subBody .param[data-key="subtitle_flow"]').select_option("single")
                expect(page.locator('#subBody .param[data-key="subtitle_chunk_words"]')).to_be_visible()
                checks.append("Advanced style exposes matching controls and hides phrase limit for legacy whole-cue wrapping")

                page.evaluate("() => {change(p=>{p.params.subtitle_flow='wrap';p.segments[0].subtitle_style={subtitle_flow:'single',subtitle_chunk_words:3};});previewStyle();}")
                page.evaluate("async () => await save()")
                chunks = plan()["cues"][0]["chunks"]
                assert all(chunk["word_end"] - chunk["word_start"] <= 3 for chunk in chunks)
                seek_to(chunks[1]["start"] + .04)
                expect(page.locator("#caption")).to_have_text(chunks[1]["text"])
                assert page.locator("#caption").get_attribute("data-flow") == "single"
                assert page.request.get(base + "/api/test/info", headers=headers).json()["segments"][0]["translated"] == TEXT
                checks.append("A per-cue one-line override works over global wrapping without shortening saved subtitle text")

                page.request.post(base + "/api/test/layout-delay", headers=headers, data={"seconds": .6})
                page.evaluate("() => {change(p=>p.segments[0].subtitle_style.subtitle_chunk_words=2);previewStyle();setTimeout(()=>{change(p=>p.segments[0].subtitle_style.subtitle_chunk_words=5);previewStyle();},200);}")
                page.wait_for_function("captionLayoutCache.plan && captionLayoutCache.key === captionLayoutFingerprint() && P.segments[0].subtitle_style.subtitle_chunk_words === 5")
                assert all(chunk["word_end"] - chunk["word_start"] <= 5 for chunk in plan()["cues"][0]["chunks"])
                page.request.post(base + "/api/test/layout-delay", headers=headers, data={"seconds": 0})
                page.evaluate("async () => await save()")
                checks.append("Delayed obsolete layout replies cannot replace the current edited phrase plan")

                page.evaluate("() => change(p=>p.params.voice_speed=1.25)")
                page.evaluate("async () => await save()")
                assert page.evaluate("P.segments[0].caption_words") == []
                exact = plan()
                assert exact["cues"][0]["source"] == "estimated"
                seek_to(exact["cues"][0]["chunks"][0]["start"] + .1)
                expect(page.locator("#captionPreviewStatus")).to_contain_text("ước lượng")
                checks.append("Saving changed voice speed prunes stale acoustic timings and visibly labels the estimated page cadence")
                assert not errors, errors
                assert not jobs, jobs
                checks.append("No JavaScript errors or generation jobs; isolated data is cleaned after execution")
            except Exception:
                page.screenshot(path=str(OUT / "failure.png"), full_page=True)
                (OUT / "failure-dom.html").write_text(page.content(), encoding="utf-8")
                raise
            finally:
                app.close()
    finally:
        try:
            httpx.post(base + "/api/test/stop", headers=headers, json={}, timeout=3, trust_env=False)
        except httpx.HTTPError:
            pass
        deadline = time.monotonic() + 5
        while not (OUT / "fixture-cleanup.json").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        report = {"checks": checks, "errors": errors, "generation_jobs": jobs,
                  "layout_requests": len(requests)}
        (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert json.loads((OUT / "fixture-cleanup.json").read_text(encoding="utf-8"))["data_removed"]
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    serve() if "--serve" in sys.argv else browser()
