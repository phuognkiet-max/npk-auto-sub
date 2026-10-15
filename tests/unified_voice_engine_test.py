"""Real NPK-owned offline VieNeu acceptance; isolated projects, no companion API."""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import wave
from unittest.mock import patch

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
out = root / 'tests/vieneu-independent-results'
out.mkdir(exist_ok=True)

with tempfile.TemporaryDirectory(prefix='npk-unified-real-', dir=root / 'tests') as isolated:
    os.environ['VIETDUB_DATA_DIR'] = isolated
    from core import create_project, DATA, DEFAULTS
    from voices import provider_status
    from worker import text_to_speech, pipeline
    from job_progress import verify_outputs
    from subtitle_quality import coverage_record
    import numpy as np

    status = provider_status()['vieneu']
    assert status['available'], status
    records = []
    # No HTTP/network requests are needed by the Python adapter. Owned native
    # and G2P processes receive only local files; there is no service endpoint.
    with patch('urllib.request.urlopen', side_effect=AssertionError('Inference must stay offline')), \
         patch('socket.create_connection', side_effect=AssertionError('No companion server is needed')):
        speech_text = ('Xin chào, tôi là giọng đọc của NPK Auto Sub. [nghỉ 1s] '
                       'Bạn có thể tạo giọng từ văn bản hoặc lồng tiếng video ngay trong cùng một ứng dụng.')
        dummy = dict(params=DEFAULTS | {'voice_id':'vieneu:Thùy Dung', 'voice_speed':.9},
                     media={'duration':0}, speech_text=speech_text)
        folder = DATA / 'jobs/standalone'
        folder.mkdir()
        began = time.perf_counter()
        result = text_to_speech(dummy, {'sample_text':speech_text}, folder)
        verified = verify_outputs(result, dummy, {'kind':'text_to_speech'}, folder)
        with wave.open(str(folder / 'speech.wav'), 'rb') as audio:
            assert audio.getframerate() == 48000
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2')
        silence = samples == 0
        boundaries = np.flatnonzero(np.diff(np.r_[False, silence, False].astype('int8')))
        longest_silence = max((boundaries[1::2] - boundaries[::2]), default=0) / 48000
        assert longest_silence >= .999, longest_silence
        records.append(dict(name='standalone Thùy Dung WAV+MP3',
            seconds=round(time.perf_counter()-began, 3), duration=result['duration'],
            sample_rate=result['sample_rate'], explicit_pause_seconds=1,
            measured_longest_exact_silence=longest_silence, verification=verified))
        shutil.copy2(folder / 'speech.wav', out / 'NPK-text-to-speech.wav')
        shutil.copy2(folder / 'speech.mp3', out / 'NPK-text-to-speech.mp3')
        print('PASS standalone', json.dumps(records[-1]), flush=True)

        (DATA / 'published').mkdir()
        project = create_project(str(root / 'tests/fixtures/demo.mp4'), 'NPK own VieNeu video')
        project['params'].update(pipeline_extract='existing', pipeline_translate=False,
            pipeline_tts=True, pipeline_clean=False, use_clean=False, original_gain=0,
            voice_gain=1, video_encoder='cpu', voice_id='vieneu:Hải Đăng',
            tts_timing_mode='natural', export_directory=str(DATA / 'published'))
        project['segments'] = [dict(start=.5, end=7.5, text='Hello. Have a nice day!',
            translated='Xin chào bạn. Chúc bạn một ngày tốt lành!')]
        project['coverage'] = coverage_record(project, 'ocr', 0, 8, sampling_interval=1)
        for name in ('first-video', 'reuse-video', 'pause-cache'):
            if name == 'pause-cache':
                project['segments'][0]['pause_before_ms'] = 200
            folder = DATA / 'jobs' / name
            folder.mkdir()
            began = time.perf_counter()
            result = pipeline(project, {'id':name, 'kind':'pipeline'}, folder)
            verified = verify_outputs(result, project, {'kind':'pipeline'}, folder)
            assert not result.get('needs_review'), result
            if name == 'reuse-video':
                assert result['steps'] == ['render'] and 'tts' in result['reused'], result
            else:
                speech = result['artifacts']['tts']
                assert speech['sample_rate'] == 48000, speech
                assert speech['cache_hits'] == (1 if name == 'pause-cache' else 0), speech
                with wave.open(str(folder / 'voice.wav'), 'rb') as audio:
                    assert audio.getframerate() == 48000 and audio.getnframes() == 8 * 48000
            records.append(dict(name=name, seconds=round(time.perf_counter()-began, 3),
                steps=result['steps'], reused=result['reused'],
                cache_hits=result.get('artifacts',{}).get('tts',{}).get('cache_hits'), verification=verified))
            if name == 'first-video':
                shutil.copy2(folder / 'voice.wav', out / 'NPK-video-Hai-Dang.wav')
                shutil.copy2(folder / 'final.mp4', out / 'NPK-unified-demo.mp4')
            print('PASS', name, json.dumps(records[-1]), flush=True)
    (out / 'acceptance.json').write_text(json.dumps(dict(status=status, tests=records,
        model_source='pnnbao-ump/VieNeu-TTS-v3-Turbo', model_format='GGUF Q8_0',
        engine='audio.cpp 0.9.1 owned by NPK', no_companion_api=True,
        scope='Two voices, manual pause, standalone WAV/MP3 and short video; no auto emotion analysis'),
        ensure_ascii=False, indent=2), encoding='utf-8')
