"""Validate OCR continuation against source, settings, and search-area strategy."""
from __future__ import annotations

import json
import math
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024
MAX_ROWS = 10000
_INVALID = 'Checkpoint OCR không hợp lệ. Dữ liệu đã nhận dạng vẫn được giữ; không tự chạy lại từ đầu.'


def _number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(_INVALID)
    return value


def _bounds(project):
    media, params = project['media'], project['params']
    duration = _number(media['duration'])
    fps = _number(params.get('ocr_fps', 2))
    if duration <= 0 or not .2 <= fps <= 8:
        raise ValueError(_INVALID)
    start = max(0, float(_number(params.get('extraction_start', 0))))
    requested_end = params.get('extraction_end', 0)
    if requested_end is not None:
        _number(requested_end)
    end = min(media['duration'], float(requested_end or media['duration']))
    if start >= end:
        raise ValueError('Phạm vi OCR hiện tại không hợp lệ; không tiếp tục checkpoint cũ.')
    return start, end, 1 / float(fps)


def signature(project):
    """Keep the original container-duration range, defaults and JSON encoding.

    Video stream duration is used by the decoder only. Adding it to this key or
    clamping the range would invalidate valid checkpoints written before the
    decoder tail fix and discard their already recognized cues.
    """
    return _signature(project, version=5)


def _signature(project, *, version):
    from subtitle_quality import digest, source_signature
    start, end, interval = _bounds(project)
    params = project['params']
    regions = project.get('extraction_regions', project['regions'])
    payload = {'source': source_signature(project), 'regions': regions,
               'range': [start, end], 'interval': interval,
               'confidence': params.get('ocr_confidence', .55),
               'similarity': params.get('ocr_similarity', .84),
               'scale': params.get('ocr_scale_mode', 'native'), 'version': version}
    if version == 5:
        payload['search_area'] = 'automatic_full_frame_v1' if not regions else 'explicit_regions_v1'
    _finite_tree(payload)
    return digest(payload)


def _finite_tree(value):
    # A bounded file can still contain deep nesting or an overflowing JSON
    # exponent. Check all values, including optional/unknown metadata.
    pending = [(value, 0)]
    visited = 0
    while pending:
        current, depth = pending.pop()
        visited += 1
        if depth > 16 or visited > 200000:
            raise ValueError(_INVALID)
        if isinstance(current, float) and not math.isfinite(current):
            raise ValueError(_INVALID)
        if isinstance(current, dict):
            pending.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            pending.extend((item, depth + 1) for item in current)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(_INVALID)
        result[key] = value
    return result


def _bad_constant(_value):
    raise ValueError(_INVALID)


def validate_resume(path, project, *, completed_legacy_original=None):
    """Return a bounded, valid checkpoint tied to the current source/config.

    The final sample increments next_time by one interval. A checkpoint already
    at the decoder tail also needs decoded_complete, which proves that FFmpeg
    exited successfully. A missing EOF confirmation must not silently restart
    OCR at zero or turn a decoder failure into a successful result.
    """
    try:
        source = Path(project['media']['path'])
        if not source.is_file():
            raise ValueError('Không tìm thấy video nguồn của checkpoint OCR. Chọn lại đúng nguồn trước khi tiếp tục.')
        start, end, interval = _bounds(project)
        checkpoint = Path(path)
        if checkpoint.stat().st_size > MAX_BYTES:
            raise ValueError('Checkpoint OCR vượt giới hạn 16 MiB; không tự nạp hoặc nhận dạng lại từ đầu.')
        with checkpoint.open('rb') as stream:
            encoded = stream.read(MAX_BYTES + 1)
        if len(encoded) > MAX_BYTES:
            raise ValueError(_INVALID)
        saved = json.loads(encoded.decode('utf-8'), parse_constant=_bad_constant,
                           object_pairs_hook=_object)
        _finite_tree(saved)
        if not isinstance(saved, dict):
            raise ValueError(_INVALID)
        legacy_complete = False
        if saved.get('signature') != signature(project) and completed_legacy_original is not None:
            # This exception is for consuming a completed v4 OCR asset, never
            # for resuming the decoder with today's full-frame strategy.
            original = completed_legacy_original
            legacy_complete = (saved.get('decoded_complete') is True and
                original.get('id') == project.get('id') and
                original.get('source_revision') == project.get('source_revision') and
                saved.get('signature') == _signature(original, version=4) == _signature(project, version=4))
        if saved.get('signature') != signature(project) and not legacy_complete:
            raise ValueError('Checkpoint OCR không khớp video, vùng đọc hoặc cấu hình hiện tại. '
                             'Giữ nguyên phần nhận dạng đã lưu; không tự chạy lại từ đầu.')
        rows = saved.get('rows')
        current = saved.get('current')
        if (not isinstance(rows, list) or len(rows) + (current is not None) > MAX_ROWS or
                'current' not in saved or ('decoded_complete' in saved and type(saved['decoded_complete']) is not bool)):
            raise ValueError(_INVALID)
        next_time = _number(saved.get('next_time'))
        epsilon = 1e-5
        if not start - epsilon <= next_time <= end + interval + epsilon:
            raise ValueError(_INVALID)
        previous_end = start
        for row in rows + ([current] if current is not None else []):
            if not isinstance(row, dict):
                raise ValueError(_INVALID)
            begin, finish = _number(row.get('start')), _number(row.get('end'))
            if (begin < start - epsilon or finish <= begin or finish > end + epsilon or
                    begin < previous_end - epsilon or finish > next_time + epsilon):
                raise ValueError(_INVALID)
            text, translated = row.get('text'), row.get('translated', '')
            if (not isinstance(text, str) or not text.strip() or len(text) > 10000 or '\x00' in text or
                    not isinstance(translated, str) or len(translated) > 10000 or '\x00' in translated):
                raise ValueError(_INVALID)
            previous_end = finish
        clipped = saved.get('clipped_samples', [])
        if not isinstance(clipped, list) or len(clipped) > 50:
            raise ValueError(_INVALID)
        previous = start
        for moment in clipped:
            moment = _number(moment)
            if not start - epsilon <= moment <= min(end, next_time) + epsilon or moment < previous:
                raise ValueError(_INVALID)
            previous = moment
        # Decoder coverage is separate from the requested OCR range: clamping its
        # signature range here would invalidate older, still recoverable cues.
        from video_sampling import sample_end, sample_tail_tolerance
        effective_end = sample_end(project['media'], end)
        tail = sample_tail_tolerance(project['media'], interval)
        if legacy_complete and next_time < effective_end - tail:
            raise ValueError('Checkpoint OCR cũ chưa hoàn tất; không tiếp tục decoder bằng chiến lược vùng đọc mới.')
        if next_time >= effective_end - tail and not saved.get('decoded_complete'):
            raise ValueError('Checkpoint OCR đã lưu các câu ở cuối video nhưng chưa xác nhận giải mã hoàn tất. '
                             'Giữ nguyên dữ liệu đã nhận dạng; không tự nhận dạng lại từ đầu.')
        return saved
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, KeyError, TypeError, OverflowError) as exc:
        raise ValueError(_INVALID) from exc
