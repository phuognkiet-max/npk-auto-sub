"""User-owned pronunciation rules; only spoken text changes, never subtitles."""
from __future__ import annotations
import functools
import hashlib
import re
import unicodedata

MAX_DICTIONARY_CHARS = 20000
MAX_RULES = 256
_TAGS = re.compile(r'(\[[^\]\r\n]{1,80}\]|<\|[^>\r\n]+\|>)')


@functools.lru_cache(maxsize=32)
def parse_dictionary(document):
    if not isinstance(document, str) or len(document) > MAX_DICTIONARY_CHARS:
        raise ValueError('Từ điển cách đọc cần là văn bản không quá 20.000 ký tự.')
    rules, seen = [], set()
    for number, raw in enumerate(document.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith('#'):
            continue
        if line.count('=>') != 1:
            raise ValueError(f'Từ điển dòng {number}: dùng dạng chữ gốc => cách đọc.')
        written, spoken = (unicodedata.normalize('NFC', value.strip()) for value in line.split('=>', 1))
        if not written or not spoken or len(written) > 120 or len(spoken) > 240:
            raise ValueError(f'Từ điển dòng {number}: vế gốc cần 1–120, cách đọc 1–240 ký tự.')
        if any(char in written + spoken for char in '[]<>') or any(ord(char) < 32 for char in written + spoken):
            raise ValueError(f'Từ điển dòng {number}: không đặt thẻ cảm xúc, thẻ nghỉ hoặc ký tự điều khiển trong quy tắc.')
        key = written.casefold()
        if key in seen:
            raise ValueError(f'Từ điển dòng {number}: chữ gốc bị trùng, chỉ giữ một cách đọc.')
        seen.add(key)
        rules.append((written, spoken))
        if len(rules) > MAX_RULES:
            raise ValueError('Từ điển hỗ trợ tối đa 256 quy tắc.')
    return tuple(rules)


def validate_dictionary(document):
    """Validate and return a stable editable representation for API/settings."""
    return '\n'.join(f'{written} => {spoken}' for written, spoken in parse_dictionary(document))


def dictionary_identity(document=''):
    normalized = validate_dictionary(document)
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest() if normalized else ''


@functools.lru_cache(maxsize=32)
def _compiled(document):
    rules = sorted(parse_dictionary(document), key=lambda row: len(row[0]), reverse=True)
    branches, values = [], {}
    for index, (written, spoken) in enumerate(rules):
        name = f'r{index}'
        pattern = r'\s+'.join(re.escape(part) for part in written.split())
        if written[0].isalnum() or written[0] == '_':
            pattern = r'(?<!\w)' + pattern
        if written[-1].isalnum() or written[-1] == '_':
            pattern += r'(?!\w)'
        branches.append(f'(?P<{name}>{pattern})')
        values[name] = spoken
    return (re.compile('|'.join(branches), re.I), values) if branches else (None, {})


def apply_pronunciation(text, document=''):
    """Single-pass replacement avoids rule cascades; inline delivery tags survive."""
    if not isinstance(text, str):
        raise ValueError('Lời đọc cần là văn bản.')
    pattern, values = _compiled(document)
    if pattern is None:
        return text
    pieces = _TAGS.split(unicodedata.normalize('NFC', text))
    result = ''.join(piece if index % 2 else pattern.sub(lambda match: values[match.lastgroup], piece)
                     for index, piece in enumerate(pieces))
    if len(result) > 20000:
        raise ValueError('Lời đọc sau áp dụng từ điển quá dài. Rút ngắn đoạn hoặc cách đọc.')
    return result
