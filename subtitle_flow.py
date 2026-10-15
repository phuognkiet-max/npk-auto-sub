"""Paginate complete subtitles into one measured line without rewriting speech."""
from __future__ import annotations

from functools import lru_cache
import math
import os
from pathlib import Path
import re


@lru_cache(maxsize=64)
def font_path(name, bold=False):
    """Find an installed font using read-only Windows font registration."""
    windows = Path(os.environ.get('WINDIR', r'C:\Windows'))
    user_fonts = Path(os.environ.get('LOCALAPPDATA', r'C:\Users\developer\AppData\Local')) / 'Microsoft/Windows/Fonts'
    directories = (windows / 'Fonts', user_fonts)
    candidates = []
    selected = str(name).strip().casefold()
    try:
        import winreg
        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, r'SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts') as key:
                    for index in range(winreg.QueryInfoKey(key)[1]):
                        label, value, _ = winreg.EnumValue(key, index)
                        if not isinstance(value, str):
                            continue
                        labels = [part.strip().casefold() for part in label.split(' (')[0].split(' & ')]
                        for family in labels:
                            plain = re.sub(r'\s+(?:bold|italic|regular|oblique)(?:\s+.*)?$', '', family)
                            if selected not in (family, plain):
                                continue
                            score = (20 if ('bold' in family) == bool(bold) else 0) + (5 if 'italic' not in family else 0)
                            score += 10 if family == selected else 0
                            path = Path(os.path.expandvars(value))
                            locations = [path] if path.is_absolute() else [directory / path for directory in directories]
                            for location in locations:
                                if location.is_file():
                                    candidates.append((score, str(location)))
            except OSError:
                continue
    except ImportError:
        pass
    if candidates:
        return max(candidates, key=lambda candidate: candidate[0])[1]
    # Known installed fallbacks also cover sparse font registration. Unknown
    # requested families are already reported by subtitle_fonts.font_warnings.
    for filename in (('arialbd.ttf' if bold else 'arial.ttf'), ('segoeuib.ttf' if bold else 'segoeui.ttf')):
        for directory in directories:
            path = directory / filename
            if path.is_file():
                return str(path)
    return None


@lru_cache(maxsize=256)
def _font(path, size):
    from PIL import ImageFont
    return ImageFont.truetype(path, size=size)


def text_width(text, font_size, params):
    """Measure the selected installed face; retain a safe unavailable-font fallback."""
    size = max(.05, float(font_size))
    path = font_path(params.get('font', 'Arial'), bool(params.get('subtitle_bold', False)))
    if path:
        integer_size = max(1, math.ceil(size))
        try:
            return float(_font(path, integer_size).getlength(text)) * size / integer_size
        except (OSError, ValueError):
            pass
    import unicodedata
    units = sum(1.05 if unicodedata.east_asian_width(character) in ('W', 'F') else .8
                for character in text)
    return units * size


def literal_caption(text):
    """Keep user text visible without allowing embedded ASS override commands."""
    return str(text).replace('\\', '＼').replace('{', '｛').replace('}', '｝')


def available_width(params, media):
    size = float(params.get('font_size', 46))
    margin = max(0., float(params.get('subtitle_margin_x', 40)))
    outline = max(0., float(params.get('outline', 3)))
    shadow = max(0., float(params.get('subtitle_shadow', 0)))
    safety = max(8., size * .2)
    available = max(1., float(media['width']) - margin * 2 - (outline + shadow) * 2 - safety)
    return available / 1.06 if params.get('subtitle_preset') == 'pop' else available


def _ass_tick(seconds):
    return (round(float(seconds) * 1000) + 5) // 10


