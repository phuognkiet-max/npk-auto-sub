"""Pure metadata for a completed single- or multi-voice dubbing timeline."""


def dubbing_voice_metadata(entries, default=None):
    used = {}
    for entry in entries:
        identifier = entry['id']
        if identifier not in used:
            used[identifier] = {'id': identifier, 'name': entry['name'], 'cue_count': 0}
        used[identifier]['cue_count'] += 1
    voices = list(used.values())
    if not voices and default is not None:
        voices = [{'id': default['id'], 'name': default['name'], 'cue_count': 0}]
    if len(voices) == 1:
        return {'voice_mode': 'single', 'voice_id': voices[0]['id'], 'voice_name': voices[0]['name'],
                'voices_used': voices}
    return {'voice_mode': 'multi' if voices else 'none', 'voice_id': None,
            'voice_name': f'{len(voices)} giọng' if voices else 'Chưa có giọng', 'voices_used': voices}
