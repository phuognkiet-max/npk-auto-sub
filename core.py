"""Storage and media utilities for the independent VietDub Studio application."""
from __future__ import annotations
import hashlib
from contextlib import closing
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import uuid

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('VIETDUB_DATA_DIR', str(ROOT / 'data'))).resolve()
MODELS = ROOT / "models"
for directory in (DATA, DATA / "jobs", DATA / "frames", MODELS):
    directory.mkdir(parents=True, exist_ok=True)
NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
DEFAULTS = {
    "provider": "gemini", "translation_model": "gemini-3.7-flash",
    "router9_base_url": "http://127.0.0.1:20128/v1",
    "target_language": "Tiếng Việt", "source_language": "auto",
    "style": "Dịch tự nhiên, chính xác; giữ tên riêng và thống nhất cách xưng hô.",
    "glossary": "", "asr_model": "small", "asr_device": "cuda", "asr_compute": "auto",
    "mask_mode": "rectangle", "dilate": 4, "context": 96, "max_side": 768,
    "temporal": True, "method": "lama", "ocr_fps": 2,
    "font_size": 46, "margin": 40, "outline": 3, "font": "Arial",
    "original_gain": 0.2, "voice_gain": 1.0, "bgm_gain": 0.15,
    "voice_speed": 1.0, "bgm_path": "", "voice_path": "",
    "burn_subtitles": True, "use_clean": True, "workload_mode": "quiet",
    "mask_source": "manual",
    "voice_id": "piper:vais1000", "tts_require_translation": True,
    "tts_timing_mode": "natural", "tts_auto_schedule": False, "tts_text_source": "translated", "tts_expressive": False,
    "subtitle_text_cleanup": False, "review_before_render": False,
    "tts_device": "auto", "pronunciation_dictionary": "",
    "voice_normalize": False, "bgm_ducking": False, "subtitle_karaoke": False,
    "subtitle_preset": "clean", "subtitle_bold": False,
    "ocr_confidence": .55, "ocr_similarity": .84,
    # Stored generic preference; translation_quality applies the Vietnamese
    # syllable floor (4.8) without silently changing saved project settings.
    "translation_batch": 20, "words_per_second": 3.6,
    "cover_color": "#000000", "blur_radius": 16, "stretch_height": 12, "stretch_direction": "above",
    "cover_opacity": 1.0, "edge_feather": 0, "stretch_offset": 0, "inpaint_radius": 5,
    "video_encoder": "auto", "video_quality": 20,
    "pipeline_extract": "existing", "pipeline_translate": True, "pipeline_tts": True, "pipeline_clean": False,
    "font_color": "#FFFFFF", "outline_color": "#000000", "subtitle_background": False,
    "subtitle_alignment": "bottom", "subtitle_shadow": 0, "subtitle_margin_x": 40,
    "translation_scope": "all", "export_name": "Video-thanh-pham", "export_directory": "",
    "voice_offset": 0, "extraction_start": 0, "extraction_end": 0,
    "ocr_scale_mode": "native",
    "subtitle_automation": "local",
    "subtitle_line_chars": 42, "subtitle_max_lines": 2, "export_resolution": "source",
    "subtitle_flow": "single", "subtitle_chunk_words": 6,
}
SUBTITLE_STYLE_KEYS = {'font', 'font_size', 'font_color', 'outline_color', 'outline',
    'subtitle_background', 'subtitle_alignment', 'subtitle_shadow', 'subtitle_margin_x', 'margin',
    'subtitle_line_chars', 'subtitle_max_lines', 'subtitle_bold', 'subtitle_flow', 'subtitle_chunk_words'}


def restored_params(params):
    """Add old defaults without changing a pre-selection project's speech.

    Missing source/preset keys are intentionally retained as missing: reading an
    old project or worker snapshot must neither select new text nor invalidate
    a previously valid speech signature or existing custom subtitle style.
    """
    merged = DEFAULTS | {'tts_timing_mode': 'legacy'} | params
    for key in ('tts_text_source', 'subtitle_preset'):
        if key not in params:
            merged.pop(key, None)
    # Existing exports retain their layout until the user selects the new flow.
    if 'subtitle_flow' not in params:
        merged['subtitle_flow'] = 'wrap'
    if 'subtitle_automation' not in params:
        merged['subtitle_automation'] = 'manual'
    return merged

