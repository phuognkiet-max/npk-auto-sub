"""Local, explicit review of dialogue candidates; never rewrite OCR or captions.

Build returns suggestions only. Apply returns a deep copy of the full project,
and can only disable speech on the chosen, current suggestions. No provider,
storage, media decoder, inference or fuzzy text comparison is used here.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
import unicodedata

from speech_delivery import spoken_text

VERSION = 1
MAX_GROUP_SECONDS = 12.0
MAX_TOUCH_GAP = .08
_CREDIT = re.compile(r'^(?:视频来源|画面来源|Nguồn video|Nguồn hình ảnh)\s*[:：]\s*\S', re.IGNORECASE)
_HANDLE_SUFFIX = re.compile(r'\s+@\w[\w.-]{0,79}$')


def _digest(value):
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise ValueError('Dữ liệu rà soát lời đọc không hợp lệ; chưa thay đổi dự án.') from None
    return hashlib.sha256(encoded).hexdigest()


def _canonical(value, *, strip_handle=False):
    text = ' '.join(unicodedata.normalize('NFKC', str(value or '')).split())
    return _HANDLE_SUFFIX.sub('', text).rstrip() if strip_handle else text


def _signature(project):
    return _digest({'version': VERSION, 'project_id': project.get('id'),
        'segments': project.get('segments', []), 'params': project.get('params', {}),
        'updated': project.get('updated'), 'source_revision': project.get('source_revision'),
        'coverage': project.get('coverage', {})})


def _enough_text(text):
    # Short exclamations/names and number-only captions are especially likely
    # to be intentional repetition. These proposals deliberately omit them.
    return sum(c.isalnum() for c in text) >= 8 and sum(c.isalpha() for c in text) >= 4


def _delivery_identity(row, params):
    rate = row.get('speech_rate')
    if rate in (None, ''):
        rate = params.get('voice_speed', 1)
    if type(rate) not in (int, float) or not math.isfinite(rate) or not .5 <= rate <= 2:
        return None
    pauses = [row.get(key, 0) for key in ('pause_before_ms', 'pause_after_ms')]
    if any(type(pause) not in (int, float) or not math.isfinite(pause) or
           not float(pause).is_integer() or not 0 <= pause <= 2000 for pause in pauses):
        return None
    return (row.get('voice_id') or params.get('voice_id'), rate,
            row.get('character_name') or '', *pauses)


def _candidate(row, params):
    if not isinstance(row, dict) or row.get('speech_enabled', True) is not True:
        return None
    # A user's separate spoken script is never silently replaced/suppressed,
    # even when its text source is not currently active.
    if str(row.get('spoken_text') or '').strip():
        return None
    start, end = row.get('start'), row.get('end')
    if (type(start) not in (int, float) or type(end) not in (int, float) or
            not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end):
        return None
    delivery = _delivery_identity(row, params)
    source = _canonical(row.get('text'), strip_handle=True)
    effective = _canonical(spoken_text(row, params), strip_handle=True)
    if delivery is None or not _enough_text(source) or not _enough_text(effective):
        return None
    return {'start': start, 'end': end, 'source': source, 'effective': effective,
            'delivery': delivery,
            'overlay_suffix': _canonical(row.get('text')) != source or
                              _canonical(spoken_text(row, params)) != effective}


def build_dialogue_review(project):
    """Return revision-bound preview proposals; do not modify ``project``."""
    if not isinstance(project, dict) or not isinstance(project.get('segments', []), list):
        raise ValueError('Thiếu bảng phụ đề hợp lệ để rà soát lời đọc.')
    signature = _signature(project)
    rows, params = project.get('segments', []), project.get('params', {})
    if not isinstance(params, dict):
        raise ValueError('Cấu hình lời đọc không hợp lệ; chưa thay đổi dự án.')
    result = {'signature': signature, 'expected_revision': project.get('updated'),
              'proposals': [], 'issues': []}
    coverage = project.get('coverage') or {}
    is_ocr = isinstance(coverage, dict) and coverage.get('method') == 'ocr'

    def propose(kind, indices, changes, message, **extra):
        payload = {'kind': kind, 'indices': indices, 'changes': changes, **extra}
        identifier = _digest({'signature': signature, **payload})
        result['proposals'].append({'id': identifier, **payload, 'message': message})

    candidates = [_candidate(row, params) for row in rows] if is_ocr else [None] * len(rows)
    index = 0
    while index < len(rows):
        first = candidates[index]
        if first is None:
            index += 1
            continue
        end = index + 1
        while end < len(rows):
            previous, current = candidates[end - 1], candidates[end]
            if (current is None or current['source'] != first['source'] or
                    current['effective'] != first['effective'] or current['delivery'] != first['delivery'] or
                    not -1e-8 <= current['start'] - previous['end'] <= MAX_TOUCH_GAP + 1e-8 or
                    current['end'] - first['start'] > MAX_GROUP_SECONDS + 1e-8):
                break
            end += 1
        if end - index > 1:
            indices = list(range(index, end))
            keep = next((i for i in indices if not candidates[i]['overlay_suffix']), None)
            # Do not retain a polluted utterance when every candidate contains
            # a suffix: manual script review is required in that case.
            if keep is not None:
                propose('duplicate_group', indices,
                    [{'index': i, 'speech_enabled': False} for i in indices if i != keep],
                    f'Câu {index + 1}–{end}: cùng lời gốc và lời đọc sau chuẩn hóa, nằm liền nhau. '
                    f'Đề nghị chỉ đọc câu {keep + 1}; nghe và xác nhận vì lời lặp có thể là chủ ý. '
                    'Phụ đề, nội dung và mốc thời gian vẫn được giữ nguyên.', keep_index=keep)
        index = end

    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        source = _canonical(row.get('text'))
        custom = str(row.get('spoken_text') or '').strip()
        if (is_ocr and row.get('speech_enabled', True) is True and not custom and _CREDIT.match(source)):
            propose('source_credit', [index], [{'index': index, 'speech_enabled': False}],
                f'Câu {index + 1}: có nhãn nguồn video/hình ảnh ở đầu câu. '
                'Đề nghị không đọc dòng nguồn; xem nội dung rồi xác nhận. Vẫn giữ phụ đề và mốc gốc.')
        if any('@' in str(row.get(key) or '') for key in ('text', 'translated', 'spoken_text')):
            result['issues'].append({'code': 'attribution_or_handle', 'index': index,
                'message': f'Câu {index + 1}: có ký tự @, có thể là tài khoản, email hoặc chữ ngoài lời thoại. '
                           'Cần xem lời đọc; không tự xóa ký tự hay nội dung.'})
    if not is_ocr:
        result['issues'].append({'code': 'ocr_evidence_required',
            'message': 'Không có coverage OCR: không đề xuất bỏ lời trùng hoặc chữ nguồn theo hình ảnh.'})
    return result


def apply_dialogue_review(project, signature, proposal_ids):
    """Return a deep copy of the full project, disabling only selected speech.

    The revision and complete input are bound into ``signature``; stale or
    unknown choices fail before constructing any applied change.
    """
    review = build_dialogue_review(project)
    if (not isinstance(signature, str) or not re.fullmatch(r'[0-9a-f]{64}', signature) or
            not hmac.compare_digest(signature, review['signature'])):
        raise ValueError('Bản rà soát đã cũ hoặc dự án đã đổi. Tải lại đề xuất trước khi áp dụng.')
    if (not isinstance(proposal_ids, list) or not proposal_ids or
            any(not isinstance(value, str) for value in proposal_ids) or
            len(proposal_ids) != len(set(proposal_ids))):
        raise ValueError('Chọn ít nhất một đề xuất hiện tại, không chọn lặp.')
    available = {proposal['id']: proposal for proposal in review['proposals']}
    if any(identifier not in available for identifier in proposal_ids):
        raise ValueError('Đề xuất không có trong bản rà soát hiện tại; chưa thay đổi dự án.')
    result = copy.deepcopy(project)
    for identifier in proposal_ids:
        for change in available[identifier]['changes']:
            result['segments'][change['index']]['speech_enabled'] = False
    return result
