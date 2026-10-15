"""Offline known-transcript alignment of the generated voice, separate from STT.

Whisper's cross-attention DTW provides approximate acoustic word boundaries;
it does not certify that the synthesizer pronounced every word correctly.
"""
import copy
import hashlib
import json
import math
from pathlib import Path
import re
import statistics

ALIGNMENT_VERSION = 'npk-dub-word-dtw-v2'


def caption_language(project):
    value = str(project['params'].get('target_language', 'vi')).strip().lower()
    return {'tiếng việt': 'vi', 'vietnamese': 'vi', 'tiếng anh': 'en', 'english': 'en',
            'tiếng trung': 'zh', 'chinese': 'zh', 'tiếng nhật': 'ja', 'japanese': 'ja',
            'tiếng hàn': 'ko', 'korean': 'ko'}.get(value, value)


def caption_timing_key(project):
    from worker import speech_signature
    params = project['params']
    external = params.get('voice_path')
    identity = project.get('artifacts', {}).get('tts', {}).get('job_id')
    if external:
        path = Path(external)
        try:
            stat = path.stat()
            identity = [str(path.resolve()), stat.st_size, stat.st_mtime_ns]
        except OSError:
            identity = ['missing', external]
    try:
        speech_identity = speech_signature(project)
    except (ValueError, OSError):
        if not external:
            raise
        # An imported WAV remains usable after an old synthesis voice is removed.
        from speech_delivery import signature_payload
        speech_identity = ['external-audio', signature_payload(project)]
    payload = [ALIGNMENT_VERSION, caption_language(project), speech_identity, identity,
               float(params.get('voice_offset', 0)),
               [[row['start'], row['end'], row.get('translated') or row.get('text', '')]
                for row in project['segments']]]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def prune_stale_timings(project):
    if not any('caption_words' in row or 'caption_timing_key' in row for row in project['segments']):
        return
    try:
        key = caption_timing_key(project)
    except (ValueError, OSError):
        # Reading a project must still allow choosing another installed voice.
        key = None
    for row in project['segments']:
        if key is None or row.get('caption_timing_key') != key:
            if 'caption_words' in row or 'caption_timing_key' in row:
                # Keep an explicit empty override: source ASR words can have
                # identical Vietnamese text but belong to another audio track.
                row['caption_words'] = []
                row['word_timing_source'] = 'estimated'
                row.pop('caption_timing_key', None)


def display_word_timings(displayed, raw, cue_start, cue_end, offset=0):
    """Map DTW tokens/punctuation to display tokens without rewriting their text."""
    from karaoke import normalized
    visible = re.findall(r'\S+', displayed)
    spans, cursor = [], 0
    for word in raw:
        normalized_word = normalized(word.get('word', ''))
        if not normalized_word:
            continue
        a, b = float(word['start']), float(word['end'])
        probability = float(word.get('probability', 0))
        if not math.isfinite(a + b + probability) or b <= a or not 0 <= probability <= 1:
            raise ValueError('Mốc từ bị co về 0 hoặc không hợp lệ.')
        spans.append((cursor, cursor + len(normalized_word), a, b, probability))
        cursor += len(normalized_word)
    if ''.join(normalized(token) for token in visible) != ''.join(normalized(word.get('word', '')) for word in raw):
        raise ValueError('Mốc âm thanh không khớp nội dung phụ đề.')
    if not spans or statistics.median(span[4] for span in spans) < .15:
        raise ValueError('Độ tin cậy căn âm thanh thấp; cần nghe kiểm tra.')
    result, position, previous = [], 0, cue_start
    for token in visible:
        size = len(normalized(token))
        matching = [span for span in spans if span[0] < position + size and position < span[1]]
        if not matching:
            raise ValueError('Chưa có mốc âm thanh cho toàn bộ chữ.')
        start = cue_start + offset + matching[0][2]
        end = cue_start + offset + matching[-1][3]
        if start < cue_start - .02 or end > cue_end + .02 or start < previous - .02:
            raise ValueError('Mốc âm thanh vượt thời gian câu hoặc bị đảo thứ tự.')
        start, end = max(cue_start, start, previous), min(cue_end, end)
        if end <= start:
            raise ValueError('Mốc từ không còn thời gian để hiển thị.')
        result.append({'word': token, 'start': round(start, 4), 'end': round(end, 4),
                       'probability': round(sum(span[4] for span in matching) / len(matching), 4)})
        position += size
        previous = end
    return result


def valid_cached_report(report, project, signature, audio_hash):
    """A partial/corrupt cache must never attach stale timings to other rows."""
    if not isinstance(report, dict) or report.get('version') != ALIGNMENT_VERSION:
        return False
    if report.get('signature') != signature or report.get('audio_sha256') != audio_hash:
        return False
    items = report.get('rows')
    if not isinstance(items, list) or len(items) != len(project['segments']):
        return False
    from subtitle_presets import highlight_word_timings
    for index, (item, row) in enumerate(zip(items, project['segments'])):
        if not isinstance(item, dict) or type(item.get('index')) is not int or item['index'] != index or item.get('source') not in ('aligned', 'estimated'):
            return False
        if not isinstance(item.get('words'), list):
            return False
        if item['source'] == 'aligned':
            for word in item['words']:
                if not isinstance(word, dict):
                    return False
                values = [word.get(field) for field in ('start', 'end', 'probability')]
                if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
                    return False
                start, end, probability = values
                if start < row['start'] or end > row['end'] or end <= start or not 0 <= probability <= 1:
                    return False
            candidate = dict(row, caption_words=item['words'], word_timing_source='aligned')
            if highlight_word_timings(candidate, row.get('translated') or row.get('text', ''))['source'] != 'aligned':
                return False
        elif item['words']:
            return False
    aligned = sum(item['source'] == 'aligned' for item in items)
    return (type(report.get('aligned_cues')) is int and type(report.get('estimated_cues')) is int and
            report['aligned_cues'] == aligned and report['estimated_cues'] == len(items) - aligned)


