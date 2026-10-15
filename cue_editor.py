"""Prepare inferred timing metadata for an atomic editor save.

Source ASR words are authored recognition data and remain strictly validated.
Dub-caption timings are a cache: edits must invalidate that cache before its
old bounds are validated against a new cue, rather than rejecting the edit.
The explicit speech_enabled role is authored data. Preparing timing metadata
must preserve it (including absence on legacy cues), never infer it from text.
"""
import copy


def prepare_editor_segments(project, rows):
    from speech_word_alignment import prune_stale_timings

    candidate = copy.deepcopy(project)
    candidate['segments'] = copy.deepcopy(rows)
    # Leave malformed authored rows to the normal validator. Calculating a
    # cache signature must never turn a bad row into an unrelated server error.
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or
        type(row.get('start')) not in (int, float) or
        type(row.get('end')) not in (int, float)
        for row in rows
    ):
        return candidate['segments']
    try:
        prune_stale_timings(candidate)
    except (KeyError, TypeError):
        # Optional generated metadata has no usable identity for malformed
        # input; validate_segments still rejects the authored fields below.
        return candidate['segments']
    return candidate['segments']
