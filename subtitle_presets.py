"""Subtitle presets shared by the saved parameters and the ASS renderer."""
from __future__ import annotations

import re
import math


PRESETS = {
    'clean': {'font': 'Arial', 'font_size': 46, 'font_color': '#FFFFFF',
              'outline_color': '#000000', 'outline': 3, 'subtitle_shadow': 0,
              'margin': 40, 'subtitle_background': False, 'subtitle_bold': False,
              'subtitle_alignment': 'bottom', 'subtitle_karaoke': False},
    'pop': {'font': 'Arial', 'font_size': 54, 'font_color': '#FFD84D',
            'outline_color': '#000000', 'outline': 4, 'subtitle_shadow': 2,
            'margin': 48, 'subtitle_background': False, 'subtitle_bold': True,
            'subtitle_alignment': 'bottom', 'subtitle_karaoke': False},
    'highlight': {'font': 'Arial', 'font_size': 48, 'font_color': '#FFFFFF',
                  'outline_color': '#000000', 'outline': 3, 'subtitle_shadow': 0,
                  'margin': 44, 'subtitle_background': True, 'subtitle_bold': True,
                  'subtitle_alignment': 'bottom', 'subtitle_karaoke': False},
    'karaoke': {'font': 'Arial', 'font_size': 48, 'font_color': '#53E0E4',
                'outline_color': '#000000', 'outline': 3, 'subtitle_shadow': 1,
                'margin': 44, 'subtitle_background': False, 'subtitle_bold': True,
                'subtitle_alignment': 'bottom', 'subtitle_karaoke': True},
}


def apply_subtitle_preset(params, preset=None):
    """Return a copy with preset values; caller may then apply deliberate edits."""
    selected = params.get('subtitle_preset') if preset is None else preset
    if selected not in PRESETS:
        raise ValueError('Kiểu phụ đề không hợp lệ. Chọn Clean, Pop, Highlight hoặc Karaoke.')
    return dict(params) | PRESETS[selected] | {'subtitle_preset': selected}


def highlight_word_timings(row, displayed):
    """Return bounded display-word intervals and their honest timing source.

    ``words`` use absolute seconds. Recognition of another language/text cannot
    drive the translated words: every whitespace token must match in order after
    punctuation normalization. Missing/mismatched timings get a presentation
    estimate, never newly invented recognition metadata on the saved row.
    """
    from karaoke import normalized
    tokens = re.findall(r'\S+', displayed)
    empty = {'source': 'static', 'words': []}
    try:
        cue_start, cue_end = float(row['start']), float(row['end'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return empty
    if not tokens or not math.isfinite(cue_start + cue_end) or cue_end <= cue_start:
        return empty
    # Keep source-language recognition separate from the rendered speech track.
    supplied = (row.get('caption_words') if 'caption_words' in row else row.get('words')) or []
    matched = isinstance(supplied, list) and len(supplied) == len(tokens)
    intervals = []
    if matched:
        try:
            for token, word in zip(tokens, supplied):
                if normalized(token) != normalized(str(word.get('word', ''))):
                    matched = False
                    break
                start, end = float(word['start']), float(word['end'])
                if not math.isfinite(start + end) or end <= start:
                    matched = False
                    break
                start, end = max(cue_start, start), min(cue_end, end)
                if end <= start or (intervals and start < intervals[-1]['start']):
                    matched = False
                    break
                intervals.append({'word': token, 'start': start, 'end': end})
            if matched:
                for index in range(len(intervals) - 1):
                    intervals[index]['end'] = min(intervals[index]['end'], intervals[index + 1]['start'])
                    if intervals[index]['end'] <= intervals[index]['start']:
                        matched = False
                        break
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
            matched = False
    if matched:
        return {'source': 'estimated' if row.get('word_timing_source') == 'estimated' else 'aligned',
                'words': intervals}

    # Allocate integer milliseconds deterministically, preserving even very
    # short words. The UI uses the same largest-remainder estimate.
    total = round((cue_end - cue_start) * 1000)
    if total < len(tokens):
        return empty
    weights = [max(1, len(normalized(token))) for token in tokens]
    weight_sum, remaining = sum(weights), total - len(tokens)
    numerators = [remaining * weight for weight in weights]
    durations = [1 + numerator // weight_sum for numerator in numerators]
    leftover = total - sum(durations)
    order = sorted(range(len(tokens)), key=lambda index: (-(numerators[index] % weight_sum), index))
    for index in order[:leftover]:
        durations[index] += 1
    elapsed, intervals = 0, []
    for token, duration in zip(tokens, durations):
        start = cue_start + elapsed / 1000
        elapsed += duration
        intervals.append({'word': token, 'start': start, 'end': min(cue_end, cue_start + elapsed / 1000)})
    return {'source': 'estimated', 'words': intervals}


def highlight_caption(row, displayed, params):
    """One ASS event; only the currently spoken word takes the accent color.

    Each word resets to the chosen base color at its end, including pauses. This
    avoids karaoke's accumulated fill and avoids overlapping ASS events whose
    backgrounds/layout can jump. One-millisecond transforms act as color steps.
    """
    timing = highlight_word_timings(row, displayed)
    if not timing['words']:
        return displayed.replace('{', '｛').replace('}', '｝').replace('\n', r'\N')
    value = str(params.get('font_color', '#FFFFFF'))
    if not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
        value = '#FFFFFF'
    base = r'\1c&H' + value[5:7] + value[3:5] + value[1:3] + '&'
    accent = r'\1c&H4DD8FF&'
    cue_start = float(row['start'])
    total = max(1, round((float(row['end']) - cue_start) * 1000))
    parts, index = [], 0
    for token in re.findall(r'\S+|\s+', displayed):
        if token.isspace():
            parts.append(token.replace('\n', r'\N'))
            continue
        word = timing['words'][index]
        index += 1
        start = max(0, min(total - 1, round((word['start'] - cue_start) * 1000)))
        end = max(start + 1, min(total, round((word['end'] - cue_start) * 1000)))
        effects = accent if start == 0 else base + rf'\t({start},{start + 1},{accent})'
        if end < total:
            effects += rf'\t({max(start, end - 1)},{end},{base})'
        parts.append('{' + effects + '}' + token.replace('{', '｛').replace('}', '｝') + '{' + base + '}')
    return ''.join(parts)


def caption_effects(row, displayed, params):
    """ASS presentation only; translated SRT and the saved words stay intact."""
    caption = displayed.replace('\n', r'\N')
    preset = params.get('subtitle_preset')
    if preset == 'highlight':
        return highlight_caption(row, displayed, params)
    if params.get('subtitle_karaoke'):
        from karaoke import estimated_caption, timed_caption
        timing_row = row | {'words': row.get('caption_words')} if 'caption_words' in row else row
        timed = timed_caption(timing_row, displayed)
        if timed is None and preset == 'karaoke':
            timed = estimated_caption(row, displayed)
        caption = timed or caption
    if preset == 'pop':
        return r'{\fad(70,90)\fscx106\fscy106\t(0,140,\fscx100\fscy100)}' + caption
    if preset == 'karaoke':
        caption = r'{\fad(45,70)}' + caption
    return caption
