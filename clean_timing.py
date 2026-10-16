"""One time contract for still restoration, sample clips and output verification."""
import math


def video_span(media):
    duration = float(media.get('video_duration') or media['duration'])
    fps = float(media['fps'])
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0:
        raise ValueError('Thời lượng hoặc tốc độ khung hình của video không hợp lệ.')
    return min(duration, float(media['duration'])), fps


def checked_time(media, moment):
    moment = float(moment)
    if not math.isfinite(moment) or not 0 <= moment <= float(media['duration']):
        raise ValueError('Thời điểm xem thử nằm ngoài video.')
    return moment


def frame_time(media, moment):
    """Select the containing CFR frame, never seek past the last video frame."""
    duration, fps = video_span(media)
    moment = checked_time(media, moment)
    # Container audio padding of a fraction of a frame is not another picture.
    last = max(0, math.floor(duration * fps + 1e-5) - 1)
    return min(math.floor(moment * fps + 1e-7), last) / fps


def clip_window(media, moment, *, refresh=False):
    """Keep the requested position in a <=3s window, shifting back at EOF.

    Old projects only have container duration (often longer than the pictures).
    Workers and verifiers refresh stream timing, without updating the project.
    """
    requested = checked_time(media, moment)
    if refresh:
        from core import probe
        media = media | probe(media['path'])
    duration, fps = video_span(media)
    length = min(3., duration)
    start = math.floor(min(requested, max(0., duration - length)) * fps + 1e-5) / fps
    return start, min(length, duration - start)


def require_active_region(project, start, duration):
    if not any(float(r.get('start', 0)) < start + duration and
               float(r.get('end', project['media']['duration'])) > start for r in project['regions']):
        raise ValueError('Mẫu thử không có vùng xử lý đang hiệu lực. Tua đến vùng đã chọn rồi xem thử.')


