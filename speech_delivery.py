"""Pure delivery planning for offline dubbing; no model or project storage imports."""
from __future__ import annotations

import math

from subtitle_text import (has_subtitle_content, is_non_dialogue_source,
                           sanitize_subtitle_text, subtitle_text_info)


NATURAL_MAX_TEMPO = 1.15
AUTO_MAX_TEMPO = 1.25
SUBTITLE_CLEANUP_VERSION = 1


def timing_mode(params):
    """Snapshots made before delivery controls retain the previous timing policy."""
    mode = params.get('tts_timing_mode', 'legacy')
    if mode not in ('natural', 'legacy'):
        raise ValueError('Chế độ căn giọng không hợp lệ. Chọn Tự nhiên hoặc Tương thích cũ.')
    return mode


def text_source(params):
    """None is the historical automatic fallback, only for old snapshots."""
    source = params.get('tts_text_source')
    if source is not None and source not in ('translated', 'edited', 'source'):
        raise ValueError('Nguồn lồng tiếng không hợp lệ. Chọn Bản dịch, Lời đã biên tập hoặc Phụ đề gốc.')
    return source


def _selected_text(row, params):
    """Choose once before cleaning; a removed credit must not fall back to OCR."""
    source = text_source(params)
    keys = {'translated': ('translated',), 'edited': ('spoken_text', 'translated'),
            'source': ('text',), None: ('spoken_text', 'translated', 'text')}[source]
    for key in keys:
        value = row.get(key) or ''
        if str(value).strip():
            return str(value).strip()
    return ''


def _cleanup_silences_row(row, params):
    if not params.get('subtitle_text_cleanup', False):
        return False
    original = str(row.get('text') or '').strip()
    original_info = subtitle_text_info(original)
    if original_info['credit_only'] or (original and not original_info['text']):
        return True
    selected = _selected_text(row, params)
    selected_info = subtitle_text_info(selected)
    return selected_info['credit_only'] or bool(selected and not selected_info['text'])


def spoken_text(row, params=None):
    params = params or {}
    source = str(row.get('text') or '').strip()
    if (row.get('speech_enabled', True) is False or
            (bool(source) and is_non_dialogue_source(source)) or _cleanup_silences_row(row, params)):
        return ''
    selected = _selected_text(row, params)
    return sanitize_subtitle_text(selected)


def speech_cleanup_report(rows, params, *, selected_indices=None):
    """Audit the exact selected narration without changing caption rows.

    The worker, preflight cache and artifact signature must all use this same
    list. Handle sanitation and non-dialogue muting are universal safety rules;
    the opt-in cleanup flag controls duplicate/orphan preparation for legacy
    snapshots. Cleanup never merges caption rows at this boundary.
    """
    import copy
    result = copy.deepcopy(rows)
    if not params.get('subtitle_text_cleanup', False):
        changes = []
        for index, row in enumerate(result):
            source = str(row.get('text') or '').strip()
            selected = _selected_text(row, params)
            source_info = subtitle_text_info(source)
            selected_info = subtitle_text_info(selected)
            source_noise = bool(source) and (
                source_info['credit_only'] or not has_subtitle_content(source_info['text']))
            selected_noise = bool(selected) and (
                selected_info['credit_only'] or not has_subtitle_content(selected_info['text']))
            if row.get('speech_enabled', True) is not False and (source_noise or selected_noise):
                row['speech_enabled'] = False
                changes.append({'index': index, 'reason': 'non_dialogue_or_empty_after_cleanup',
                                'speech_enabled': False})
        return {'version': SUBTITLE_CLEANUP_VERSION, 'stage': 'speech',
                'text_source': text_source(params), 'cleanup_enabled': False,
                'segments': result, 'changes': changes, 'input_count': len(rows),
                'output_count': len(rows), 'needs_review': False, 'review_reasons': [],
                'source_indices': [[index] for index in range(len(rows))]}
    for row in result:
        # Supply the selected raw string, so the report captures credit/noise
        # removal as a role transition rather than silently pre-muting it.
        row['_speech_comparison_text'] = _selected_text(row, params)
    from clean_timing import prepare_subtitle_segments
    report = prepare_subtitle_segments(result, stage='speech', selected_indices=selected_indices)
    prepared = report['segments']
    caption_fields = ('id', 'start', 'end', 'text', 'translated', 'spoken_text')
    if len(prepared) != len(rows) or any(
            any(before.get(key) != after.get(key) for key in caption_fields)
            for before, after in zip(rows, prepared)):
        raise ValueError('Làm sạch lời đọc đã thay đổi số câu, nội dung hoặc mốc phụ đề; chưa tạo cache giọng.')
    for row in prepared:
        row.pop('_speech_comparison_text', None)
    report.update(text_source=text_source(params), cleanup_enabled=True)
    return report


