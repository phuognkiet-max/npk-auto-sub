"""Content identity for local voice previews, independent of the current project."""
import hashlib
import json
from voices import resolve_voice, audio_identity
from pronunciation import apply_pronunciation, dictionary_identity


def sample_signature(params, text):
    row = resolve_voice(params['voice_id'], require_ready=True)
    if row.get('provider') == 'vieneu':
        payload = ['npk-vieneu-sample-v2', row['id'], float(params['voice_speed']), text, audio_identity(row, params)]
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assets = [(str(row[key]), row[key].stat().st_size, row[key].stat().st_mtime_ns)
              for key in ('model', 'config')]
    payload = ['npk-sample-v1', params['voice_id'], float(params['voice_speed']), text, assets]
    dictionary = dictionary_identity(params.get('pronunciation_dictionary', ''))
    if dictionary:
        payload += [dictionary, apply_pronunciation(text, params['pronunciation_dictionary'])]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()
