"""Bounded local UI preferences, independent of the webview's random port."""
import math
import re
from core import get, put

DEFAULTS = {'tts_text': '', 'tts_voice': '', 'tts_speed': 1.0, 'tts_job': '',
            'favorite_voices': [], 'viewer_ratio': 54, 'current_project': '', 'sample_text': '',
            'tts_params': {'tts_device': 'auto', 'workload_mode': 'quiet', 'pronunciation_dictionary': ''},
            'narration_segments': []}
ID = re.compile(r'^[0-9a-f]{32}$')


def read():
    stored = get('ui_preferences', 'main') or {}
    return DEFAULTS | {k: v for k, v in stored.items() if k in DEFAULTS}


def snapshot():
    """GET-only migration metadata distinguishes a saved empty draft from defaults."""
    stored = get('ui_preferences', 'main')
    values = {k: v for k, v in (stored or {}).items() if k in DEFAULTS}
    return DEFAULTS | values | {'_persisted': stored is not None}


def update(body):
    if not isinstance(body, dict) or set(body) - DEFAULTS.keys():
        raise ValueError('Tùy chọn giao diện không được hỗ trợ.')
    clean = {}
    for key, value in body.items():
        if key == 'tts_params':
            if not isinstance(value, dict) or set(value) - {'tts_device', 'workload_mode', 'pronunciation_dictionary'}:
                raise ValueError('Tùy chọn giọng không hợp lệ.')
            value = read().get('tts_params', DEFAULTS['tts_params']) | value
            if value.get('tts_device') not in ('auto', 'cpu', 'cuda') or value.get('workload_mode') not in ('quiet', 'balanced', 'fast'):
                raise ValueError('Thiết bị hoặc tải xử lý giọng không hợp lệ.')
            lexicon = value.get('pronunciation_dictionary', '')
            if not isinstance(lexicon, str) or len(lexicon) > 20000 or '\x00' in lexicon:
                raise ValueError('Từ điển cách đọc quá dài hoặc không hợp lệ.')
        elif key == 'narration_segments':
            if not isinstance(value, list) or len(value) > 80:
                raise ValueError('Kịch bản cần tối đa 80 đoạn.')
            total = 0
            for row in value:
                if not isinstance(row, dict) or set(row) - {'text', 'voice_id', 'rate', 'pause_after_ms'}:
                    raise ValueError('Đoạn kịch bản không hợp lệ.')
                text = row.get('text', '')
                voice = row.get('voice_id', '')
                rate, pause = row.get('rate', 1), row.get('pause_after_ms', 0)
                if not isinstance(text, str) or len(text) > 10000 or '\x00' in text or not isinstance(voice, str) or len(voice) > 200:
                    raise ValueError('Văn bản hoặc giọng của kịch bản không hợp lệ.')
                if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not math.isfinite(rate) or not .5 <= rate <= 2 or type(pause) is not int or not 0 <= pause <= 10000:
                    raise ValueError('Nhịp đọc của kịch bản không hợp lệ.')
                total += len(text)
            if total > 10000:
                raise ValueError('Tổng kịch bản cần tối đa 10.000 ký tự.')
        elif key in ('tts_speed', 'viewer_ratio'):
            low, high = (.5, 2) if key == 'tts_speed' else (25, 75)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('Giá trị tùy chọn giao diện ngoài phạm vi.')
        elif key == 'favorite_voices':
            if not isinstance(value, list) or len(value) > 200 or any(not isinstance(v, str) or not 1 <= len(v) <= 200 for v in value):
                raise ValueError('Danh sách giọng yêu thích không hợp lệ.')
            value = list(dict.fromkeys(value))
        else:
            maximum = 10000 if key == 'tts_text' else 400 if key == 'sample_text' else 200
            if not isinstance(value, str) or len(value) > maximum or '\x00' in value:
                raise ValueError('Nội dung tùy chọn giao diện quá dài hoặc không hợp lệ.')
            if key in ('tts_job', 'current_project') and value and not ID.fullmatch(value):
                raise ValueError('Mã dự án hoặc tác vụ không hợp lệ.')
        clean[key] = value
    return put('ui_preferences', 'main', read() | clean)