def align_captions(project, voice_path, folder, emit=None):
    """Align matching narration on CPU; cache across font-only rerenders."""
    from core import DATA, MODELS
    from karaoke import normalized
    from speech_delivery import spoken_text
    folder = Path(folder)
    key = caption_timing_key(project)
    with Path(voice_path).open('rb') as source:
        audio_hash = hashlib.file_digest(source, 'sha256').hexdigest()
    model_name, model_path = None, None
    for name in ('small', 'tiny'):
        candidates = sorted((MODELS / 'whisper' / ('models--Systran--faster-whisper-' + name) / 'snapshots').glob('*'))
        for candidate in candidates:
            if all((candidate / required).is_file() for required in ('model.bin', 'tokenizer.json', 'config.json')):
                model_name, model_path = name, candidate
                break
        if model_path:
            break
    cache_key = hashlib.sha256(json.dumps([key, audio_hash, str(model_path), ALIGNMENT_VERSION]).encode()).hexdigest()
    cache = DATA / 'word-timing-cache'
    cache.mkdir(exist_ok=True)
    cached = cache / (cache_key + '.json')
    if cached.is_file():
        try:
            report = json.loads(cached.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            report = None
        if valid_cached_report(report, project, key, audio_hash):
            report['cache_reused'] = True
            report.setdefault('model', model_name)
            report.setdefault('method', 'known-transcript-dtw')
            (folder / 'speech-word-timings.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            return apply_timings(project, report), report
    report = {'version': ALIGNMENT_VERSION, 'signature': key, 'audio_sha256': audio_hash,
              'model': model_name, 'method': 'known-transcript-dtw', 'device': 'cpu',
              'language': caption_language(project), 'cache_reused': False, 'rows': []}
    model, audio, tokenizer = None, None, None
    startup_error = None
    if model_path:
        try:
            from faster_whisper import WhisperModel
            from faster_whisper.audio import decode_audio
            from faster_whisper.tokenizer import Tokenizer, _LANGUAGE_CODES
            model = WhisperModel(str(model_path), device='cpu', compute_type='int8', cpu_threads=4, local_files_only=True)
            audio = decode_audio(str(voice_path), sampling_rate=16000)
            if report['language'] in _LANGUAGE_CODES:
                tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task='transcribe', language=report['language'])
        except (ImportError, OSError, RuntimeError, ValueError):
            # This optional presentation feature must not prevent a verified
            # video from rendering if its offline alignment runtime is damaged.
            model, audio, tokenizer = None, None, None
            startup_error = 'Chưa khởi động được bộ căn từ offline; kiểm tra runtime/model trên máy.'
    try:
        for index, row in enumerate(project['segments']):
            text = (row.get('translated') or row.get('text', '')).strip()
            item = {'index': index, 'source': 'estimated', 'words': []}
            try:
                if not model:
                    raise ValueError(startup_error or 'Chưa có model căn từ offline trên máy.')
                if tokenizer is None:
                    raise ValueError('Ngôn ngữ đích chưa được bộ căn từ hỗ trợ.')
                if normalized(spoken_text(row, project['params'])) != normalized(text):
                    raise ValueError('Lời đọc riêng khác phụ đề; không thể căn đủ chữ theo giọng.')
                a, b = float(row['start']), float(row['end'])
                if b - a > 29 or len(tokenizer.encode(text)) > 420:
                    raise ValueError('Câu quá dài cho bộ căn từ; chia câu trước khi căn chính xác.')
                clip = audio[round(a * 16000):round(b * 16000)]
                if len(clip) < 320:
                    raise ValueError('Không đủ âm thanh để căn từ.')
                from faster_whisper.audio import pad_or_trim
                from faster_whisper.transcribe import merge_punctuations
                features = model.feature_extractor(clip)
                frames = min(3000, features.shape[-1] - 1)
                encoded = model.encode(pad_or_trim(features))
                raw = model.find_alignment(tokenizer, [tokenizer.encode(' ' + text)], encoded, frames)[0]
                merge_punctuations(raw, "\"'“¿([{-", "\"'.。,，!！?？:：”)]}、")
                raw = [word for word in raw if word.get('word')]
                item['words'] = display_word_timings(text, raw, a, b, float(project['params'].get('voice_offset', 0)))
                item['source'] = 'aligned'
            except (ValueError, RuntimeError, IndexError) as exc:
                item['reason'] = str(exc)
            report['rows'].append(item)
            if emit:
                emit(stage='word_alignment', progress=(index + 1) / max(1, len(project['segments'])),
                     message=f'Căn màu theo lời đọc · {index + 1}/{len(project["segments"])} câu · CPU')
    finally:
        del model, audio, tokenizer
    report['aligned_cues'] = sum(row['source'] == 'aligned' for row in report['rows'])
    report['estimated_cues'] = len(report['rows']) - report['aligned_cues']
    content = json.dumps(report, ensure_ascii=False, indent=2)
    if not startup_error:
        temporary = cached.with_suffix('.tmp')
        temporary.write_text(content, encoding='utf-8')
        temporary.replace(cached)
    (folder / 'speech-word-timings.json').write_text(content, encoding='utf-8')
    return apply_timings(project, report), report


def apply_timings(project, report):
    rows = copy.deepcopy(project['segments'])
    for item in report['rows']:
        row = rows[item['index']]
        row.update(caption_words=copy.deepcopy(item['words']), word_timing_source=item['source'],
                   caption_timing_key=report['signature'])
    return rows
