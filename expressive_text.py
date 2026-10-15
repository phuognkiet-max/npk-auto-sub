"""Opt-in nonverbal delivery preparation; displayed subtitles stay untouched.

Only isolated laughter utterances and narrow Vietnamese laughter prefixes are
recognized. This is a text rule, not video emotion recognition, and there is
no supported scream token to invent.
"""
from __future__ import annotations

import re


EXPRESSIVE_VERSION = 'npk-nonverbal-laughter-v2'
_LAUGHTER = re.compile(r'(?<!\w)(?P<syllable>hi|hì|ha)(?:[ \t]+(?P=syllable))+(?!\w)', re.I)
_TAGS = re.compile(r'\[[^\]\r\n]*\]|<\|[^>\r\n]*\|>')
_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’', '«': '»'}
_SENTENCE_END = '.!?…'


def _protected_ranges(text):
    """Protect tags and quoted examples, including an unfinished quotation."""
    ranges = [(match.start(), match.end()) for match in _TAGS.finditer(text)]
    start, closing = None, None
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == '\\':
            escaped = True
            continue
        if closing is not None:
            if char == closing:
                ranges.append((start, index + 1))
                start, closing = None, None
        elif char in _QUOTES:
            # An apostrophe inside a word is not a quotation opener.
            if char == "'" and index and index + 1 < len(text) and text[index - 1].isalnum() and text[index + 1].isalnum():
                continue
            start, closing = index, _QUOTES[char]
    if closing is not None:
        ranges.append((start, len(text)))
    return ranges


def prepare_expressive_text(text, enabled=False):
    """Map complete unquoted laughter utterances to the supported [cười] cue.

    A phrase must occupy a whole sentence/line, or be a Vietnamese laughter
    prefix before a comma. Unaccented "hi hi" before a comma is accepted only
    before "chào"; English greetings and embedded mentions are kept.
    Punctuation, spacing, every other word, and all figures remain verbatim.
    """
    if not isinstance(text, str):
        raise ValueError('Lời đọc cần là văn bản.')
    if type(enabled) is not bool:
        raise ValueError('Tùy chọn biểu cảm cần là bật hoặc tắt.')
    if not enabled:
        return text
    protected = _protected_ranges(text)

    def replacement(match):
        if any(match.start() < end and start < match.end() for start, end in protected):
            return match.group()
        left = text[:match.start()].rstrip(' \t')
        right = text[match.end():].lstrip(' \t')
        before = not left or left[-1] in _SENTENCE_END + '\r\n'
        after = not right or right[0] in _SENTENCE_END + '\r\n'
        syllable = match.group('syllable').casefold()
        if right.startswith(','):
            after = syllable in ('hì', 'ha') or bool(re.match(r',[ \t]*chào\b', right, re.I))
        return '[cười]' if before and after else match.group()

    return _LAUGHTER.sub(replacement, text)
