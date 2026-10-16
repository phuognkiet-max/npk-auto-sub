"""Pure, language-neutral cleanup of OCR subtitle text before translation/TTS.

This module deliberately has no media, storage, model or provider imports. The
same canonical text is used by extraction preparation and the speech selector.
Source-only credits remain visible captions but are identified as non-dialogue.
"""
from __future__ import annotations

import re
import unicodedata


_HANDLE = re.compile(r'@[A-Za-z0-9_\u4e00-\u9fff.\-]+')
_CREDIT_LABEL = re.compile(
    r'(?:视频来源|文字来源|图片来源|画面来源|公文编号|来源|'
    r'(?<![\w])(?:Nguồn\s+(?:video|hình\s+ảnh|văn\s+bản)|'
    r'Video\s+source|Image\s+source|Text\s+source|Source|Credits?))\s*[:：]',
    re.IGNORECASE)


def _remove_unicode_handles(text):
    # Python re's \w excludes combining vowel marks in several scripts. Scan
    # Unicode categories as well as the explicit ASCII/Han handle contract so
    # @हिन्दी and @tài_khoản do not leave a misleading spoken suffix behind.
    output = []
    index = 0
    while index < len(text):
        if text[index] != '@':
            output.append(text[index])
            index += 1
            continue
        index += 1
        while index < len(text) and (unicodedata.category(text[index])[0] in ('L', 'M', 'N') or
                                     text[index] in '_.-\u200c\u200d'):
            index += 1
    return ''.join(output)


def _base_text(text):
    normalized = unicodedata.normalize('NFKC', str(text or ''))
    # Retain line boundaries so an attribution on a separate line cannot eat
    # the dialogue preceding/following it. Format/control glyphs are OCR noise.
    normalized = ''.join(c for c in normalized if c in '\n\r\t\u200c\u200d' or
                         unicodedata.category(c) not in ('Cc', 'Cf', 'Cs'))
    normalized = normalized.replace('\ufffd', '')
    normalized = _HANDLE.sub('', _remove_unicode_handles(normalized)).replace('@', '')
    return normalized


def subtitle_text_info(text):
    """Return cleaned caption plus role evidence without mutating caller data.

    A whole credit-only row keeps its cleaned label/value for display. A credit
    line attached to dialogue is removed; an inline label removes its suffix.
    Consumers must use ``credit_only`` to disable speech for the whole credit.
    """
    original = str(text or '')
    base = _base_text(original)
    dialogue, credits = [], []
    inline_credit = False
    for line in base.splitlines() or ['']:
        line = ' '.join(line.split())
        if not line:
            continue
        marker = _CREDIT_LABEL.search(line)
        if marker is None:
            dialogue.append(line)
            continue
        prefix = line[:marker.start()].rstrip(' \t|｜;；,，—–-:：([{【')
        if not any(c.isalnum() for c in prefix):
            prefix = ''
        if prefix:
            dialogue.append(prefix)
            inline_credit = True
        credits.append(line[marker.start():].strip())
    credit_only = bool(credits) and not dialogue
    cleaned = '\n'.join(credits if credit_only else dialogue).strip()
    return {'text': cleaned, 'credit_only': credit_only,
            'credit_removed': bool(credits) and not credit_only,
            'inline_credit': inline_credit,
            'handle_removed': '@' in unicodedata.normalize('NFKC', original),
            'empty': not cleaned,
            'changed': cleaned != original}


def sanitize_subtitle_text(text):
    """Remove all @ handles/stray @ and attached attribution text.

    Keep display-only credit captions; ``subtitle_text_info`` supplies their
    non-speaking role. Calling this function repeatedly is idempotent.
    """
    return subtitle_text_info(text)['text']


def subtitle_comparison_text(text):
    """Case-fold and remove whitespace/punctuation/symbol noise in any script."""
    clean = sanitize_subtitle_text(text).casefold()
    return ''.join(c for c in clean if unicodedata.category(c)[0] in ('L', 'M', 'N'))


def has_subtitle_content(text):
    """Accounts, punctuation/emoji and isolated marks alone are not dialogue."""
    return any(unicodedata.category(c)[0] in ('L', 'N') for c in sanitize_subtitle_text(text))


def is_non_dialogue_source(text):
    """Whether a source row contains only credit text or no speakable content.

    Credit captions stay visible in the subtitle table; callers use this signal
    only to keep them out of translation/TTS requests and mute their narration.
    """
    info = subtitle_text_info(text)
    return info['credit_only'] or not has_subtitle_content(info['text'])


def is_translation_noise(text):
    """Whether sanitizing a source row leaves no text to send to translation."""
    return not has_subtitle_content(text)


def is_han(character):
    point = ord(character)
    return (0x3400 <= point <= 0x4DBF or 0x4E00 <= point <= 0x9FFF or
            0xF900 <= point <= 0xFAFF or 0x20000 <= point <= 0x323AF)
