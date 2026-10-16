"""Small, bounded job events and verified output contracts for the desktop editor."""
import math
import time
from pathlib import Path


def accept_event(current, event):
    """A worker cannot mark the server's running job complete through a progress event."""
    if current.get('state') != 'running':
        return current
    if 'progress' in event:
        try:
            value = float(event['progress'])
            if math.isfinite(value):
                current['progress'] = min(.99, max(0, value))
        except (ValueError, TypeError):
            pass
    stage = str(event.get('stage', current.get('stage', 'process')))[:80]
    terminal_event = stage == 'done' or 'result' in event
    if terminal_event:
        stage = 'verify'
    if stage != current.get('stage'):
        for key in ('completed', 'total', 'fps', 'elapsed_seconds', 'unit'):
            current.pop(key, None)
    current['stage'] = stage
    if 'message' in event:
        current['message'] = str(event['message'])[:1500]
    if terminal_event:
        current['message'] = 'Đã kiểm tra đầu ra; đang chờ worker kết thúc và lưu kết quả.'
    for key in ('completed', 'total', 'fps', 'elapsed_seconds'):
        if key in event:
            try:
                value = float(event[key])
                if math.isfinite(value) and value >= 0:
                    current[key] = value
            except (ValueError, TypeError):
                pass
    if 'unit' in event:
        current['unit'] = str(event['unit'])[:32]
    usage = event.get('api_usage')
    if isinstance(usage, dict):
        counts = ('request_attempts', 'request_limit', 'baseline_requests', 'responses_received',
                  'usage_reported_requests', 'usage_unknown_requests', 'completed_cues',
                  'lineage_request_attempts', 'lineage_request_limit', 'lineage_remaining_requests',
                  'history_unknown_jobs', 'reused_cues', 'newly_completed_cues', 'remaining_cues',
                  'response_model_count', 'response_model_reported_requests',
                  'response_model_missing_requests', 'model_mismatch_requests')
        current['api_usage'] = {key: usage[key] for key in counts
                                if type(usage.get(key)) is int and usage[key] >= 0}
        from provider_usage import _model_id
        for key in ('configured_model', 'response_model'):
            if _model_id(usage.get(key)):
                current['api_usage'][key] = usage[key]
        if isinstance(usage.get('response_models'), list):
            current['api_usage']['response_models'] = list(dict.fromkeys(
                value for value in usage['response_models'][:100] if _model_id(value)))
        tokens = usage.get('reported_tokens')
        if isinstance(tokens, dict):
            current['api_usage']['reported_tokens'] = {key: tokens[key] for key in
                ('input_tokens', 'output_tokens', 'total_tokens', 'thinking_tokens', 'cached_input_tokens')
                if type(tokens.get(key)) is int and tokens[key] >= 0}
        current['api_usage']['cost_verification'] = 'unverified'
    if str(event.get('failure_code','')).startswith('provider_'):
        for key in ('failure_code','failure_stage','next_action'):
            current[key] = str(event.get(key,''))[:80]
        if type(event.get('http_status')) is int:
            current['http_status'] = event['http_status']
        if event.get('quota_scope') in ('daily', 'requests_per_minute', 'tokens_per_minute', 'unknown'):
            current['quota_scope'] = event['quota_scope']
        delay = event.get('retry_after')
        if type(delay) in (int, float) and math.isfinite(delay) and delay >= 0:
            current['retry_after'] = delay
    if event.get('failure_code') == 'speech_timing_constraint':
        current['failure_code'] = 'speech_timing_constraint'
        current['next_action'] = 'edit_speech_timing'
        report = event.get('delivery')
        if isinstance(report, dict):
            current['delivery_summary'] = {key: report.get(key) for key in
                ('schema_version', 'cue_count', 'measured_count', 'unknown_count', 'constraint_count', 'status')}
    history = current.setdefault('events', [])
    # Keep milestones, not a log row for each frame. Last row of a phase is updated.
    row = {'stage': stage, 'message': current.get('message', ''), 'at': time.time()}
    if history and history[-1]['stage'] == stage:
        history[-1] = row
    else:
        history.append(row)
    current['events'] = history[-40:]
    return current