def caption_chunks(row, text, params, media, measure=None):
    """Return display pages with absolute word times; input data is never edited.

    ``measure(text, font_size)`` optionally supplies pixel widths. Every token is
    retained in order, normally at the chosen font size. A word too wide to fit
    lowers the cue's consistent display font size instead of splitting letters.
    Legacy/missing ``subtitle_flow`` keeps the existing wrapped whole cue.
    """
    from subtitle_layout import wrap_caption
    from subtitle_presets import highlight_word_timings
    try:
        start, end = float(row['start']), float(row['end'])
    except (KeyError, TypeError, ValueError, OverflowError):
        return []
    if not math.isfinite(start + end) or start < 0 or end <= start or not str(text).strip():
        return []
    size = max(.05, float(params.get('font_size', 46)))
    if params.get('subtitle_flow', 'wrap') != 'single':
        displayed = wrap_caption(str(text), params)
        timing = highlight_word_timings(row, displayed)
        return [{'start': start, 'end': end, 'text': displayed,
                 'caption_words': timing['words'], 'word_timing_source': timing['source'] if timing['source'] != 'static' else 'estimated',
                 'font_size': size, 'word_start': 0, 'word_end': len(re.findall(r'\S+', displayed))}]

    tokens = re.findall(r'\S+', str(text))
    minimum_size = min(size, 8.)
    displayed = ' '.join(tokens)
    timing = highlight_word_timings(row, displayed)
    words = timing['words']
    try:
        limit = max(2, min(12, int(params.get('subtitle_chunk_words', 6))))
    except (TypeError, ValueError, OverflowError):
        limit = 6
    width = available_width(params, media)
    raw_measure = measure or (lambda value, selected_size: text_width(value, selected_size, params))
    measuring = lambda value, selected_size: raw_measure(literal_caption(value), selected_size)
    longest = max(float(measuring(token, size)) for token in tokens)
    if longest > width:
        size = max(minimum_size, size * width / longest * .98)

    def fits(a, b):
        return float(measuring(' '.join(tokens[a:b]), size)) <= width

    ranges, a = [], 0
    while a < len(tokens):
        b = a + 1
        while b < len(tokens) and b - a < limit and fits(a, b + 1):
            b += 1
        # Finish at a useful clause boundary, while avoiding a trailing one-word
        # page. Punctuation stays attached to its original token.
        candidates = [index for index in range(a + 2, b + 1)
                      if re.search(r'[.!?;:,…][\"\'”’)]*$', tokens[index - 1])
                      and len(tokens) - index >= 2]
        if candidates:
            b = candidates[-1]
        ranges.append((a, b))
        a = b
    if len(ranges) >= 2 and ranges[-1][1] - ranges[-1][0] == 1:
        a, b = ranges[-2]
        _, c = ranges[-1]
        if c - a <= limit and fits(a, c):
            ranges[-2:] = [(a, c)]
        elif b - a > 2 and fits(b - 1, c):
            ranges[-2:] = [(a, b - 1), (b - 1, c)]

    def boundary(index):
        if index == 0:
            return start
        if index >= len(tokens):
            return end
        return words[index]['start'] if words else start + (end - start) * index / len(tokens)

    # A millisecond-scale cue may have boundaries collapsing to the same ASS
    # centisecond. Merge such pages rather than silently dropping their words.
    index = 0
    while index < len(ranges):
        a, b = ranges[index]
        if _ass_tick(boundary(b)) <= _ass_tick(boundary(a)) and len(ranges) > 1:
            if index + 1 < len(ranges):
                ranges[index:index + 2] = [(a, ranges[index + 1][1])]
            else:
                ranges[index - 1:index + 1] = [(ranges[index - 1][0], b)]
                index -= 1
            continue
        index += 1
    widest_page = max(float(measuring(' '.join(tokens[a:b]), size)) for a, b in ranges)
    if widest_page > width:
        size = max(minimum_size, size * width / widest_page * .98)
    pages = []
    for a, b in ranges:
        page_start, page_end = boundary(a), boundary(b)
        pages.append({'start': page_start, 'end': page_end, 'text': ' '.join(tokens[a:b]),
                      'display_text': literal_caption(' '.join(tokens[a:b])),
                      'caption_words': [dict(word) for word in words[a:b]],
                      'word_timing_source': timing['source'] if timing['source'] != 'static' else 'estimated', 'font_size': size,
                      'layout_overflow': float(measuring(' '.join(tokens[a:b]), size)) > width,
                      'word_start': a, 'word_end': b})
    return pages