def connect():
    db = sqlite3.connect(DATA / "studio.sqlite3", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))")
    return db

def put(kind, identifier, value):
    with closing(connect()) as db:
        db.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?)", (kind, identifier, json.dumps(value, ensure_ascii=False)))
        db.commit()
    return value


def save_project_version(project, previous=None):
    """Project and bounded revision history are committed in the same transaction."""
    with closing(connect()) as db:
        if previous:
            revision = f'{project["id"]}:{previous["updated"]:.6f}'
            db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)',
                       ('project_revision', revision, json.dumps(previous, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)',
                       ('project_backup', project['id'], json.dumps(previous, ensure_ascii=False)))
            ids = db.execute("SELECT id FROM objects WHERE kind='project_revision' AND id LIKE ? ORDER BY id DESC",
                             (project['id'] + ':%',)).fetchall()
            for (old,) in ids[30:]:
                db.execute("DELETE FROM objects WHERE kind='project_revision' AND id=?", (old,))
        db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)',
                   ('project', project['id'], json.dumps(project, ensure_ascii=False)))
        db.commit()
    return project

def get(kind, identifier):
    with closing(connect()) as db:
        row = db.execute("SELECT value FROM objects WHERE kind=? AND id=?", (kind, identifier)).fetchone()
    return json.loads(row[0]) if row else None

def all_items(kind):
    with closing(connect()) as db:
        rows = db.execute("SELECT value FROM objects WHERE kind=?", (kind,)).fetchall()
    return sorted((json.loads(r[0]) for r in rows), key=lambda x: x.get("updated", x.get("created", 0)), reverse=True)

def settings():
    return DEFAULTS | (get("settings", "main") or {})

def binary(name):
    owned = ROOT / "runtime" / "ffmpeg" / (name + ".exe")
    found = str(owned) if owned.is_file() else shutil.which(name)
    if not found:
        raise RuntimeError(f"Thiếu {name}. Cài FFmpeg hoặc đặt bộ FFmpeg vào runtime/ffmpeg.")
    return found

