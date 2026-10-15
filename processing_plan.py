"""Shared content requirements for tab, export and pipeline entry points.

This module has no storage/model/provider imports. Reading source text is an
explicit supported choice; an unrelated translation checkbox cannot change it.
"""
from speech_delivery import text_source, missing_speech_rows


def content_requirements(project, kind='pipeline'):
    p, rows = project['params'], project.get('segments', [])
    pipeline = kind == 'pipeline'
    extract = p.get('pipeline_extract', 'existing') if pipeline else kind
    future_rows = extract in ('ocr', 'asr')
    native_voice = p.get('voice_gain', 0) > 0 and not p.get('voice_path')
    execute_speech = kind == 'tts' or (pipeline and p.get('pipeline_tts', True))
    caption = bool(p.get('burn_subtitles'))
    translation_requested = kind in ('translate', 'optimize_spoken') or (
        pipeline and p.get('pipeline_translate', True))
    missing_translation = [i for i, row in enumerate(rows)
                           if not str(row.get('translated') or '').strip()]
    # Direct translate retains its selected overwrite policy; a pipeline never
    # needs a provider/key merely to reuse an already translated table.
    will_translate = translation_requested and (
        kind in ('translate', 'optimize_spoken') or future_rows or bool(missing_translation))
    source = text_source(p)
    missing_speech = missing_speech_rows(rows, p) if rows else []
    return {'text_source': source, 'future_rows': future_rows,
            'needs_captions': caption, 'needs_voice_text': bool(native_voice or kind == 'tts'),
            'execute_speech': bool(execute_speech), 'will_translate': bool(will_translate),
            'missing_translation': missing_translation, 'missing_speech': missing_speech,
            'speech_text_pending': bool(will_translate and source != 'source'),
            'needs_text': bool(caption or native_voice or kind == 'tts')}
