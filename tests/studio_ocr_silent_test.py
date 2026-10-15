"""Real image-only recognition of the user's reported missing 30-second caption.

Uses an independent 28-36s silent excerpt. No user DB mutation, translation API,
voice engine, model download or cleaning job. Leaves a small inspectable sample.
"""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
out = root / 'tests/studio-ocr-silent-results'
out.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='npk-ocr-silent-', dir=root / 'tests') as isolated:
    os.environ['VIETDUB_DATA_DIR'] = isolated
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    from core import create_project, DEFAULTS, run, binary, save_srt
    from performance import configure_compute
    from worker import ocr, subtitle_preview
    from job_progress import verify_outputs
    import pysubs2

    source = json.loads((root / 'data/jobs/a396bb4189544951bb355495ace72685/input.json').read_text(encoding='utf-8'))['project']['media']['path']
    silent = out / 'source-28-36-no-audio.mp4'
    run([binary('ffmpeg'), '-v', 'error', '-y', '-threads', '1', '-ss', '28', '-i', source,
         '-t', '8', '-map', '0:v:0', '-an', '-c:v', 'libx264', '-preset', 'ultrafast',
         '-crf', '20', '-threads', '1', silent], timeout=60)
    project = create_project(str(silent), 'OCR regression · silent 28–36s excerpt')
    assert project['media']['audio'] is False, 'The recognition test must have no audio stream'
    project['params'] = DEFAULTS | dict(workload_mode='quiet', ocr_fps=2, ocr_scale_mode='native',
        extraction_start=0, extraction_end=8, use_clean=False, font='Tahoma')
    # Broad bottom caption band; independent of the user's clean mask.
    project['extraction_regions'] = [dict(x=79, y=918, w=1770, h=135, start=0, end=8)]
    original_regions = copy.deepcopy(project['regions'])
    configure_compute(project['params'])
    began = time.perf_counter()
    result = ocr(project, {'kind': 'ocr'}, out)
    elapsed = time.perf_counter() - began
    verification = verify_outputs(result, project, {'kind': 'ocr'}, out)
    at_reported_time = [r for r in result['segments'] if r['start'] <= 2.65 < r['end']]
    compact = ''.join(c for r in at_reported_time for c in r['text'] if c.isalnum())
    assert '你们俩睡这房间吧' in compact, (at_reported_time, result)
    assert project['regions'] == original_regions
    assert result['coverage']['analyzed_end'] == 8

    # Real ASS renderer must consume the selected global/per-cue font, not just UI.
    project['segments'] = [dict(start=0, end=8, text='Phông chữ tiếng Việt: Ắ Ề Ữ Ộ',
        translated='Hai bạn ngủ phòng này nhé.', subtitle_style={'font': 'Segoe UI', 'font_size': 48})]
    save_srt(project['segments'], out / 'font-test.ass', ass_style=(project['params'], project['media']))
    ass = pysubs2.load(str(out / 'font-test.ass'))
    assert ass.styles['Default'].fontname == 'Tahoma'
    assert ass.styles[ass.events[0].style].fontname == 'Segoe UI'
    subtitle_preview(project, {'kind': 'subtitle_preview', 'time': 2.65}, out)
    report = dict(seconds=elapsed, source_range=[28, 36], audio_stream=False,
        found_at_source_30_65=at_reported_time, segments=result['segments'], coverage=result['coverage'],
        verification=verification, global_font='Tahoma', cue_font='Segoe UI',
        translation='Manually supplied test sentence; no provider request',
        user_data='Unchanged; original source and clean settings are read-only')
    (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False), flush=True)
