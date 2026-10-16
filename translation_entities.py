"""Bounded, evidence-backed terminology carried across translation batches.

No independent NER/model call is made. Optional entities in the same provider
response must occur in both the accepted source and translation. The fallback
only records visibly unchanged multiword proper names; it never invents a name
translation from script-specific guesses.
"""
from __future__ import annotations

import copy
import re
import unicodedata


MAX_ENTITIES = 256
MAX_ENTITY_TEXT = 120
MAX_CONFIRMATIONS = 50
_PROPER_NAME = re.compile(r'(?<!\w)([A-ZÀ-ÖØ-ÞĐ][^\W\d_]*(?:[-\x27’][^\W\d_]+)*'
                          r'(?:[ \t]+[A-ZÀ-ÖØ-ÞĐ][^\W\d_]*(?:[-\x27’][^\W\d_]+)*){1,3})(?!\w)', re.UNICODE)


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _valid_text(value):
    return (isinstance(value, str) and 1 <= len(value.strip()) <= MAX_ENTITY_TEXT and
            not any(unicodedata.category(c)[0] == 'C' for c in value))


def _contains(text, term):
    """Do not bind Ann to Joanna; Han names can occur in unspaced prose."""
    text, term = _key(text), _key(term)
    if not term:
        return False
    han = any('\u3400' <= c <= '\u9fff' or '\U00020000' <= c <= '\U000323af' for c in term)
    if han:
        return term in text
    def joined(c):
        return bool(c) and unicodedata.category(c)[0] in ('L', 'M', 'N')
    offset = 0
    while (index := text.find(term, offset)) >= 0:
        before = text[index - 1] if index else ''
        after = text[index + len(term)] if index + len(term) < len(text) else ''
        if not (joined(term[0]) and joined(before)) and not (joined(term[-1]) and joined(after)):
            return True
        offset = index + 1
    return False


def empty_glossary():
    return {'version': 1, 'entries': []}


def validate_entity_glossary(glossary, rows):
    """Reject corrupt/unanchored terminology rather than trust cached prose."""
    if glossary is None:
        return empty_glossary()
    if (not isinstance(glossary, dict) or glossary.get('version') != 1 or
            not isinstance(glossary.get('entries'), list) or len(glossary['entries']) > MAX_ENTITIES):
        raise ValueError('Từ điển thực thể trong checkpoint không hợp lệ; giữ dữ liệu và kiểm tra trước khi dịch tiếp.')
    seen = set()
    for entry in glossary['entries']:
        if (not isinstance(entry, dict) or not _valid_text(entry.get('source')) or
                not _valid_text(entry.get('target')) or entry.get('origin') not in ('provider', 'same_spelling') or
                type(entry.get('first_id')) is not int or
                not isinstance(entry.get('confirmed_ids'), list) or
                not 1 <= len(entry['confirmed_ids']) <= MAX_CONFIRMATIONS):
            raise ValueError('Mục từ thực thể trong checkpoint không hợp lệ.')
        ids = entry['confirmed_ids']
        if (any(type(i) is not int or not 0 <= i < len(rows) for i in ids) or len(set(ids)) != len(ids) or
                entry['first_id'] not in ids or _key(entry['source']) in seen):
            raise ValueError('Bằng chứng từ điển thực thể không còn khớp câu phụ đề.')
        seen.add(_key(entry['source']))
        if any(not _contains(str(rows[i].get('text') or ''), entry['source']) or
               not _contains(str(rows[i].get('translated') or ''), entry['target']) for i in ids):
            raise ValueError('Bản dịch không còn chứa thực thể đã xác nhận; không dùng từ điển cũ.')
    return copy.deepcopy(glossary)


def update_entity_glossary(glossary, rows, accepted_indices, proposals=None):
    result = validate_entity_glossary(glossary, rows)
    warnings = []
    accepted = list(accepted_indices)
    candidates = []
    if proposals is not None:
        if not isinstance(proposals, list) or len(proposals) > MAX_ENTITIES:
            warnings.append('Bỏ qua phần thực thể trả về sai cấu trúc; các câu dịch hợp lệ vẫn được lưu.')
        else:
            candidates.extend((entry, 'provider') for entry in proposals)
    for index in accepted:
        source, target = str(rows[index].get('text') or ''), str(rows[index].get('translated') or '')
        for match in _PROPER_NAME.finditer(source):
            name = match.group(1).strip()
            if _contains(target, name):
                candidates.append(({'source': name, 'target': name}, 'same_spelling'))
    existing = {_key(entry['source']): entry for entry in result['entries']}
    for proposed, origin in candidates:
        if (not isinstance(proposed, dict) or not _valid_text(proposed.get('source')) or
                not _valid_text(proposed.get('target'))):
            continue
        source, target = proposed['source'].strip(), proposed['target'].strip()
        evidence = [i for i in accepted if _contains(str(rows[i].get('text') or ''), source) and
                    _contains(str(rows[i].get('translated') or ''), target)]
        if not evidence:
            continue
        key = _key(source)
        if key in existing:
            item = existing[key]
            if _key(item['target']) != _key(target):
                warnings.append(f'Thực thể “{source}” có cách dịch khác “{item["target"]}” / “{target}”; '
                                'giữ mục đã xác nhận và yêu cầu xem lại tính nhất quán.')
                continue
            item['confirmed_ids'] = sorted(set(item['confirmed_ids'] + evidence))[:MAX_CONFIRMATIONS]
        elif len(result['entries']) < MAX_ENTITIES:
            item = {'source': source, 'target': target, 'first_id': min(evidence),
                    'confirmed_ids': sorted(evidence)[:MAX_CONFIRMATIONS], 'origin': origin}
            result['entries'].append(item)
            existing[key] = item
    return result, list(dict.fromkeys(warnings))


def glossary_context(glossary):
    return [{'source': entry['source'], 'target': entry['target']}
            for entry in glossary.get('entries', [])]


def conflicting_entity_ids(glossary, rows, mapping, user_pairs=()):
    """Only retry outputs which contradict an anchored/user entity mapping."""
    expected = {_key(entry['source']): (entry['source'], entry['target'])
                for entry in glossary.get('entries', [])}
    for source, target in user_pairs:
        if _valid_text(source) and _valid_text(target):
            expected[_key(source)] = (source, target)
    conflicts = {}
    for index, target in mapping.items():
        for key, (source, wanted) in expected.items():
            if _contains(str(rows[index].get('text') or ''), source) and not _contains(target, wanted):
                conflicts.setdefault(index, []).append({'source': source, 'target': wanted})
    return conflicts


def accept_entity_consistent_mapping(glossary, rows, mapping, proposals=None, user_pairs=()):
    """Learn from each accepted ID in order, including within one response.

    A later spelling cannot be marked completed before a first-batch entity
    anchor exists. Rejected IDs never seed the durable entity dictionary.
    """
    prepared = copy.deepcopy(rows)
    learned = validate_entity_glossary(glossary, rows)
    accepted, conflicts, warnings = {}, {}, []
    for index in sorted(mapping):
        target = mapping[index]
        bad = conflicting_entity_ids(learned, prepared, {index: target}, user_pairs)
        if bad:
            conflicts.update(bad)
            continue
        prepared[index]['translated'] = target
        learned, messages = update_entity_glossary(learned, prepared, [index], proposals)
        warnings.extend(messages)
        accepted[index] = target
    return accepted, learned, conflicts, list(dict.fromkeys(warnings))
