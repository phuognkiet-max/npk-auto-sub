"""Bounded offline audio measurements, not emotion or speaker recognition.

Only a 20 ms PCM frame, at most 1,200 waveform bins and per-cue counters are
kept in memory. Rates are conservative editing hints and never change a cue.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile

from core import NO_WINDOW, binary
from performance import ffmpeg_limits
from subtitle_quality import source_signature

SAMPLE_RATE = 16000
FRAME_SAMPLES = 320
WAVEFORM_BINS = 1200
SILENCE_RMS = .01  # -40 dBFS; deliberately described as an energy threshold.


def insights_signature(project):
    value = ['npk-audio-insights-v2', source_signature(project),
             [[r.get('start'),r.get('end'),r.get('text','')] for r in project.get('segments',[])]]
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def _dbfs(amplitude):
    return round(20*math.log10(max(1e-6, amplitude)), 2)


def _units(text):
    # CJK characters and whitespace-separated alphabetic/number tokens are
    # counting units, not a claim to linguistic word/syllable segmentation.
    return len(re.findall(r'[\u3400-\u9fff]|[^\W_]+', str(text), flags=re.UNICODE))


def _atomic_json(path, document):
    handle, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle,'w',encoding='utf-8') as output:
            json.dump(document, output, ensure_ascii=False, allow_nan=False)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def analyze_audio(project, job, folder, emit=None):
    import numpy as np
    emit = emit or (lambda **kwargs: None)
    media, params = project['media'], project['params']
    if not media.get('audio'):
        raise ValueError('Video không có âm thanh để xem năng lượng và nhịp nghỉ.')
    duration = float(media['duration'])
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('Thời lượng âm thanh không hợp lệ.')
    from audio_inputs import source_audio_clock
    source_clock = source_audio_clock(media, refresh=True)
    if source_clock['start'] >= source_clock['end']:
        raise ValueError('Luồng âm thanh nằm ngoài khoảng video; hãy nhập lại nguồn.')
    folder = Path(folder)
    target = folder/'audio-insights.json'
    fingerprint = insights_signature(project)
    if target.is_file():
        try:
            cached = json.loads(target.read_text(encoding='utf-8'))
            if (isinstance(cached, dict) and cached.get('signature') == fingerprint and
                    cached.get('schema_version') == 1 and isinstance(cached.get('waveform'), list) and
                    isinstance(cached.get('cues'), list)):
                return {'type':'analyze_audio','files':[target.name], 'insights':cached,
                        'source_signature':source_signature(project), 'signature':fingerprint, 'reused':True}
        except (OSError, ValueError, TypeError):
            pass
    rows = project.get('segments', [])
    if len(rows) > 10000:
        raise ValueError('Phân tích nhịp hiện hỗ trợ tối đa 10.000 câu mỗi lượt.')
    states, events = [], []
    for index, row in enumerate(rows):
        start, end = float(row['start']), float(row['end'])
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < min(end,duration) and end <= duration+.05):
            raise ValueError(f'Câu {index+1}: thời gian không hợp lệ để phân tích âm thanh.')
        end = min(end, duration)
        states.append({'start':start,'end':end,'energy':0.,'duration':0.,'peak':0.,
                       'silent':0.,'leading':0.,'trailing':0.,'longest':0.,'heard':False})
        events.extend([(start,1,index),(end,0,index)])
    events.sort()
    count = min(WAVEFORM_BINS, max(1, math.ceil(duration/.02)))
    energy = np.zeros(count,dtype=np.float64)
    weights = np.zeros(count,dtype=np.float64)
    peaks = np.zeros(count,dtype=np.float32)
    alignment = 'aresample=async=1:first_pts=0'
    if source_clock['known']:
        # An absent source stream after its measured end is timeline silence.
        # Unknown/legacy spans stay strict so padding cannot hide an early EOF.
        alignment += f',apad=whole_dur={duration}'
    command = [binary('ffmpeg'),'-hide_banner','-loglevel','error','-xerror',
               *ffmpeg_limits(params),'-i',media['path'],'-t',str(duration),'-map','0:a:0',
               '-vn','-sn','-ac','1','-ar',str(SAMPLE_RATE),'-af',alignment,'-f','s16le','pipe:1']
    samples, event_index, active = 0, 0, set()
    emit(stage='audio_analysis',progress=0,message='Đo năng lượng, khoảng lặng và nhịp lời nguồn; không suy đoán cảm xúc.')
    with (folder/'audio-analysis-decoder.log').open('wb') as log:
        process = subprocess.Popen([str(v) for v in command], stdout=subprocess.PIPE,
                                   stderr=log, creationflags=NO_WINDOW)
        try:
            while True:
                raw = process.stdout.read(FRAME_SAMPLES*2)
                if not raw:
                    break
                if len(raw)%2:
                    raise RuntimeError('Âm thanh giải mã thiếu mẫu; chưa công bố phân tích.')
                values = np.frombuffer(raw,dtype='<i2').astype(np.float32)/32768.
                if not values.size:
                    break
                a, b = samples/SAMPLE_RATE, (samples+values.size)/SAMPLE_RATE
                power, peak = float(np.mean(values*values)), float(np.max(np.abs(values)))
                rms = math.sqrt(power)
                # Split across waveform-bin edges so long recordings stay bounded.
                left = a
                while left < min(b,duration)-1e-9:
                    bin_index = min(count-1, int(left/duration*count+1e-9))
                    right = min(b,duration,(bin_index+1)*duration/count)
                    weight = max(0,right-left)
                    energy[bin_index] += power*weight
                    weights[bin_index] += weight
                    peaks[bin_index] = max(peaks[bin_index],peak)
                    left = right
                # Include cues starting within this frame; remove cues that ended
                # before it. Exact overlaps weight the 20 ms energy estimate.
                while event_index < len(events) and events[event_index][0] < b:
                    when, opening, index = events[event_index]
                    if opening:
                        active.add(index)
                    elif when <= a:
                        active.discard(index)
                    event_index += 1
                finished = []
                for index in active:
                    state = states[index]
                    overlap = max(0, min(b,state['end'])-max(a,state['start']))
                    if overlap:
                        state['energy'] += power*overlap
                        state['duration'] += overlap
                        state['peak'] = max(state['peak'],peak)
                        if rms < SILENCE_RMS:
                            state['silent'] += overlap
                            state['trailing'] += overlap
                            if not state['heard']:
                                state['leading'] += overlap
                            state['longest'] = max(state['longest'],state['trailing'])
                        else:
                            state['heard'] = True
                            state['trailing'] = 0.
                    if state['end'] <= b:
                        finished.append(index)
                active.difference_update(finished)
                samples += values.size
                if samples//FRAME_SAMPLES % 50 == 0:
                    emit(stage='audio_analysis',progress=min(.99,b/duration),
                         message=f'Đo âm thanh {b:.1f}/{duration:.1f} giây')
            if process.wait(timeout=30):
                raise RuntimeError('Không giải mã trọn âm thanh. Xem audio-analysis-decoder.log; chưa công bố phân tích.')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
    actual_duration = samples/SAMPLE_RATE
    if not samples or abs(actual_duration-duration) > .35:
        raise ValueError('Âm thanh nguồn không phủ đủ thời lượng video; chưa coi phân tích là đầy đủ.')
    cues = []
    for index,(state,row) in enumerate(zip(states,rows)):
        span = max(.001,state['duration'])
        voiced = max(0,state['duration']-state['silent'])
        units = _units(row.get('text',''))
        pacing = units/max(.05,voiced) if voiced >= .1 and units else None
        rate = round(max(.8,min(1.2,pacing/3.6)),2) if pacing is not None else None
        cues.append({'index':index,'start':state['start'],'end':state['end'], 'source_prosody':{
            'rms_dbfs':_dbfs(math.sqrt(state['energy']/span)), 'peak_dbfs':_dbfs(state['peak']),
            'silence_ratio':round(state['silent']/span,3),
            'pause_before_ms':min(2000,round(state['leading']*1000)),
            'pause_after_ms':min(2000,round(state['trailing']*1000)),
            'longest_pause_ms':round(state['longest']*1000),
            'source_units_per_second':round(pacing,2) if pacing is not None else None,
            'suggested_rate':rate, 'confidence':'heuristic',
            'note':'Nhịp gợi ý từ năng lượng và số token lời gốc; nhạc/ồn ảnh hưởng kết quả. '
                   'Không nhận biết cảm xúc hoặc tách người nói; nghe rồi quyết định.'}})
    waveform = [{'time':round(i*duration/count,3),
                 'rms':round(math.sqrt(float(energy[i])/max(1e-9,float(weights[i]))),5),
                 'peak':round(float(peaks[i]),5)} for i in range(count)]
    insights = {'schema_version':1,'signature':fingerprint,'source_signature':source_signature(project),
                'method':'energy/pacing','sample_rate':SAMPLE_RATE,'frame_ms':20,
                'duration':actual_duration,'timeline_duration':duration,
                'source_audio_start':source_clock['start'], 'source_audio_end':source_clock['end'],
                'source_audio_span_known':source_clock['known'], 'silence_threshold_dbfs':-40,
                'waveform':waveform,'cues':cues,
                'warnings':['Đo trên âm nguồn hỗn hợp: nhạc và tiếng ồn có thể bị tính là lời nói. '
                            'Gợi ý không tự thay tốc độ/nghỉ của câu và không chứng minh cảm xúc.']}
    if source_clock['start'] > .1 or source_clock['end'] < duration - .3:
        insights['warnings'].append('Âm nguồn chỉ có trong khoảng %.2f–%.2f giây. '
            'Phần ngoài luồng được giữ im lặng đúng mốc video, không suy đoán lời nói.' %
            (source_clock['start'], source_clock['end']))
    # Source or cue changes while processing must not publish a stale report.
    if insights_signature(project) != fingerprint:
        raise ValueError('Nguồn đã thay đổi khi phân tích; chưa công bố kết quả cũ.')
    _atomic_json(target,insights)
    emit(stage='audio_analysis',progress=1,message='Đã đo xong nhịp nguồn; nghe kiểm tra trước khi áp dụng gợi ý.')
    return {'type':'analyze_audio','files':[target.name],'insights':insights,
            'source_signature':insights['source_signature'],'signature':fingerprint}