def effective_speech_rows(rows, params):
    """The narration rows shared by worker, preflight cache and signature."""
    return speech_cleanup_report(rows, params)['segments']


def missing_speech_rows(rows, params):
    """Explicit choices are strict; only old snapshots honor legacy fallback."""
    rows = effective_speech_rows(rows, params)
    source = text_source(params)
    if source is None and params.get('tts_require_translation', True):
        return [i for i, row in enumerate(rows) if row.get('speech_enabled', True) is not False and not any(
            str(row.get(key) or '').strip() for key in ('spoken_text', 'translated'))]
    return [i for i, row in enumerate(rows) if row.get('speech_enabled', True) is not False and not spoken_text(row, params)]


def require_speech_text(rows, params):
    rows = effective_speech_rows(rows, params)
    if rows and not any(row.get('speech_enabled', True) is not False for row in rows):
        raise ValueError('Không có câu bật lồng tiếng. Bật Lồng tiếng câu này hoặc tắt bước lồng tiếng để chỉ xuất phụ đề.')
    missing = missing_speech_rows(rows, params)
    if not missing:
        return
    indices = ', '.join(str(i + 1) for i in missing[:12])
    if len(missing) > 12:
        indices += f' và {len(missing) - 12} câu khác'
    source = text_source(params)
    action = {
        'translated': 'Đang chọn Bản dịch. Dịch đủ các câu hoặc chọn Lời đã biên tập / Phụ đề gốc.',
        'edited': 'Đang chọn Lời đã biên tập. Điền lời đọc riêng hoặc bản dịch; không tự đọc phụ đề gốc.',
        'source': 'Đang chọn Phụ đề gốc. Điền nội dung gốc cho các câu này.',
        None: 'Điền bản dịch/lời đọc riêng trước, hoặc chủ động chọn Phụ đề gốc để đọc nguồn.',
    }[source]
    raise ValueError(f'Câu {indices}: thiếu nội dung lồng tiếng. {action}')


def speech_rate(row, params):
    value = row.get('speech_rate')
    if value is None or value == '':
        value = params.get('voice_speed', 1)
    try:
        rate = float(value)
    except (TypeError, ValueError):
        raise ValueError('Tốc độ đọc cần là số từ 0,5 đến 2.') from None
    if not math.isfinite(rate) or not .5 <= rate <= 2:
        raise ValueError('Tốc độ đọc cần nằm trong khoảng 0,5–2.')
    return rate


def _pause(row, key, cue_number):
    try:
        value = float(row.get(key) or 0)
    except (TypeError, ValueError):
        raise ValueError(f'Câu {cue_number}: thời gian ngắt nghỉ không hợp lệ.') from None
    if not math.isfinite(value) or not value.is_integer() or not 0 <= value <= 2000:
        raise ValueError(f'Câu {cue_number}: khoảng nghỉ cần là số nguyên từ 0 đến 2000 ms.')
    return int(value)


def delivery_window(row, params, cue_number, media_duration=None):
    """Reserve silence within the cue; never extend a cue into the next one."""
    mode = timing_mode(params)
    before = _pause(row, 'pause_before_ms', cue_number)
    after = _pause(row, 'pause_after_ms', cue_number)
    try:
        cue_start, cue_end = float(row['start']), float(row['end'])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f'Câu {cue_number}: mốc thời gian không hợp lệ.') from None
    if not math.isfinite(cue_start) or not math.isfinite(cue_end) or cue_end <= cue_start:
        raise ValueError(f'Câu {cue_number}: thời gian kết thúc cần lớn hơn thời gian bắt đầu.')
    start, end = ((cue_start, cue_end) if row.get('speech_enabled', True) is False
                  else (cue_start + before / 1000, cue_end - after / 1000))
    if end <= start:
        raise ValueError(f'Câu {cue_number}: khoảng nghỉ chiếm hết thời gian đọc. Giảm ngắt nghỉ hoặc tăng thời gian câu.')
    if mode == 'natural' and (cue_start < 0 or
            (media_duration is not None and cue_end > float(media_duration) + 1e-9)):
        raise ValueError(f'Câu {cue_number}: thời gian lời đọc vượt video. Sửa mốc câu trước khi tạo giọng.')
    return {'mode': mode, 'start': start, 'end': end, 'available': end - start,
            'rate': speech_rate(row, params), 'text': spoken_text(row, params),
            'pause_before_ms': before, 'pause_after_ms': after}


