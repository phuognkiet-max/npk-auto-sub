"""NPK-owned local Piper and VieNeu engines; catalog metadata is data only."""
import json
from functools import lru_cache
from pathlib import Path
import wave
from core import MODELS, get
from performance import profile_for
from vieneu_voice import synthesize_vieneu, synthesize_vieneu_batch
from pronunciation import apply_pronunciation, dictionary_identity

LIBRARY = MODELS / 'voice-library/xtrans-20260905'
DEFAULT_VOICE = 'piper:vais1000'


def inside(relative):
    path = (LIBRARY / relative).resolve()
    if LIBRARY.resolve() not in path.parents:
        raise ValueError('Đường dẫn giọng không thuộc thư viện.')
    return path


@lru_cache(maxsize=1)
def entries():
    rows = [{'id': DEFAULT_VOICE, 'name': 'Việt chuẩn · vais1000', 'language': 'vi',
             'model': MODELS/'piper/vi_VN-vais1000-medium.onnx',
             'config': MODELS/'piper/vi_VN-vais1000-medium.onnx.json',
             'license_note': 'Piper vais1000 · xem giấy phép model trong tài liệu bên thứ ba.'}]
    catalog = LIBRARY / 'catalog/piper-voices.json'
    if catalog.is_file():
        for item in json.loads(catalog.read_text(encoding='utf-8-sig')):
            if not item.get('model_path') or not item.get('config_path'):
                continue
            model, config = inside(item['model_path']), inside(item['config_path'])
            if model.is_file() and config.is_file():
                rows.append({'id': item['id'], 'name': item['name'], 'language': item['language'],
                    'model': model, 'config': config, 'speaker_id': item.get('speaker_id'),
                    'same_weights_as': item.get('same_weights_as'),
                    'license_note': 'Tên theo catalog nguồn; chưa xác minh quyền giọng để phân phối thương mại.'})
    return rows


def resolve_voice(identifier, require_ready=False):
    if str(identifier).startswith(('vieneu:', 'cit:')):
        from vieneu_voice import resolve_voice as resolve_vieneu
        return resolve_vieneu(str(identifier).replace('cit:', 'vieneu:', 1), require_ready=require_ready)
    for row in entries():
        if row['id'] == identifier:
            if not row['model'].is_file() or not row['config'].is_file():
                raise ValueError('Thiếu model hoặc cấu hình của giọng đã chọn.')
            return row | {'provider': 'piper', 'available': True, 'state': 'ready'}
    raise ValueError('Giọng chưa được cài hoặc mã giọng không hợp lệ.')


def catalog():
    from vieneu_voice import entries as vieneu_entries
    piper = [{k: v for k, v in row.items() if k not in ('model', 'config')} |
            {'installed': row['model'].is_file() and row['config'].is_file(),
             'provider': 'piper', 'available': row['model'].is_file() and row['config'].is_file(), 'state': 'ready',
             'size_mb': round(row['model'].stat().st_size/1048576, 1) if row['model'].is_file() else 0,
             'test': get('voice_test', row['id'])} for row in entries()]
    return piper + [row | {'installed': True, 'size_mb': 0, 'test': get('voice_test', row['id'])}
                    for row in vieneu_entries()]


def provider_status(force=False):
    from vieneu_voice import provider_status as vieneu_status
    return {'piper': {'provider': 'piper', 'available': True, 'state': 'ready',
                     'message': 'Piper chạy offline trên CPU.'}, 'vieneu': vieneu_status(force=force)}


def audio_identity(entry, params=None):
    if entry.get('provider') == 'vieneu' or entry['id'].startswith('vieneu:'):
        if params is not None:
            from vieneu_voice import synthesis_identity
            return [entry['id'], synthesis_identity(entry, params)]
        return [entry['id'], entry['cache_identity']]
    result = [entry['id'], entry['model'].stat().st_size, entry['model'].stat().st_mtime_ns,
            entry['config'].stat().st_mtime_ns, entry.get('speaker_id')]
    dictionary = dictionary_identity((params or {}).get('pronunciation_dictionary', ''))
    return result + [{'pronunciation': dictionary}] if dictionary else result


def load_voice(identifier, params):
    from piper import PiperVoice
    from piper.config import PiperConfig
    import onnxruntime as ort
    row = resolve_voice(identifier)
    if row.get('provider') == 'vieneu':
        raise ValueError('Giọng VieNeu dùng engine riêng trong NPK, không nạp bằng Piper.')
    options = ort.SessionOptions()
    options.intra_op_num_threads = profile_for(params)['threads']
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    config = PiperConfig.from_dict(json.loads(row['config'].read_text(encoding='utf-8-sig')))
    voice = PiperVoice(config=config, session=ort.InferenceSession(str(row['model']), sess_options=options,
                       providers=['CPUExecutionProvider']), download_dir=MODELS/'piper')
    return voice, row


def synthesize_sample(identifier, text, path, params, progress=None):
    text = str(text).strip()
    if not text or len(text) > 400:
        raise ValueError('Mẫu nghe thử cần 1–400 ký tự.')
    return synthesize_text(identifier, text, path, params, progress=progress)


def synthesize_text(identifier, text, path, params, progress=None):
    text = str(text).strip()
    if not text or len(text) > 10000:
        raise ValueError('Nội dung đọc cần 1–10000 ký tự.')
    row = resolve_voice(identifier, require_ready=True)
    if row.get('provider') == 'vieneu':
        return synthesize_vieneu(row, text, path, params.get('voice_speed', 1), params=params, progress=progress)
    spoken = apply_pronunciation(text, params.get('pronunciation_dictionary', ''))
    from piper import SynthesisConfig
    voice, row = load_voice(identifier, params)
    with wave.open(str(path), 'wb') as output:
        voice.synthesize_wav(spoken, output, syn_config=SynthesisConfig(
            speaker_id=row.get('speaker_id'), length_scale=1/float(params.get('voice_speed', 1))))
    with wave.open(str(path), 'rb') as source:
        sample_rate = source.getframerate()
        duration = source.getnframes() / sample_rate
    return {'voice_id': row['id'], 'voice_name': row['name'], 'provider': 'piper',
            'sample_rate': sample_rate, 'duration': duration,
            'chunks': [{'kind': 'speech', 'start': 0, 'end': duration, 'text': spoken}]}
