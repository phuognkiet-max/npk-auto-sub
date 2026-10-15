"""Short real Piper/FFmpeg acceptance; isolated data, no network or user media."""
import copy
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import wave

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
out = root / 'tests/natural-voice-engine-results'
out.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory(prefix='npk-natural-voice-', dir=root / 'tests') as isolated:
    os.environ['VIETDUB_DATA_DIR'] = isolated
    from core import create_project, DATA, DEFAULTS
    from performance import configure_compute
    from subtitle_quality import coverage_record
    from worker import pipeline
    from job_progress import verify_outputs
    import numpy as np

    configure_compute(DEFAULTS)
    (DATA / 'published').mkdir()
    project = create_project(str(root / 'tests/fixtures/demo.mp4'), 'Nhịp đọc thử nghiệm')
    project['params'].update(pipeline_extract='existing', pipeline_translate=False,
        pipeline_tts=True, pipeline_clean=False, use_clean=False, original_gain=0,
        voice_gain=1, video_encoder='cpu', font_size=25, margin=26,
        tts_timing_mode='natural', export_directory=str(DATA / 'published'))
    project['segments'] = [
        dict(start=.5, end=3.5, text='Hello there', translated='Xin chào bạn.',
             spoken_text='Xin chào bạn!', speech_rate=.95, pause_before_ms=180, pause_after_ms=220),
        dict(start=4, end=7.8, text='Have a nice day', translated='Chúc bạn một ngày tốt lành.',
             spoken_text='Chúc bạn một ngày vui!', speech_rate=1.05, pause_before_ms=120, pause_after_ms=200),
    ]
    project['coverage'] = coverage_record(project, 'ocr', 0, 8, sampling_interval=1)
    reports = []
    for name in ('first', 'reuse', 'pause-only', 'edit-one'):
        if name == 'pause-only':
            project['segments'][0]['pause_before_ms'] = 360
        if name == 'edit-one':
            project['segments'][1]['spoken_text'] = 'Hẹn gặp lại bạn!'
        folder = DATA / 'jobs' / name
        folder.mkdir()
        began = time.perf_counter()
        result = pipeline(project, {'id': name, 'kind': 'pipeline'}, folder)
        verification = verify_outputs(result, project, {'kind': 'pipeline'}, folder)
        assert result['primary_file'] == 'final.mp4'
        assert not result.get('needs_review'), result
        if name == 'reuse':
            assert result['steps'] == ['render'], result
        else:
            assert result['steps'] == ['tts', 'render'], result
            speech = result['artifacts']['tts']
            expected_hits = {'first': 0, 'pause-only': 2, 'edit-one': 1}[name]
            assert speech['cache_hits'] == expected_hits, speech
            with wave.open(str(folder / 'voice.wav'), 'rb') as audio:
                sample_rate = audio.getframerate()
                samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2')
            assert len(samples) == round(project['media']['duration'] * sample_rate)
            for cue in project['segments']:
                start = round(cue['start'] * sample_rate)
                speech_start = round((cue['start'] + cue['pause_before_ms'] / 1000) * sample_rate)
                speech_end = round((cue['end'] - cue['pause_after_ms'] / 1000) * sample_rate)
                end = round(cue['end'] * sample_rate)
                assert not np.any(samples[start:speech_start]), 'leading pause contains speech'
                assert not np.any(samples[speech_end:end]), 'trailing pause contains speech'
                assert np.max(np.abs(samples[speech_start:speech_end].astype('int32'))) > 100
            subtitle = (folder / 'subtitles.srt').read_text(encoding='utf-8-sig')
            assert 'Chúc bạn một ngày tốt lành.' in subtitle
            assert 'Chúc bạn một ngày vui!' not in subtitle
        record = dict(name=name, steps=result['steps'], reused=result['reused'],
            seconds=round(time.perf_counter() - began, 3),
            cache_hits=result.get('artifacts', {}).get('tts', {}).get('cache_hits'),
            verification=verification)
        reports.append(record)
        print('PASS', name, json.dumps(record, ensure_ascii=False), flush=True)
        if name == 'first':
            for filename in ('voice.wav', 'final.mp4', 'subtitles.srt'):
                shutil.copy2(folder / filename, out / filename)
            (out / 'delivery.json').write_text(json.dumps(copy.deepcopy(project['segments']),
                ensure_ascii=False, indent=2), encoding='utf-8')
    (out / 'report.json').write_text(json.dumps(dict(tests=reports,
        real_engines=['Piper CPU', 'FFmpeg H.264/AAC', 'ASS burn-in', 'full video decode'],
        network='No translation or TTS service calls; installed voice only',
        scope='8-second fixture, timing/cache verification; no claim of automatic emotional speech'),
        ensure_ascii=False, indent=2), encoding='utf-8')