def fit_rate(duration, available, mode, cue_number, *, preserve_text=False, tempo_limit=None):
    """Bound post synthesis time compression; long natural speech needs editing."""
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(f'Câu {cue_number}: âm thanh tạo ra không có thời lượng hợp lệ.')
    if not math.isfinite(available) or available <= 0:
        raise ValueError(f'Câu {cue_number}: không còn thời gian cho lời đọc sau ngắt nghỉ.')
    rate = max(1., duration / available)
    limit = (NATURAL_MAX_TEMPO if tempo_limit is None else tempo_limit) if mode == 'natural' else 3.
    if not math.isfinite(limit) or not 1 <= limit <= AUTO_MAX_TEMPO and mode == 'natural':
        raise ValueError('Giới hạn căn giọng không hợp lệ.')
    if rate > limit + 1e-9:
        if mode == 'natural':
            advice = ('Giữ đủ nội dung; giảm ngắt nghỉ, tăng thời gian câu hoặc điều chỉnh tốc độ đọc rồi tạo lại; '
                      if preserve_text else 'Rút gọn lời đọc, giảm ngắt nghỉ hoặc tăng thời gian câu rồi tạo lại; ')
            raise ValueError(f'Câu {cue_number}: lời đọc dài {duration:.2f} giây nhưng chỉ còn {available:.2f} giây sau ngắt nghỉ. '
                f'Cần tăng tốc {rate:.2f}×, vượt giới hạn tự nhiên {limit:.2f}×. '
                + advice + 'âm thanh đã tạo được giữ trong bộ nhớ đệm.')
        raise ValueError(f'Câu {cue_number} quá dài: cần tăng tốc {rate:.1f} lần. '
            'Rút gọn lời hoặc tăng thời gian câu rồi chạy lại; các câu đã tạo được giữ trong bộ nhớ đệm.')
    return min(rate, limit)


def signature_payload(project):
    """Keep existing artifacts valid when an old project has no delivery changes."""
    params = project['params']
    segments = effective_speech_rows(project['segments'], params)
    mode = timing_mode(params)
    source = text_source(params)
    def with_expressive(payload):
        if params.get('tts_auto_schedule', False):
            payload = ['npk-voice-auto-schedule-v2', AUTO_MAX_TEMPO, payload]
        # Preserve every historical False/missing digest byte-for-byte.
        if not params.get('tts_expressive', False):
            return payload
        from expressive_text import EXPRESSIVE_VERSION
        return ['npk-voice-expressive-v1', EXPRESSIVE_VERSION, payload]
    if params.get('subtitle_text_cleanup', False):
        rows = [[r['start'], r['end'], spoken_text(r, params), r.get('voice_id'), speech_rate(r, params),
                 _pause(r, 'pause_before_ms', i + 1), _pause(r, 'pause_after_ms', i + 1)]
                for i, r in enumerate(segments) if r.get('speech_enabled', True) is not False]
        return with_expressive(['npk-voice-subtitle-cleanup-v1', SUBTITLE_CLEANUP_VERSION,
                project['media']['duration'], rows, mode, source,
                params.get('voice_speed', 1), params.get('voice_id', 'piper:vais1000')])
    if source is not None:
        rows = [[r['start'], r['end'], spoken_text(r, params), r.get('voice_id'), speech_rate(r, params),
                 _pause(r, 'pause_before_ms', i + 1), _pause(r, 'pause_after_ms', i + 1)]
                for i, r in enumerate(segments) if r.get('speech_enabled', True) is not False]
        return with_expressive(['npk-voice-v5', project['media']['duration'], rows, mode, source,
                params.get('voice_speed', 1), params.get('voice_id', 'piper:vais1000')])
    legacy_rows = [[r['start'], r['end'], spoken_text(r, params),
                    r.get('voice_id')] for r in segments]
    changed = any(
        r.get('speech_enabled', True) is False or spoken_text(r, params) !=
            (r.get('spoken_text') or r.get('translated') or r.get('text', '')).strip() or
        speech_rate(r, params) != float(params.get('voice_speed', 1)) or
        (r.get('pause_before_ms') or 0) != 0 or (r.get('pause_after_ms') or 0) != 0
        for r in segments)
    if mode == 'legacy' and not changed:
        return with_expressive(['npk-voice-v3', project['media']['duration'], legacy_rows,
                params.get('voice_speed', 1), params.get('voice_id', 'piper:vais1000')])
    rows = [[r['start'], r['end'], spoken_text(r), r.get('voice_id'), speech_rate(r, params),
             _pause(r, 'pause_before_ms', index + 1), _pause(r, 'pause_after_ms', index + 1)]
            for index, r in enumerate(segments) if r.get('speech_enabled', True) is not False]
    return with_expressive(['npk-voice-v4', project['media']['duration'], rows, mode,
            params.get('voice_speed', 1), params.get('voice_id', 'piper:vais1000')])


