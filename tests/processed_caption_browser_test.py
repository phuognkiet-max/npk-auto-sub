"""Processed media suppresses captions except an explicit translation preview.

Run with webapp-testing's with_server.py and this script's --serve mode.
The fixture uses temporary DATA, synthetic media and no queue workers or keys.
"""
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
OUT = ROOT / "tests/processed-caption-ui-results"
PORT = 19876
TOKEN = "npk-processed-caption-isolated-fixture"
sys.path.insert(0, str(ROOT))


def serve():
    OUT.mkdir(exist_ok=True)
    cleanup = OUT / "fixture-cleanup.json"
    cleanup.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory(prefix="npk-caption-browser-", dir=ROOT / "tests") as temporary:
        os.environ["VIETDUB_DATA_DIR"] = temporary
        os.environ["VIETDUB_TOKEN"] = TOKEN
        import credentials
        credentials.keyring.get_password = lambda *args: None
        from core import DATA, create_project, put
        import server
        import uvicorn
        project = create_project(str(ROOT / "tests/fixtures/demo.mp4"), "Processed caption fixture")
        # Retain the existing whole-cue fixture; single-line acceptance is separate.
        project["params"].update(font_size=24, pipeline_translate=False, pipeline_tts=False, subtitle_flow="wrap")
        project["segments"] = [
            {"start": .1, "end": 3., "text": "你看他俩多恩爱", "translated": ""},
            {"start": 3., "end": 7.9, "text": "原字幕", "translated": "Phụ đề chỉnh sửa.",
             "spoken_text": "Lời đọc ngắn.", "character_name": "Nhân vật mẫu"},
        ]
        for kind, filename in (("clean", "clean.mp4"), ("render", "final.mp4")):
            job_id = uuid.uuid4().hex
            folder = DATA / "jobs" / job_id
            folder.mkdir()
            shutil.copyfile(ROOT / "tests/fixtures/demo.mp4", folder / filename)
            result = {"files": [filename], **({"primary_file": "final.mp4"} if kind == "render" else {})}
            put("job", job_id, {"id": job_id, "project_id": project["id"], "kind": kind,
                "state": "done", "created": time.time(), "result": result})
            project["artifacts"][kind] = {"job_id": job_id, **result}
        put("project", project["id"], project)
        http = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=PORT,
            access_log=False, log_level="warning", timeout_graceful_shutdown=2))

        @server.app.post("/api/test/stop")
        def stop():
            http.should_exit = True
            return {}

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
    checks, errors, jobs, writes = [], [], [], []
    try:
        with sync_playwright() as pw:
            app = pw.chromium.launch(channel="msedge", headless=True)
            page = app.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("request", lambda request: jobs.append(request.url)
                if request.method == "POST" and request.url.endswith("/api/jobs") else None)
            page.on("request", lambda request: writes.append(request.url)
                if request.method == "PUT" and "/api/projects/" in request.url else None)
            try:
                page.goto(base + "/studio#token=" + TOKEN)
                page.wait_for_load_state("networkidle")
                expect(page.locator("#projectName")).to_have_text("Processed caption fixture")
                page.wait_for_function('document.querySelector("#video").readyState >= 2')

                def seek_to(seconds):
                    page.evaluate("time => seek(time)", seconds)
                    page.wait_for_function('time => Math.abs(document.querySelector("#video").currentTime-time)<.1', arg=seconds)

                def choose(source):
                    page.locator("#viewSource").select_option(source)
                    page.wait_for_function('document.querySelector("#video").readyState >= 2')
                    expect(page.locator("#viewSource")).to_have_value(source)

                seek_to(1.)
                expect(page.locator("#caption")).to_be_visible()
                expect(page.locator("#caption")).to_have_text("你看他俩多恩爱")
                checks.append("Source editing retains the untranslated source subtitle preview")

                choose("clean")
                expect(page.locator("#video")).to_have_attribute("src", __import__("re").compile("/clean.mp4"))
                expect(page.locator("#caption")).to_be_hidden()
                for area in ("voice", "settings", "projects", "clean", "subtitles"):
                    page.locator(f'[data-area="{area}"]').click()
                    expect(page.locator("#viewSource")).to_have_value("clean")
                    expect(page.locator("#caption")).to_be_hidden()
                page.locator("#exportTop").click()
                expect(page.locator("#caption")).to_be_hidden()
                seek_to(4.)
                expect(page.locator("#caption")).to_be_hidden()
                page.screenshot(path=str(OUT / "clean-video-without-repainted-subtitle.png"), full_page=True)
                checks.append("Clean media has no UI captions across all viewer tabs, export and seeks, including translated cues")

                page.locator('[data-area="subtitles"]').click()
                choose("source")
                seek_to(4.)
                expect(page.locator("#caption")).to_be_visible()
                expect(page.locator("#caption")).to_have_text("Phụ đề chỉnh sửa.")
                checks.append("Returning to source restores the translated editing preview")

                choose("final")
                expect(page.locator("#video")).to_have_attribute("src", __import__("re").compile("/final.mp4"))
                expect(page.locator("#caption")).to_be_hidden()
                page.locator('[data-area="voice"]').click()
                expect(page.locator("#caption")).to_be_hidden()
                checks.append("Final media preserves its existing suppression of duplicate UI captions")

                choose("source")
                page.locator('[data-area="clean"]').click()
                expect(page.locator("#caption")).to_be_hidden()
                page.locator('[data-area="subtitles"]').click()
                expect(page.locator("#caption")).to_be_visible()
                choose("final")
                seek_to(4.)
                page.evaluate("async () => await save()")
                writes.clear()
                preview_revision = page.evaluate("P.updated")
                page.locator("#captionPreviewToggle").click()
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                expect(page.locator("#viewSource")).to_have_value("clean")
                expect(page.locator("#captionPreviewToggle")).to_have_attribute("aria-pressed", "true")
                expect(page.locator("#captionPreviewStatus")).to_have_text("Xem trước · Bản dịch")
                expect(page.locator("#caption")).to_have_text("Phụ đề chỉnh sửa.")
                expect(page.locator("#caption")).to_be_visible()
                assert abs(page.locator("#video").evaluate("el => el.currentTime") - 4.) < .1
                assert not writes, writes
                assert page.evaluate("P.updated") == preview_revision
                seek_to(1.)
                expect(page.locator("#caption")).to_be_hidden()
                checks.append("Opt-in preview switches final to clean at the current time without saves and never falls back to Chinese")

                seek_to(4.)
                page.locator("#captionPreviewToggle").click()
                expect(page.locator("#caption")).to_be_hidden()
                expect(page.locator("#captionPreviewStatus")).to_have_text("Video đã xử lý chữ")
                page.locator("#captionPreviewToggle").click()
                page.locator('[data-area="clean"]').click()
                expect(page.locator("#captionPreviewBar")).to_be_hidden()
                expect(page.locator("#caption")).to_be_hidden()
                page.locator('[data-area="subtitles"]').click()
                expect(page.locator("#captionPreviewToggle")).to_have_attribute("aria-pressed", "false")
                expect(page.locator("#caption")).to_be_hidden()
                checks.append("Turning preview off or opening Chữ gốc preserves clean pixels without captions")

                choose("final")
                page.locator('[data-tab="style"]').click()
                page.locator('.param[data-key="font_size"]').fill("64")
                page.locator('.param[data-key="font_size"]').dispatch_event("change")
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                expect(page.locator("#viewSource")).to_have_value("clean")
                expect(page.locator("#caption")).to_have_text("Phụ đề chỉnh sửa.")
                expect(page.locator("#caption")).to_be_visible()
                page.locator('.param[data-key="font_color"]').fill("#ff55cc")
                page.locator('.param[data-key="font_color"]').dispatch_event("change")
                assert page.locator("#caption").evaluate("el => getComputedStyle(el).color") == "rgb(255, 85, 204)"
                page.screenshot(path=str(OUT / "translated-style-editing-preview.png"), full_page=True)
                checks.append("Advanced font size/color changes from final automatically open the clean translation editing preview")

                page.locator('[data-area="setup"]').click()
                choose("final")
                page.locator('[data-setup-preset="highlight"]').click()
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                expect(page.locator("#viewSource")).to_have_value("clean")
                expect(page.locator("#caption")).to_be_visible()
                expect(page.locator(".caption-preview-highlight")).to_have_text("Phụ")
                choose("final")
                expect(page.locator("#caption")).to_be_hidden()
                expect(page.locator("#captionPreviewStatus")).to_have_text("Thành phẩm đã xuất")
                checks.append("Setup preset selection previews on clean; returning to final always suppresses duplicate overlays")

                seek_to(4.)
                page.locator("#play").click()
                page.wait_for_function('!document.querySelector("#video").paused')
                page.locator("#captionPreviewToggle").click()
                page.wait_for_function('document.querySelector("#video").readyState >= 2 && !document.querySelector("#video").paused')
                assert 4. <= page.locator("#video").evaluate("el => el.currentTime") < 5.
                page.locator("#play").click()
                page.wait_for_function('document.querySelector("#video").paused')
                checks.append("Switching a playing final video to editing preview preserves playback and the current position")

                page.evaluate("() => { delete P.artifacts.clean; setMedia('final'); }")
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                seek_to(4.)
                page.locator("#captionPreviewToggle").click()
                page.wait_for_function('document.querySelector("#video").readyState >= 2')
                expect(page.locator("#viewSource")).to_have_value("source")
                expect(page.locator("#caption")).to_have_text("Phụ đề chỉnh sửa.")
                seek_to(1.)
                expect(page.locator("#caption")).to_be_hidden()
                checks.append("A project without a clean artifact previews on source but still refuses untranslated source text")

                page.locator('[data-area="subtitles"]').click()
                page.locator('[data-tab="content"]').click()
                expect(page.locator('[data-cue-speech-effective="1"]')).to_have_text("Bản dịch: Phụ đề chỉnh sửa.")
                expect(page.locator('#cueSpoken1')).to_have_value("Lời đọc ngắn.")
                expect(page.locator('#cueSpoken1')).to_have_attribute("placeholder", "Để trống để dùng Bản dịch khi chọn Lời đã biên tập")
                expect(page.locator('#cueCharacter1')).to_have_value("Nhân vật mẫu")
                page.locator('[data-area="voice"]').click()
                expect(page.locator('#voiceSpeechSourceHint')).to_contain_text("Đọc đầy đủ ô Bản dịch; bỏ qua lời đọc riêng")
                page.locator('[data-key="tts_text_source"]').select_option("edited")
                expect(page.locator('#voiceSpeechSourceHint')).to_contain_text("1 câu sẽ đọc khác phụ đề hiển thị")
                page.locator('[data-key="tts_expressive"]').check()
                page.evaluate("async () => await save()")
                assert page.evaluate("P.params.tts_expressive") is True
                page.locator('[data-area="subtitles"]').click()
                expect(page.locator('[data-cue-speech-effective="1"]')).to_have_text("Lời đã biên tập: Lời đọc ngắn.")
                checks.append("Cue and voice hints follow the strict text-source selection, preserve dormant edited text, and save the opt-in laughter setting")

                page.locator('#cueCharacter1').fill("Tên mới")
                expect(page.locator('[data-cue-character-summary="1"]')).to_contain_text("Tên mới")
                page.evaluate("async () => await save()")
                assert page.evaluate("P.segments[1].character_name") == "Tên mới"
                page.locator('#cueCharacter1').fill("")
                page.evaluate("async () => await save()")
                assert not page.evaluate("'character_name' in P.segments[1]")
                page.locator('#cueCharacter1').fill("Người nói chung")
                page.locator('[data-cue="0"] .cue-speech summary').click()
                page.locator('#cueCharacter0').fill("Người nói chung")
                page.locator('.cue-select[data-i="0"]').check()
                page.locator('.cue-select[data-i="1"]').check()
                page.locator('#mergeCue').click()
                assert page.evaluate("P.segments.length") == 1
                assert page.evaluate("P.segments[0].character_name") == "Người nói chung"
                page.locator('#cueSpoken0').fill("")
                page.locator('.cue-select[data-i="0"]').check()
                seek_to(4.)
                page.locator('#splitCue').click()
                assert page.evaluate("P.segments.map(row=>row.character_name)") == ["Người nói chung", "Người nói chung"]
                page.locator('[data-cue="1"] .cue-speech summary').click()
                page.locator('#cueCharacter1').fill("Người nói khác")
                page.locator('.cue-select[data-i="1"]').check()
                page.locator('#mergeCue').click()
                assert page.evaluate("P.segments.length") == 1
                assert not page.evaluate("'character_name' in P.segments[0]")
                checks.append("Character labels save and clear, survive splitting and same-character merges, and are omitted when merging distinct characters")

                page.locator('[data-area="text_to_speech"]').click()
                page.evaluate("() => { standaloneSpeech.voiceId='vieneu:fixture'; catalog.voices.push({id:'vieneu:fixture',name:'Giọng kiểm thử',provider:'vieneu',installed:true,available:true}); updateTextToSpeechVoices(); }")
                expect(page.locator('#ttsExpressive')).to_be_visible()
                page.locator('#ttsExpressive').check()
                assert page.evaluate("standaloneSpeech.expressive") is True
                assert page.evaluate("localStorage.getItem('npk-tts-expressive')") == "true"
                checks.append("Standalone text narration exposes and remembers the opt-in laughter switch without starting synthesis")
                assert not errors, errors
                assert not jobs, jobs
                checks.append("Source clean workspace behavior remains unchanged; no generation jobs or JavaScript errors")
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