def prepare_subtitle_segments(segments, *, stage='source', selected_indices=None):
    """Prepare a copy before translation/speech, with reversible change records.

    ``source`` cleans and physically merges OCR rows. ``speech`` (also accepted
    as ``translation``) retains every caption/timestamp and only disables repeat
    speech. Fuzzy similarity is explicitly the requested 0.80 policy, so unequal
    text mergers are flagged for the pre-render review rather than declared
    semantically equivalent. No file, cache, API or caller object is modified.
    """
    import copy
    import re
    from difflib import SequenceMatcher
    from subtitle_text import (has_subtitle_content, is_han, sanitize_subtitle_text,
                               subtitle_comparison_text, subtitle_text_info)

    if stage == 'translation':
        stage = 'speech'
    if stage not in ('source', 'speech') or not isinstance(segments, list):
        raise ValueError('Bước chuẩn bị phụ đề hoặc danh sách câu không hợp lệ.')
    if selected_indices is not None:
        if (not isinstance(selected_indices, (list, tuple, set)) or
                any(type(i) is not int or not 0 <= i < len(segments) for i in selected_indices)):
            raise ValueError('Phạm vi chuẩn bị phụ đề không hợp lệ.')
        selected_indices = set(selected_indices)
    result = {'version': 1, 'stage': stage, 'segments': [], 'changes': [],
              'input_count': len(segments), 'output_count': 0,
              'needs_review': False, 'review_reasons': [], 'source_indices': [],
              'removed_indices': []}
    records = []

    def change(kind, items, after=None, *, review=False, **extra):
        indices = sorted(i for item in items for i in item['indices'])
        if kind == 'protected_metadata_conflict' and any(
                item['kind'] == kind and item['indices'] == indices and
                item.get('operation') == extra.get('operation') for item in result['changes']):
            return
        entry = {'kind': kind, 'indices': indices,
                 'before': [copy.deepcopy(item['row']) for item in items], **extra}
        if after is not None:
            entry['after'] = copy.deepcopy(after)
        result['changes'].append(entry)
        if review:
            result['needs_review'] = True
            reason = {'kind': kind, 'indices': indices}
            if reason not in result['review_reasons']:
                result['review_reasons'].append(reason)

    for index, original in enumerate(segments):
        if not isinstance(original, dict):
            raise ValueError(f'Câu {index + 1}: dữ liệu phụ đề không hợp lệ.')
        row = copy.deepcopy(original)
        try:
            if isinstance(row.get('start'), bool) or isinstance(row.get('end'), bool):
                raise ValueError
            start, end = float(row['start']), float(row['end'])
            if not math.isfinite(start + end) or start < 0 or end <= start:
                raise ValueError
        except (KeyError, ValueError, TypeError, OverflowError):
            raise ValueError(f'Câu {index + 1}: mốc thời gian không hợp lệ.') from None
        source = subtitle_text_info(row.get('text'))
        chosen = (row['_speech_comparison_text'] if '_speech_comparison_text' in row else
                  next((row[k] for k in ('spoken_text', 'translated', 'text')
                        if str(row.get(k) or '').strip()), ''))
        selected = subtitle_text_info(chosen)
        row.pop('_speech_comparison_text', None)
        record = {'row': row, 'indices': [index], 'start': start, 'end': end,
                  'raw': str(original.get('text') or ''),
                  'selected': selected['text'], 'source': source,
                  'active': selected_indices is None or index in selected_indices}
        if stage == 'source' and record['active'] and source['text'] != str(row.get('text') or ''):
            before = copy.deepcopy(record)
            row['text'] = source['text']
            change('sanitize_text', [before], row)
        # A caption whose only information was an account handle is not speech.
        # A blank source with an explicit translated/edited script is still valid.
        source_only_noise = bool(str(original.get('text') or '').strip()) and (
            source['empty'] or not has_subtitle_content(source['text']))
        mute = source['credit_only'] or source_only_noise
        if stage == 'speech':
            # A missing selected translation is an error for the strict speech
            # selector, not permission to silently drop that dialogue cue.
            mute = mute or selected['credit_only'] or (bool(str(chosen or '').strip()) and
                (selected['empty'] or not has_subtitle_content(selected['text'])))
        if record['active'] and mute and row.get('speech_enabled', True) is not False:
            before = copy.deepcopy(record)
            row['speech_enabled'] = False
            change('credit_only' if source['credit_only'] or selected['credit_only']
                   else 'empty_speech', [before], row)
        if stage == 'source' and record['active'] and source['empty']:
            protected_empty = any(row.get(key) not in (None, '', False, 0) for key in (
                'translated', 'spoken_text', 'voice_id', 'speech_rate', 'character_name',
                'speaker', 'pause_before_ms', 'pause_after_ms', 'subtitle_style',
                'manual', 'edited', 'user_edited', 'manual_edit', 'text_edited', 'reviewed'))
            before = dict(record, row=copy.deepcopy(original))
            if not protected_empty:
                # An empty OCR account overlay is not a translatable caption.
                # Never send blank input to a provider or invent replacement
                # words just to keep a meaningless cue in the timeline.
                change('empty_source_removed', [before], [])
                result['removed_indices'].append(index)
                continue
            change('protected_empty_source', [before], row, review=True)
        records.append(record)

    protected = ('voice_id', 'speech_rate', 'character_name', 'speaker',
                 'pause_before_ms', 'pause_after_ms', 'subtitle_style', 'speech_enabled')

    def conflict(left, right, *, speech=False, orphan=False):
        a, b = left['row'], right['row']
        # Authored repetitions may be intentional. Never collapse a manual
        # cue or suppress its neighbour as an automatic deduplication side effect.
        if a.get('user_edited') is True or b.get('user_edited') is True:
            return True
        if str(a.get('spoken_text') or '').strip() or str(b.get('spoken_text') or '').strip():
            return True
        if any(a.get(key) != b.get(key) for key in protected):
            return True
        if not speech:
            # A partial/different translation cannot safely be attached to a new
            # source unit. It must remain recoverable and separately reviewed.
            ta, tb = str(a.get('translated') or '').strip(), str(b.get('translated') or '').strip()
            if orphan and (ta or tb):
                return True
            if ta != tb and (ta or tb):
                return True
        return False

    def comparable(item):
        value = item['selected'] if stage == 'speech' else item['row'].get('text')
        return subtitle_comparison_text(value)

    def nearby(left, right, max_gap, max_span):
        return (right['start'] >= left['start'] and
                right['start'] - left['end'] <= max_gap + 1e-9 and
                max(left['end'], right['end']) - min(left['start'], right['start']) <= max_span + 1e-9)

    def duplicate(left, right):
        a, b = comparable(left), comparable(right)
        if not a or not b or not nearby(left, right, 1.5, 12.):
            return None
        ratio = SequenceMatcher(None, a, b, autojunk=False).ratio()
        return ratio if ratio >= .8 or a in b or b in a else None

    def speaking(item):
        return (item['active'] and item['row'].get('speech_enabled', True) is not False
                and bool(comparable(item)))

    def best_text(left, right):
        def score(item):
            raw = item['raw']
            cleaned = str(item['row'].get('text') or '')
            return (int('@' in raw) + raw.count('\ufffd'), -len(subtitle_comparison_text(cleaned)))
        return min((left, right), key=score)['row'].get('text', '')

    def merge(left, right, kind, ratio=None):
        row = copy.deepcopy(left['row'])
        row.update(start=min(left['start'], right['start']), end=max(left['end'], right['end']))
        if kind == 'duplicate_merge':
            row['text'] = best_text(left, right)
        else:
            a, b = str(left['row'].get('text') or '').rstrip(), str(right['row'].get('text') or '').lstrip()
            separator = '' if a and b and is_han(a[-1]) and is_han(b[0]) else ' '
            row['text'] = a + separator + b
        # Token/word timings describe the old caption and must be rebuilt. The
        # exact originals remain in the reversible audit, including metadata.
        for key in ('words', 'word_timings', 'word_timestamps'):
            row.pop(key, None)
        unequal = comparable(left) != comparable(right)
        change(kind, [left, right], row, review=kind == 'orphan_merge' or unequal,
               **({'similarity': ratio} if ratio is not None else {}))
        return {'row': row, 'indices': left['indices'] + right['indices'],
                'start': row['start'], 'end': row['end'], 'raw': str(row['text']),
                'selected': '', 'source': subtitle_text_info(row['text']), 'active': True}

    if stage == 'speech':
        anchor = None
        for item in records:
            if not speaking(item):
                # Keep a suppressed duplicate as adjacency evidence, but a
                # credit/explicitly disabled cue is a dialogue boundary.
                anchor = None
                continue
            ratio = duplicate(anchor, item) if anchor is not None else None
            if ratio is not None:
                if conflict(anchor, item, speech=True):
                    change('protected_metadata_conflict', [anchor, item], review=True,
                           operation='duplicate_speech')
                    anchor = item
                    continue
                before = copy.deepcopy(item)
                item['row']['speech_enabled'] = False
                change('duplicate_speech', [before], item['row'],
                       review=comparable(anchor) != comparable(item), similarity=ratio,
                       keep_indices=anchor['indices'])
                # Span measured from the first retained cue; adjacency gap from
                # the last duplicate. This suppresses all three rows in A/A/A.
                anchor = dict(anchor, end=max(anchor['end'], item['end']))
            else:
                anchor = item
    else:
        # Reach a fixed point so rerunning cleanup cannot merge more fragments.
        changed = True
        while changed:
            changed = False
            index = 1
            while index < len(records):
                left, right = records[index - 1:index + 1]
                ratio = duplicate(left, right) if speaking(left) and speaking(right) else None
                if ratio is not None:
                    if conflict(left, right):
                        change('protected_metadata_conflict', [left, right], review=True,
                               operation='duplicate_merge')
                    else:
                        records[index - 1:index + 1] = [merge(left, right, 'duplicate_merge', ratio)]
                        changed = True
                        continue
                index += 1

            def orphan(item):
                text = sanitize_subtitle_text(item['row'].get('text'))
                letters = [c for c in text if c.isalpha()]
                # No-space East Asian scripts must not become a one-word orphan
                # merely because Python split() cannot tokenize them.
                if any(is_han(c) for c in letters):
                    short_text = sum(is_han(c) for c in letters) <= 3 and len(letters) <= 3
                elif any('\u3040' <= c <= '\u30ff' or '\u0e00' <= c <= '\u0e7f' for c in letters):
                    short_text = len(letters) <= 3
                else:
                    short_text = len(re.findall(r'\w+', text, flags=re.UNICODE)) < 3
                return item['end'] - item['start'] < 1. or short_text

            index = 0
            while index < len(records):
                current = records[index]
                if not speaking(current) or not orphan(current):
                    index += 1
                    continue
                candidates = []
                for neighbor in (index - 1, index + 1):
                    if not 0 <= neighbor < len(records) or not speaking(records[neighbor]):
                        continue
                    first, second = (records[neighbor], current) if neighbor < index else (current, records[neighbor])
                    if nearby(first, second, .35, 6.):
                        if conflict(first, second, orphan=True):
                            change('protected_metadata_conflict', [first, second], review=True,
                                   operation='orphan_merge')
                        else:
                            candidates.append((max(0., second['start'] - first['end']), neighbor))
                if not candidates:
                    index += 1
                    continue
                _, neighbor = min(candidates)
                begin = min(index, neighbor)
                records[begin:begin + 2] = [merge(*records[begin:begin + 2], 'orphan_merge')]
                changed = True
                index = max(0, begin - 1)

    result['segments'] = [item['row'] for item in records]
    result['source_indices'] = [item['indices'] for item in records]
    result['output_count'] = len(records)
    return result