def schedule_speech(plans, durations, media_duration):
    """Place measured, complete clips in order; captions and raw cache stay intact.

    Reserve a small decoder/resampler margin and explicit pauses. Prefer original
    starts, borrowing later gaps and, if necessary, earlier free time. A uniform
    tempo is raised only when needed for capacity or to reduce accumulated drift.
    """
    limit = float(media_duration)
    if not math.isfinite(limit) or limit <= 0 or len(plans) != len(durations):
        raise ValueError('Không đủ dữ liệu để bố trí lời đọc trong video.')
    spoken = [(i, p, durations[i]) for i, p in enumerate(plans) if p['text']]
    if any(d is None or not math.isfinite(d) or d <= 0 for _, _, d in spoken):
        raise ValueError('Cần đo đủ mọi câu trước khi bố trí lời đọc.')
    if any(b[1]['start'] < a[1]['start'] for a, b in zip(spoken, spoken[1:])):
        raise ValueError('Thứ tự câu không tăng theo thời gian; cần kiểm tra phụ đề trước khi bố trí lời đọc.')

    def arrange(tempo):
        spans = [d / tempo + .06 + (p['pause_before_ms'] + p['pause_after_ms']) / 1000
                 for _, p, d in spoken]
        if sum(spans) > limit + 1e-9:
            return None
        # Subtract the packed prefix: non-overlap becomes a nondecreasing
        # sequence of free-space offsets. Minimax isotonic bounds spread a
        # dense passage's displacement both earlier and later, instead of
        # letting a greedy late cue absorb the entire accumulated drift.
        slack, prefix, targets = max(0., limit-sum(spans)), 0., []
        for (_, plan, _), span in zip(spoken, spans):
            targets.append(plan['start'] - plan['pause_before_ms']/1000 - prefix)
            prefix += span
        delta, peak = 0., -math.inf
        for target in targets:
            peak = max(peak, target)
            delta = max(delta, -target, target-slack, (peak-target)/2)
        upper, suffix = [], slack
        for target in reversed(targets):
            suffix = min(suffix, target+delta)
            upper.append(suffix)
        upper.reverse()
        prefix, offset, placed = 0., 0., []
        for (index, plan, duration), span, target, bound in zip(spoken, spans, targets, upper):
            before, after = plan['pause_before_ms'] / 1000, plan['pause_after_ms'] / 1000
            offset = max(offset, 0., target-delta, min(target, bound))
            block_start = prefix + offset
            start = block_start + before
            end = block_start + span - after
            placed.append((index, dict(plan, mode='natural', start=start, end=end, available=end-start,
                                      scheduled_tempo=tempo, tempo_limit=AUTO_MAX_TEMPO, original_start=plan['start'],
                                      original_end=plan['end'])))
            prefix += span
        return placed

    def drift(placed):
        return max((abs(p['start'] - plans[i]['start']) for i, p in placed), default=0.)

    at_limit = arrange(AUTO_MAX_TEMPO)
    if at_limit is None:
        raw = sum(d for _, _, d in spoken)
        raise ValueError(f'Tổng lời đọc {raw:.1f} giây chưa vừa video {limit:.1f} giây ở giới hạn '
                         '1,25×; toàn bộ âm thanh đã được giữ trong cache, chưa cắt lời.')
    chosen = arrange(1.)
    # Prefer no extra compression when gaps already absorb the whole narration.
    if chosen is None or drift(chosen) > 2.:
        target = max(2., drift(at_limit))
        low, high = 1., AUTO_MAX_TEMPO
        for _ in range(32):
            mid = (low + high) / 2
            candidate = arrange(mid)
            if candidate is not None and drift(candidate) <= target + 1e-8:
                high = mid
            else:
                low = mid
        chosen = arrange(high)
    result = [dict(p) for p in plans]
    for index, plan in chosen:
        result[index] = plan
    return result