def _verify_separation(result, project, base):
    """Verify native float32 stems without treating a silent stem as failed TTS."""
    import json
    import struct
    import numpy as np
    from audio_separation import MODEL_SHA256, SAMPLE_RATE, separation_signature
    report_path = (base / 'audio-separation.json').resolve()
    if report_path.parent != base or not report_path.is_file() or report_path.stat().st_size > 1024 * 1024:
        raise ValueError('Thiếu báo cáo tách giọng/nhạc hợp lệ.')
    try:
        report = json.loads(report_path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValueError('Không đọc được báo cáo tách giọng/nhạc.') from exc
    if not isinstance(report, dict) or report.get('schema_version') != 1 or \
            report.get('signature') != separation_signature(project) or report.get('model_sha256') != MODEL_SHA256:
        raise ValueError('Báo cáo tách giọng/nhạc không khớp nguồn hoặc model.')
    frames = report.get('frames')
    if type(frames) is not int or frames <= 0 or report.get('sample_rate') != SAMPLE_RATE:
        raise ValueError('Báo cáo stem chưa có số mẫu hoặc tần số hợp lệ.')
    duration = frames / SAMPLE_RATE
    if not math.isfinite(float(report.get('duration', 0))) or \
            abs(float(report.get('duration', 0)) - duration) > 1 / SAMPLE_RATE or \
            abs(duration - float(project['media']['duration'])) > .35:
        raise ValueError('Thời lượng stem không khớp nguồn và báo cáo.')
    metadata = result.get('separation', {})
    if not isinstance(metadata, dict) or any(metadata.get(key) != report.get(key)
            for key in ('signature', 'model_sha256', 'frames', 'sample_rate', 'duration')):
        raise ValueError('Metadata tác vụ không khớp báo cáo tách giọng/nhạc.')
    verified = {}
    for name in ('instrumentals.wav', 'vocals.wav'):
        path = (base / name).resolve()
        if path.parent != base or not path.is_file() or path.stat().st_size != 44 + frames * 8:
            raise ValueError('Stem thiếu dữ liệu hoặc không khớp số mẫu đã báo cáo.')
        with path.open('rb') as stream:
            header = struct.unpack('<4sI4s4sIHHIIHH4sI', stream.read(44))
            if header != (b'RIFF', 36 + frames * 8, b'WAVE', b'fmt ', 16, 3, 2,
                          SAMPLE_RATE, SAMPLE_RATE * 8, 8, 32, b'data', frames * 8):
                raise ValueError('Stem cần WAV float32 stereo 44,1 kHz đúng định dạng.')
            while block := stream.read(SAMPLE_RATE * 8 * 10):
                samples = np.frombuffer(block, dtype='<f4')
                if not np.isfinite(samples).all() or float(np.max(np.abs(samples))) > 8:
                    raise ValueError('Stem có mẫu NaN/Inf hoặc biên độ bất thường; chưa công bố hoàn tất.')
        verified[name] = frames
    return verified


def verify_outputs(result, project, job, folder):
    """Fail before publication if an output is missing, out of scope or unreadable."""
    from core import probe
    base = Path(folder).resolve()
    files = result.get('files')
    decoded_videos = {}
    verified_audio = {}
    if not isinstance(files, list) or not all(isinstance(name, str) for name in files):
        raise ValueError('Worker không cung cấp danh sách kết quả hợp lệ.')
    required = {'clean': {'clean.mp4'}, 'clip': {'clean.mp4'},
                'render': {'final.mp4'}, 'pipeline': {'final.mp4'}, 'tts': {'voice.wav'}, 'voice_preview': {'sample.wav'},
                'text_to_speech': {'speech.wav', 'speech.mp3'},
                'analyze_audio': {'audio-insights.json'},
                'separate_audio': {'instrumentals.wav', 'vocals.wav', 'audio-separation.json'},
                'optimize_spoken': {'speech-suggestions.json', 'speech-rewrite-checkpoint.json'},
                'speaker_groups': {'speaker-groups.json'},
                'enroll_voice': {'reference.wav', 'ref_codes.txt', 'speaker.emb.txt', 'voice.json'},
                'preview': {'before.jpg', 'after.jpg', 'mask.jpg'},
                'ocr': {'source.srt'}, 'asr': {'source.srt'},
                'auto_subtitles': {'ocr-tracks.json','subtitle-automation.json'}, 'subtitle_preview':{'subtitle-preview.png'},
                'translate': {'translated.srt'}, 'scan': {'masks.json'}}.get(job['kind'], set())
    if result.get('needs_review') and job['kind'] in ('pipeline', 'ocr', 'asr'):
        # A semantic-only continuation retains raw OCR in its owner job and
        # produces a separate selected transcript in this job's folder.
        if job['kind'] == 'pipeline' and result.get('review_stage') == 'pre_render':
            required = {'pipeline-review.json'}
            if (result.get('remaining_steps') != ['render'] or 'final.mp4' in files or
                    result.get('primary_file') == 'final.mp4' or
                    not isinstance(result.get('pre_render_signature'), str) or
                    len(result['pre_render_signature']) != 64):
                raise ValueError('Kết quả chờ duyệt trước xuất không hợp lệ; chưa công bố video thành phẩm.')
        else:
            required = ({'selected-source.srt'} if job['kind'] == 'pipeline' and
                        'source.srt' not in files else {'source.srt'})
    if not required.issubset(files):
        raise ValueError('Worker thiếu kết quả bắt buộc của bước xử lý.')
    if job['kind'] == 'separate_audio':
        verified_audio = _verify_separation(result, project, base)
    for filename in files:
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ValueError('Tên tệp kết quả không hợp lệ.')
        path = (base / filename).resolve()
        if path.parent != base or not path.is_file():
            raise ValueError('Thiếu tệp kết quả hoặc đường dẫn không thuộc tác vụ.')
        if path.suffix.lower() in {'.mp4', '.mkv', '.mov'}:
            if not path.stat().st_size:
                raise ValueError('Video đầu ra rỗng.')
            info = probe(path)
            expected = float(project['media']['duration'])
            from clean_timing import video_span
            expected_video, _ = video_span(project['media'])
            params = project.get('params', {})
            expects_audio = bool(project['media'].get('audio')) if job['kind'] in {'clean', 'clip'} else (
                (project['media'].get('audio') and params.get('original_gain', 0) > 0)
                or (params.get('bgm_path') and params.get('bgm_gain', 0) > 0)
                or ((params.get('voice_path') or project.get('artifacts', {}).get('tts'))
                    and params.get('voice_gain', 0) > 0))
            if job['kind'] == 'clip':
                from clean_timing import clip_window
                start, expected = clip_window(project['media'], job.get('time', 0), refresh=True)
                expected_video = expected
                if abs(float(result.get('start', start)) - start) > 1 / max(1, project['media']['fps']):
                    raise ValueError('Mốc bắt đầu của mẫu không khớp khoảng xem thử.')
            elif not expects_audio:
                # Muted exports end with the pictures. A source's longer audio
                # tail must not be mistaken for extra video frames.
                expected = expected_video
            tolerance = max(.12, 2 / max(1, project['media']['fps']))
            if abs(info['duration'] - expected) > tolerance:
                raise ValueError(f'Thời lượng đầu ra {info["duration"]:.3f}s không khớp {expected:.3f}s.')
            if abs(info.get('video_duration', info['duration']) - expected_video) > tolerance:
                raise ValueError('Luồng hình ngắn hơn khoảng đã chọn; âm thanh không thể bù cho hình bị thiếu.')
            if job['kind'] in {'clean', 'clip'} and project['media']['audio'] and not info['audio']:
                raise ValueError('Đầu ra xóa sub bị thiếu âm thanh nguồn.')
            if job['kind'] in {'clean', 'clip', 'render', 'pipeline'}:
                from core import run, binary
                # Decode the published output, not merely its metadata header.
                decoded = run([binary('ffmpeg'),'-v','error','-xerror','-threads','1','-i',path,
                    '-map','0:v:0','-map','0:a:0?','-progress','pipe:1','-nostats','-f','null','-'], timeout=max(60,expected*2))
                counts = [int(line.split('=',1)[1]) for line in decoded.stdout.decode('utf-8').splitlines() if line.startswith('frame=')]
                frames = max(counts, default=0)
                output_fps = max(1, info.get('fps', project['media']['fps']))
                if not frames or abs(frames / output_fps - expected_video) > tolerance:
                    raise ValueError('Không giải mã đủ số khung của bản kết quả. Chưa công bố hoàn tất.')
                decoded_videos[filename] = frames
            if job['kind'] in {'render', 'pipeline'} and filename == 'final.mp4':
                if expects_audio and not info['audio']:
                    raise ValueError('Bản xuất thiếu luồng âm thanh đã chọn.')
        elif path.suffix.lower() == '.wav':
            if filename in verified_audio:
                continue
            from audio_verification import verify_pcm_speech
            duration = project['media']['duration'] if filename == 'voice.wav' else result.get('duration')
            audio = verify_pcm_speech(path, expected_duration=duration,
                                      expected_rate=result.get('sample_rate'))
            verified_audio[filename] = audio['frames']
        elif path.suffix.lower() == '.mp3' and job['kind'] == 'text_to_speech':
            from audio_inputs import inspect_audio
            from core import run, binary
            info = inspect_audio(str(path))
            if result.get('duration') and abs(info['duration'] - float(result['duration'])) > .2:
                raise ValueError('MP3 không khớp thời lượng bản giọng WAV đã tạo.')
            run([binary('ffmpeg'), '-v', 'error', '-i', path, '-f', 'null', '-'], timeout=120)
    verification = {'files_checked': len(files), 'video_frames_decoded': decoded_videos, 'checked_at': time.time()}
    from stage_journal import describe_file
    verification['file_integrity'] = [describe_file(base, filename) for filename in files]
    if verified_audio:
        verification['audio_frames_verified'] = verified_audio
    return verification
