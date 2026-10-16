"""Prepare inferred timing metadata for an atomic editor save.

Source ASR words are authored recognition data and remain strictly validated.
Dub-caption timings are a cache: edits must invalidate that cache before its
old bounds are validated against a new cue, rather than rejecting the edit.
The explicit speech_enabled role is authored data. Preparing timing metadata
must preserve it (including absence on legacy cues), never infer it from text.
"""
import copy
from collections import Counter


def prepare_editor_segments(project, rows):
    from speech_word_alignment import prune_stale_timings

    candidate = copy.deepcopy(project)
    candidate['segments'] = copy.deepcopy(rows)
    # Leave malformed authored rows to the normal validator. Calculating a
    # cache signature must never turn a bad row into an unrelated server error.
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or
        type(row.get('start')) not in (int, float) or
        type(row.get('end')) not in (int, float) or
        not isinstance(row.get('text', ''), str)
        for row in rows
    ):
        return candidate['segments']
    previous = [row for row in project.get('segments', []) if isinstance(row, dict)]
    def identity(row):
        return row.get('start'), row.get('end'), row.get('text')
    ids = Counter(str(row.get('id')) for row in previous if row.get('id') is not None)
    identities = Counter(identity(row) for row in previous)
    by_id = {str(row['id']): row for row in previous if row.get('id') is not None and ids[str(row['id'])] == 1}
    by_identity = {identity(row): row for row in previous if identities[identity(row)] == 1}
    for index, row in enumerate(candidate['segments']):
        # Invalid authored markers must reach the normal validator unchanged.
        if 'user_edited' in row and type(row['user_edited']) is not bool:
            continue
        original = by_id.get(str(row.get('id'))) if row.get('id') is not None else None
        original = original or by_identity.get(identity(row))
        if original is None and len(rows) == len(previous):
            original = previous[index]
        if original is not None:
            edited = original.get('user_edited') is True or any(
                row.get(key, '') != original.get(key, '') for key in ('translated', 'spoken_text'))
        else:
            # A newly authored/split script has no prior translation identity.
            edited = any(bool(str(row.get(key) or '').strip()) for key in ('translated', 'spoken_text'))
        if edited:
            row['user_edited'] = True
    try:
        prune_stale_timings(candidate)
    except (KeyError, TypeError):
        # Optional generated metadata has no usable identity for malformed
        # input; validate_segments still rejects the authored fields below.
        return candidate['segments']
    return candidate['segments']