def run(command, **kwargs):
    result = subprocess.run([str(v) for v in command], capture_output=True, creationflags=NO_WINDOW, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace")[-3500:])
    return result

def probe(path):
    p = Path(path).expanduser().resolve(strict=True)
    if not p.is_file() or p.suffix.lower() not in {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v"}:
        raise ValueError("Chọn một tệp video MP4, MKV, MOV, AVI hoặc WebM.")
    info = json.loads(run([binary("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", p], timeout=30).stdout)
    video = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    if not video:
        raise ValueError("Không tìm thấy luồng hình trong video.")
    numerator, denominator = map(float, video.get("avg_frame_rate", "25/1").split("/"))
    rotation = float(video.get('tags', {}).get('rotate', 0))
    for side_data in video.get('side_data_list', []):
        if 'rotation' in side_data:
            rotation = float(side_data['rotation'])
    width, height = int(video['width']), int(video['height'])
    if round(rotation / 90) % 2:
        width, height = height, width
    duration = float(info.get("format", {}).get("duration", video.get("duration", 0)))
    try:
        video_duration = float(video.get("duration") or duration)
    except (ValueError, TypeError):
        video_duration = duration
    if not math.isfinite(video_duration) or video_duration <= 0:
        video_duration = duration
    audio = next((s for s in info['streams'] if s.get('codec_type') == 'audio'), None)
    audio_duration, audio_start = None, 0.
    if audio:
        try:
            measured = float(audio.get('duration'))
            if math.isfinite(measured) and measured > 0:
                audio_duration = measured
        except (TypeError, ValueError):
            pass
        try:
            # FFmpeg's default input timestamps are relative to the container
            # start, which need not be zero (for example MPEG transport streams).
            audio_start = float(audio.get('start_time') or 0) - float(info.get('format', {}).get('start_time') or 0)
            if not math.isfinite(audio_start):
                audio_start = 0.
        except (TypeError, ValueError):
            audio_start = 0.
    return {"path": str(p), "width": width, "height": height, "rotation": rotation,
            "fps": numerator / denominator if denominator and numerator else 25,
            "duration": duration, "video_duration": video_duration,
            "audio": audio is not None, "audio_duration": audio_duration,
            "audio_start_time": audio_start, "bytes": p.stat().st_size}

def create_project(path, name=""):
    media = probe(path)
    # Workflow changes apply to new projects. Loading an older snapshot must
    # never silently reinterpret its captions, speech cache or export policy.
    params = settings() | {'subtitle_text_cleanup': True, 'review_before_render': True}
    item = {"id": uuid.uuid4().hex, "name": name.strip() or Path(path).stem,
            "media": media, "regions": [], "extraction_regions": [], "segments": [], "params": params,
            "created": time.time(), "updated": time.time(), "artifacts": {}}
    return put("project", item["id"], item)


def prepare_project_text(project, *, stage='source', selected_indices=None):
    """Prepare an isolated draft; the caller commits it as an authored revision.

    The report retains pre-cleaning rows and source-index mapping. No provider,
    voice, model, store write or image processing is performed by this helper.
    """
    import copy
    draft = copy.deepcopy(project)
    rows = draft.get('segments', [])
    if not draft.get('params', {}).get('subtitle_text_cleanup', False):
        return draft, {'version': 1, 'segments': copy.deepcopy(rows), 'changes': [],
                       'source_indices': [[index] for index in range(len(rows))],
                       'input_count': len(rows), 'output_count': len(rows),
                       'needs_review': False, 'review_reasons': []}
    if stage == 'speech':
        # Match worker/preflight selection exactly. Generic fallback to an
        # edited script must not silently override the chosen source.
        from speech_delivery import speech_cleanup_report
        report = speech_cleanup_report(rows, draft['params'], selected_indices=selected_indices)
    else:
        from clean_timing import prepare_subtitle_segments
        report = prepare_subtitle_segments(rows, stage=stage, selected_indices=selected_indices)
    draft['segments'] = report['segments']
    if report['changes']:
        from speech_word_alignment import prune_stale_timings
        prune_stale_timings(draft)
    return draft, report

def safe_owned(path, parent=DATA):
    resolved, root = Path(path).resolve(), parent.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("Đường dẫn nằm ngoài dữ liệu của NPK Auto Sub.")
    return resolved

def signature(project):
    fields = {k: project["params"].get(k, DEFAULTS[k]) for k in ("method", "mask_mode", "mask_source", "dilate", "context", "max_side", "temporal", "cover_opacity", "edge_feather")}
    if fields['method'] in ('box', 'blur', 'stretch'):
        fields.update({k: project['params'].get(k, DEFAULTS[k]) for k in ('cover_color','blur_radius','stretch_height','stretch_direction','stretch_offset')})
    if fields['method'] == 'telea':
        fields['inpaint_radius'] = project['params'].get('inpaint_radius', DEFAULTS['inpaint_radius'])
    quality = float(project['params'].get('video_quality', DEFAULTS['video_quality']))
    if quality != DEFAULTS['video_quality']:
        fields['video_quality'] = quality
    source = Path(project["media"]["path"])
    track = project.get('artifacts', {}).get('scan', {}) if fields['mask_source'] == 'tracked' else {}
    payload = ['npk-clean-v3', str(source), source.stat().st_size, source.stat().st_mtime_ns,
               project["regions"], fields, track.get('job_id'), track.get('tracking_signature'),
               project['params'].get('ocr_fps', 2) if track else None]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

def save_srt(rows, path, translated=True, ass_style=None):
    import pysubs2
    doc = pysubs2.SSAFile()
    event_rows = []
    for row in rows:
        try:
            start, end = float(row['start']), float(row['end'])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if not math.isfinite(start + end) or start < 0 or end <= start:
            continue
        start_ms, end_ms = round(start * 1000), round(end * 1000)
        # ASS stores centiseconds. Do not emit an invisible zero-length cue
        # after format rounding, or extend it past its original bounds.
        if end_ms <= start_ms or (ass_style and (start_ms + 5) // 10 >= (end_ms + 5) // 10):
            continue
        text = row.get("translated") if translated else None
        text = (text or row.get("text", "")).strip()
        if not text:
            continue
        single = False
        if ass_style:
            from subtitle_flow import caption_chunks
            q = ass_style[0] | row.get('subtitle_style', {})
            single = q.get('subtitle_flow', 'wrap') == 'single'
            pages = caption_chunks(row, text, q, ass_style[1])
        else:
            pages = [{'start': start, 'end': end, 'text': text}]
        for page in pages:
            page_start_ms, page_end_ms = round(page['start'] * 1000), round(page['end'] * 1000)
            if (page_end_ms <= page_start_ms or
                    (ass_style and (page_start_ms + 5) // 10 >= (page_end_ms + 5) // 10)):
                continue
            displayed = page['text']
            effects_row = row
            if single:
                # Neutralize ASS escape/control syntax from literal user text.
                # The untouched original text still goes to SRT/project storage.
                displayed = page['display_text']
                effects_row = row | {'start': page['start'], 'end': page['end'],
                                     'caption_words': page['caption_words'],
                                     'word_timing_source': page['word_timing_source'],
                                     'subtitle_style': row.get('subtitle_style', {}) | {'font_size': page['font_size']}}
            caption = displayed.replace("\n", r"\N")
            if ass_style:
                from subtitle_presets import caption_effects
                caption = caption_effects(effects_row, displayed, q)
                if single:
                    caption = r'{\q2}' + caption
            doc.events.append(pysubs2.SSAEvent(start=page_start_ms, end=page_end_ms, text=caption))
            event_rows.append(effects_row)
    if ass_style:
        p, media = ass_style
        doc.info.update(PlayResX=str(media["width"]), PlayResY=str(media["height"]), WrapStyle="0")
        style = doc.styles["Default"]
        style.fontname = p["font"]
        style.fontsize = float(p["font_size"])
        style.bold = bool(p.get('subtitle_bold', False))
        style.outline = float(p["outline"])
        style.shadow = 0
        style.marginv = int(p["margin"])
        style.alignment = {'bottom':2, 'middle':5, 'top':8}.get(p.get('subtitle_alignment'), 2)
        style.marginl = style.marginr = int(p.get('subtitle_margin_x', 40))
        def color(value):
            return pysubs2.Color(*[int(value[i:i+2],16) for i in (1,3,5)])
        style.primarycolor = color(p.get('font_color', '#FFFFFF'))
        if p.get('subtitle_karaoke'):
            style.secondarycolor = color('#8C9DAF')
        style.outlinecolor = color(p.get('outline_color', '#000000'))
        style.backcolor = style.outlinecolor
        style.shadow = float(p.get('subtitle_shadow', 0))
        style.borderstyle = 3 if p.get('subtitle_background') else 1
        for index,(event,row) in enumerate(zip(doc.events,event_rows)):
            overrides=row.get('subtitle_style')
            if not overrides:continue
            q=p|{k:v for k,v in overrides.items() if k in SUBTITLE_STYLE_KEYS}
            own=style.copy()
            own.fontname=q['font'];own.fontsize=float(q['font_size'])
            own.bold=bool(q.get('subtitle_bold', False))
            own.primarycolor=color(q['font_color']);own.outlinecolor=color(q['outline_color'])
            own.backcolor=own.outlinecolor;own.outline=float(q['outline'])
            own.shadow=float(q['subtitle_shadow']);own.borderstyle=3 if q['subtitle_background'] else 1
            own.alignment={'bottom':2,'middle':5,'top':8}[q['subtitle_alignment']]
            own.marginv=int(q['margin']);own.marginl=own.marginr=int(q['subtitle_margin_x'])
            event.style=f'Cue{index+1}';doc.styles[event.style]=own
    doc.save(str(path), encoding="utf-8")
