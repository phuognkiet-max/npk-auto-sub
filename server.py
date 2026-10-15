from __future__ import annotations
import hmac
import json
import copy
import math
import os
import re
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
import keyring
import psutil

from core import ROOT, DATA, MODELS, DEFAULTS, NO_WINDOW, all_items, create_project, get, put, settings, safe_owned, save_srt, save_project_version
from credentials import (PROVIDERS, credential_status, store_credential, credential_list, add_credentials,
    select_credential, delete_credential, record_check, active_key_id, active_secret, secret_name)
from process_control import terminate_job_workers
from job_progress import accept_event

TOKEN = os.environ.get('VIETDUB_TOKEN') or secrets.token_urlsafe(32)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
state_lock = threading.RLock()
active_lock = threading.Lock()
active_process = None
active_id = None
stop_event = threading.Event()
queue_file_lock = None
queue_thread = None
queue_runtime = {'healthy': True, 'last_error': '', 'last_success': None, 'consecutive_errors': 0}
owner_record = None
owner_heartbeat_thread = None
owner_runtime = {'healthy': True}


def owner_heartbeat_loop():
    """Discovery remains live while the queue waits for a long/silent worker."""
    while not stop_event.wait(10):
        with state_lock:
            if owner_record is None:
                return
            try:
                from owner_connection import refresh
                owner_record.update(refresh(DATA, owner_record,
                    queue_owned=bool(queue_file_lock and not queue_file_lock.closed and
                                     queue_thread and queue_thread.is_alive())))
                owner_runtime['healthy'] = True
            except Exception:
                owner_runtime['healthy'] = False


def register_owner(port):
    """Publish the authenticated loopback connection of this queue owner."""
    global owner_record, owner_heartbeat_thread
    if owner_record is not None:
        raise RuntimeError('Phiên sở hữu hàng đợi đã được công bố.')
    from owner_connection import publish
    owner_record = publish(DATA, port, TOKEN, queue_owned=bool(queue_file_lock and not queue_file_lock.closed))
    owner_runtime['healthy'] = True
    owner_heartbeat_thread = threading.Thread(target=owner_heartbeat_loop, name='NPKOwnerHeartbeat', daemon=True)
    owner_heartbeat_thread.start()
    return owner_record


@app.get('/api/studio/owner')
def live_owner():
    from owner_connection import public_identity
    status = queue_status()
    if owner_record is None:
        raise HTTPException(503, 'Chưa xác nhận ứng dụng đang sở hữu hàng đợi.')
    return public_identity(owner_record) | {
        'queue_owned': bool(queue_file_lock and not queue_file_lock.closed),
        'healthy': bool(status['healthy'] and owner_runtime['healthy']), 'thread_alive': status['thread_alive']}


def require_owner_instance(body):
    instance = body.get('owner_instance_id')
    # UI requests use the existing session authentication. External CLI
    # requests additionally pin the owner observed during their handshake.
    if instance is not None and (owner_record is None or instance != owner_record['instance_id']):
        raise HTTPException(409, 'Ứng dụng sở hữu hàng đợi đã thay đổi; kiểm tra lại trước khi tiếp tục.')


def queue_status():
    control = get('queue_control', 'main') or {}
    return dict(queue_runtime, paused=bool(control.get('paused')), active_id=active_id,
                thread_alive=bool(queue_thread and queue_thread.is_alive()))


def queue_control(body):
    if not isinstance(body, dict) or set(body) != {'paused'} or type(body['paused']) is not bool:
        raise ValueError('Chọn tạm dừng hoặc tiếp tục hàng đợi.')
    with state_lock:
        put('queue_control', 'main', {'paused': body['paused']})
    return queue_status()


def reorder_queue(body):
    ids = body.get('ids') if isinstance(body, dict) else None
    if not isinstance(ids, list) or len(ids) > 1000 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('Thứ tự hàng đợi không hợp lệ.')
    from contextlib import closing
    from core import connect
    with state_lock, closing(connect()) as db:
        queued = [json.loads(r[0]) for r in db.execute("SELECT value FROM objects WHERE kind='job' AND json_extract(value,'$.state')='queued'").fetchall()]
        queued.sort(key=lambda j: (j.get('queue_order', j['created']), j['created']))
        available = {j['id']: j for j in queued}
        if any(i not in available for i in ids):
            raise ValueError('Hàng đợi đã thay đổi. Làm mới trước khi sắp xếp.')
        ordered = [available[i] for i in ids] + [j for j in queued if j['id'] not in ids]
        start = time.time() - len(ordered) - 1
        with db:
            for index, job in enumerate(ordered):
                job['queue_order'] = start + index
                db.execute('UPDATE objects SET value=? WHERE kind=? AND id=?', (json.dumps(job, ensure_ascii=False), 'job', job['id']))
    return {'ids': [j['id'] for j in ordered]}

@app.middleware('http')
async def authenticate(request: Request, call_next):
    if request.url.path.startswith(('/api', '/media')):
        supplied = request.headers.get('x-studio-token') or request.query_params.get('token', '')
        if not hmac.compare_digest(supplied, TOKEN):
            return JSONResponse({'detail': 'Phiên truy cập không hợp lệ.'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Cache-Control'] = 'no-store'
    return response

@app.exception_handler(ValueError)
async def value_error(request, exc):
    from execution_admission import AdmissionError
    if isinstance(exc, AdmissionError):
        return JSONResponse({'detail': str(exc), 'execution_admission': exc.report}, status_code=409)
    return JSONResponse({'detail': str(exc)}, status_code=400)

@app.exception_handler(FileNotFoundError)
async def missing_file(request, exc):
    return JSONResponse({'detail': 'Không tìm thấy tệp. Kiểm tra lại đường dẫn.'}, status_code=404)

def project_by_id(identifier):
    project = get('project', identifier)
    if not project:
        raise HTTPException(404, 'Không tìm thấy dự án.')
    # Restored projects without this setting retain their original timing and
    # speech signature. Only newly created projects inherit the natural default.
    from core import restored_params
    project['params'] = restored_params(project['params'])
    # The former companion bridge is replaced by NPK's own local engine.
    if str(project['params'].get('voice_id', '')).startswith('cit:'):
        project['params']['voice_id'] = project['params']['voice_id'].replace('cit:', 'vieneu:', 1)
    for row in project.get('segments', []):
        if str(row.get('voice_id', '')).startswith('cit:'):
            row['voice_id'] = row['voice_id'].replace('cit:', 'vieneu:', 1)
    from speech_word_alignment import prune_stale_timings
    prune_stale_timings(project)
    return project

def number(value, low, high):
    try:
        if isinstance(value, bool):
            raise ValueError
        value = float(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError('Cần nhập một số hợp lệ, không để trống mốc thời gian hoặc thông số.') from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'Giá trị phải trong khoảng {low}–{high}.')
    return value

def validate_params(params, *, provider=None):
    if not isinstance(params, dict):
        raise ValueError('Thông số không hợp lệ.')
    clean = {}
    ranges = {'dilate': (0, 32), 'context': (8, 512), 'max_side': (128, 1536), 'ocr_fps': (.2, 8),
              'cover_opacity': (0, 1), 'edge_feather': (0, 32), 'stretch_offset': (0, 200), 'inpaint_radius': (1, 9),
              'font_size': (8, 240), 'margin': (0, 2000), 'outline': (0, 20), 'original_gain': (0, 2),
              'voice_gain': (0, 2), 'bgm_gain': (0, 2), 'voice_speed': (.5, 2),
              'ocr_confidence': (.1, 1), 'ocr_similarity': (.1, 1), 'translation_batch': (1, 60),
              'words_per_second': (1, 8), 'blur_radius': (2, 64), 'stretch_height': (2, 100), 'video_quality': (14, 30),
              'subtitle_shadow':(0,20), 'subtitle_margin_x':(0,2000), 'voice_offset':(-3600,3600),
              'extraction_start':(0,86400), 'extraction_end':(0,86400),
              'subtitle_line_chars':(10,120),'subtitle_max_lines':(1,6), 'subtitle_chunk_words':(2,12)}
    choices = {'provider': {'gemini', 'deepseek', 'router9'}, 'asr_model': {'tiny', 'base', 'small', 'medium', 'large-v3'},
               'asr_device': {'cpu', 'cuda'}, 'method': {'lama', 'telea', 'box', 'blur', 'stretch'}, 'mask_mode': {'rectangle', 'glyph'},
               'source_language': {'auto', 'zh', 'en', 'vi', 'ja', 'ko', 'fr', 'de', 'es'},
               'workload_mode': {'quiet', 'balanced', 'fast'}, 'mask_source': {'manual', 'tracked'},
               'video_encoder': {'auto','cpu','nvenc'}, 'stretch_direction': {'above','below'},
               'pipeline_extract': {'existing','ocr','asr'}, 'subtitle_alignment':{'bottom','middle','top'},
               'translation_scope':{'all','missing','selected'},'ocr_scale_mode':{'native','legacy'},
               'export_resolution':{'source','720','1080','2160'},
               'tts_timing_mode': {'natural', 'legacy'}, 'tts_device': {'auto', 'cpu', 'cuda'},
               'tts_text_source': {'translated', 'edited', 'source'},
               'subtitle_preset': {'clean', 'pop', 'highlight', 'karaoke'},
               'subtitle_flow': {'single', 'wrap'},
               'subtitle_automation': {'local', 'gemini', 'router9', 'manual'},
               'asr_compute': {'auto', 'float16', 'int8_float16', 'int8'}}
    for key, value in params.items():
        if key not in DEFAULTS:
            continue
        if key in ranges:
            value = number(value, *ranges[key])
            if key == 'subtitle_chunk_words':
                if not value.is_integer():
                    raise ValueError('Số từ mỗi cụm phải là số nguyên từ 2 đến 12.')
                value = int(value)
        elif key in choices:
            if value not in choices[key]:
                raise ValueError(f'Lựa chọn {key} không hợp lệ.')
        elif isinstance(DEFAULTS[key], bool):
            if not isinstance(value, bool):
                raise ValueError(f'{key} phải là bật/tắt.')
        else:
            value = str(value)[:20000]
        if key in {'voice_path', 'bgm_path'} and value:
            from audio_inputs import local_audio_path
            path = local_audio_path(value)
            value = str(path)
        if key == 'voice_id':
            from voices import resolve_voice
            value = resolve_voice(value)['id']
        if key == 'pronunciation_dictionary':
            from pronunciation import validate_dictionary
            value = validate_dictionary(value)
        if key in ('cover_color','font_color','outline_color'):
            import re
            if not re.fullmatch(r'#[a-fA-F0-9]{6}', value):
                raise ValueError('Màu hộp cần dạng #RRGGBB.')
        if key == 'translation_model':
            from providers import safe_model
            selected_provider = params.get('provider', provider)
            value = '' if value == '' and selected_provider == 'router9' else safe_model(value, provider=selected_provider)
        if key == 'router9_base_url':
            from providers import router_base_url
            value = router_base_url(value)
        if key == 'export_name':
            import re
            value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', value).strip(' .')[:120]
            if not value or value.upper().split('.')[0] in {'CON','PRN','AUX','NUL',*[f'COM{i}' for i in range(1,10)],*[f'LPT{i}' for i in range(1,10)]}:
                raise ValueError('Tên video thành phẩm không hợp lệ.')
        if key == 'export_directory' and value:
            if value.startswith(('\\\\','//')) or not Path(value).is_absolute() or not Path(value).is_dir():
                raise ValueError('Chọn một thư mục xuất hiện có trên máy.')
        clean[key] = value
    if 'subtitle_preset' in clean:
        from subtitle_presets import apply_subtitle_preset
        # Selecting a preset returns its full style immediately. Deliberate
        # style overrides in this same request remain authoritative.
        clean = apply_subtitle_preset({}, clean['subtitle_preset']) | clean
    return clean

def validate_segments(rows, duration):
    if not isinstance(rows, list) or len(rows) > 50000:
        raise ValueError('Danh sách phụ đề không hợp lệ hoặc quá lớn.')
    clean = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('Mỗi câu phụ đề cần có nội dung và mốc thời gian hợp lệ.')
        start = number(row.get('start'), 0, duration)
        end = min(duration, number(row.get('end'), 0, duration + .5))
        if end <= start:
            raise ValueError('Mốc kết thúc phụ đề phải lớn hơn mốc bắt đầu.')
        content = {}
        for key, label in (('text', 'Lời gốc'), ('translated', 'Bản dịch')):
            value = row.get(key, '')
            if not isinstance(value, str) or len(value) > 10000:
                raise ValueError(f'{label} phải là văn bản không quá 10.000 ký tự; nội dung chưa được lưu hoặc cắt bớt.')
            content[key] = value
        clean.append({'start': start, 'end': min(end, duration), **content})
        # Missing keeps the legacy spoken-cue role; an explicit false preserves
        # text for display/translation while excluding it from AI dubbing.
        if 'speech_enabled' in row:
            if type(row['speech_enabled']) is not bool:
                raise ValueError('Lồng tiếng câu này phải là lựa chọn bật hoặc tắt.')
            clean[-1]['speech_enabled'] = row['speech_enabled']
        if 'character_name' in row:
            name = row['character_name']
            if not isinstance(name, str) or len(name) > 80 or any(ord(c) < 32 for c in name):
                raise ValueError('Tên nhân vật phải là văn bản một dòng không quá 80 ký tự.')
            if name.strip():
                clean[-1]['character_name'] = name.strip()
        if row.get('voice_id'):
            clean[-1]['voice_id']=validate_params({'voice_id':row['voice_id']})['voice_id']
        if 'spoken_text' in row:
            if not isinstance(row['spoken_text'], str) or len(row['spoken_text']) > 10000:
                raise ValueError('Lời đọc riêng phải là văn bản không quá 10.000 ký tự.')
            spoken = row['spoken_text'].strip()
            if spoken:
                clean[-1]['spoken_text'] = spoken
        if row.get('speech_rate') is not None:
            if isinstance(row['speech_rate'], bool):
                raise ValueError('Tốc độ đọc riêng phải là số trong khoảng 0,5–2.')
            clean[-1]['speech_rate'] = number(row['speech_rate'], .5, 2)
        for pause_key in ('pause_before_ms', 'pause_after_ms'):
            if row.get(pause_key) is None:
                continue
            if isinstance(row[pause_key], bool):
                raise ValueError('Khoảng nghỉ phải là số nguyên từ 0 đến 2.000 ms.')
            pause = number(row[pause_key], 0, 2000)
            if not pause.is_integer():
                raise ValueError('Khoảng nghỉ phải là số nguyên từ 0 đến 2.000 ms.')
            clean[-1][pause_key] = int(pause)
        pauses = sum(clean[-1].get(key, 0) for key in ('pause_before_ms', 'pause_after_ms'))
        if pauses / 1000 >= end - start:
            raise ValueError('Tổng khoảng nghỉ phải ngắn hơn thời lượng câu để còn chỗ cho lời đọc.')
        if row.get('subtitle_style'):
            from core import SUBTITLE_STYLE_KEYS
            if not isinstance(row['subtitle_style'],dict):raise ValueError('Kiểu riêng của câu không hợp lệ.')
            clean[-1]['subtitle_style']=validate_params({k:v for k,v in row['subtitle_style'].items() if k in SUBTITLE_STYLE_KEYS})
        for word_field in ('words', 'caption_words'):
            if word_field not in row:
                continue
            words = row[word_field]
            if not isinstance(words, list) or len(words) > 3000:
                raise ValueError('Mốc từng từ không hợp lệ hoặc quá dài.')
            clean_words, previous = [], start
            for word in words:
                if not isinstance(word, dict):
                    raise ValueError('Mốc từng từ không hợp lệ.')
                a, b = number(word.get('start'), start, end), number(word.get('end'), start, end)
                if b < a or (word_field == 'caption_words' and b == a) or a < previous - .001:
                    raise ValueError('Mốc từng từ cần theo thứ tự bên trong câu.')
                text = str(word.get('word', word.get('text', '')))[:200]
                clean_words.append({'word': text, 'start': a, 'end': b})
                if word.get('probability') is not None:
                    clean_words[-1]['probability'] = number(word['probability'], 0, 1)
                previous = a
            clean[-1][word_field] = clean_words
        if 'word_timing_source' in row:
            if row['word_timing_source'] not in ('aligned', 'estimated'):
                raise ValueError('Nguồn mốc từ cần là căn âm thanh hoặc ước lượng.')
            clean[-1]['word_timing_source'] = row['word_timing_source']
        if 'caption_timing_key' in row:
            if not isinstance(row['caption_timing_key'], str) or not re.fullmatch('[a-f0-9]{64}', row['caption_timing_key']):
                raise ValueError('Chữ ký căn từ không hợp lệ.')
            clean[-1]['caption_timing_key'] = row['caption_timing_key']
    # Preserve the editor's order: selected indices must refer to the exact rows
    # the user sees. Sorting is an explicit undoable editor operation.
    return clean

@app.get('/api/state')
def state():
    statuses = {name: credential_status(name) for name in PROVIDERS}
    return {'projects': all_items('project'), 'jobs': all_items('job')[:100], 'settings': settings(),
            'root': str(ROOT), 'models': {'lama': (MODELS / 'lama/big-lama.pt').is_file(),
                                         'piper': (MODELS / 'piper/vi_VN-vais1000-medium.onnx').is_file()},
            'keys': {name: status['present'] for name, status in statuses.items()}, 'key_status': statuses}

@app.get('/api/keys/{provider}/status')
def read_key_status(provider: str):
    return credential_status(provider)

@app.post('/api/projects')
def new_project(body: dict):
    with state_lock:
        return create_project(body.get('path', ''), body.get('name', ''))


@app.get('/api/catalog')
def catalog(refresh_voices: bool = False):
    from voices import catalog as voice_catalog, provider_status
    from providers import CATALOG_SEEDS
    from preflight import engines
    status = provider_status(force=refresh_voices)
    return {'voices': voice_catalog(), 'voice_providers': status,
            'models': CATALOG_SEEDS, 'engines': engines()}


@app.get('/api/keys/{provider}')
def keys_list(provider: str):
    return credential_list(provider)


@app.post('/api/keys/{provider}/add')
def keys_add(provider: str, body: dict):
    with state_lock:
        return add_credentials(provider, body.get('keys', ''))


@app.post('/api/keys/{provider}/select')
def keys_select(provider: str, body: dict):
    with state_lock:
        return select_credential(provider, body.get('id', ''))


@app.post('/api/keys/{provider}/delete')
def keys_delete(provider: str, body: dict):
    with state_lock:
        return delete_credential(provider, body.get('id', ''))


@app.post('/api/keys/{provider}/check')
def keys_check(provider: str, body: dict):
    from providers import check_connection, ensure_provider, router_base_url, safe_model, ensure_model_provider
    ensure_provider(provider)
    mode = body.get('mode','catalog')
    if mode not in ('catalog','generation'):
        raise ValueError('Chế độ kiểm tra không hợp lệ.')
    if mode == 'generation' and body.get('confirm_cost') is not True:
        raise ValueError('Cần xác nhận phép thử sinh nội dung có thể tính phí.')
    options = {'base_url': router_base_url(body.get('base_url'))} if provider == 'router9' else {}
    if mode == 'generation':
        model = safe_model(body.get('model', ''), provider)
        ensure_model_provider(provider, model)
        if not body.get('project_id'):
            raise ValueError('Mở hoặc chọn dự án trước khi thử dịch để kiểm tra giới hạn API của đúng tác vụ.')
        from execution_admission import require_admission
        # A connection probe may target a different provider than the editor.
        # Check the requested connection without persisting it in the project.
        probe_project = copy.deepcopy(project_by_id(body['project_id']))
        probe_project['params']['provider'] = provider
        probe_project['params']['translation_model'] = model
        if provider == 'router9':
            probe_project['params']['router9_base_url'] = options['base_url']
        require_admission(probe_project, all_items('job'), kind='translate')
        def check_probe_admission(event):
            if event.get('event') == 'before_send':
                # The shared scheduler may wait behind a worker. A hold added
                # during that wait must stop a probe before its HTTP send too.
                require_admission(probe_project, all_items('job'), kind='translate')
        options['on_request'] = check_probe_admission
    identifier = body.get('id') or active_key_id(provider)
    if identifier == 'none':
        raise ValueError('Chọn một key trước khi kiểm tra.')
    secret = keyring.get_password('VietDubStudio', secret_name(provider, identifier))
    if not secret:
        raise ValueError('Key chưa được lưu hoặc đã bị xóa.')
    result = check_connection(provider, secret, body.get('model',''), mode, **options)
    with state_lock:
        record_check(provider, identifier, secret, result)
    return result


@app.post('/api/preflight')
def preflight(body: dict):
    from preflight import inspect
    from execution_admission import admission_status
    project = project_by_id(body['project_id'])
    kind = body.get('kind','pipeline')
    report = inspect(project, kind)
    from worker import pipeline_provider_steps
    admission = admission_status(project, all_items('job'), kind=kind,
                                 remaining_steps=pipeline_provider_steps(project, kind))
    report['execution_admission'] = admission
    if not admission['execution_allowed']:
        report['errors'] = list(dict.fromkeys(report.get('errors', []) + [admission['blocked_reason']]))
        report['ok'] = False
    return report

def validate_regions(regions, m):
    if not isinstance(regions,list) or len(regions)>500:
        raise ValueError('Tối đa 500 vùng xử lý.')
    clean=[]
    for region in regions:
        if not isinstance(region, dict):
            raise ValueError('Mỗi vùng xử lý cần có vị trí và kích thước hợp lệ.')
        x,y=number(region.get('x'),0,m['width']-1),number(region.get('y'),0,m['height']-1)
        w,h=number(region.get('w'),1,m['width']),number(region.get('h'),1,m['height'])
        start,end=number(region.get('start',0),0,m['duration']),number(region.get('end',m['duration']),0,m['duration'])
        if end<=start:raise ValueError('Thời gian kết thúc vùng phải lớn hơn thời gian bắt đầu.')
        clean.append(dict(x=x,y=y,w=min(w,m['width']-x),h=min(h,m['height']-y),start=start,end=end))
    return clean


@app.put('/api/projects/{identifier}')
def update_project(identifier: str, body: dict):
    with state_lock:
        project = project_by_id(identifier)
        if ('expected_revision' in body and body['expected_revision'] != project['updated'] and
                not artifact_only_revision_change(project, body['expected_revision'])):
            raise HTTPException(409, 'Dự án đã thay đổi ở phiên hoặc tác vụ khác. Tải lại trước khi lưu để tránh ghi đè.')
        previous = json.loads(json.dumps(project))
        if 'name' in body:
            project['name'] = str(body['name'])[:200]
        if 'params' in body:
            project['params'].update(validate_params(body['params'], provider=project['params'].get('provider')))
        for region_key in ('regions','extraction_regions'):
            if region_key not in body:continue
            project[region_key] = validate_regions(body[region_key],project['media'])
        if 'segments' in body:
            from cue_editor import prepare_editor_segments
            project['segments'] = validate_segments(
                prepare_editor_segments(project, body['segments']), project['media']['duration'])
        from speech_word_alignment import prune_stale_timings
        prune_stale_timings(project)
        # Windows clock resolution can return the same timestamp twice. A save
        # must still invalidate the previous revision used by rename/delete UI.
        project['updated'] = max(time.time(), previous['updated'] + .000001)
        return save_project_version(project, previous)


def artifact_only_revision_change(project, expected):
    """A completed preview/voice may add files while the user types a draft.

    Rebase only when retained history proves that every editable project field
    stayed identical. Saving merges into current artifacts; a genuine concurrent
    edit to cues, settings, name or video must continue to return HTTP 409.
    """
    if type(expected) not in (int, float) or not math.isfinite(expected):
        return False
    previous = get('project_revision', f'{project["id"]}:{expected:.6f}')
    if not previous or previous.get('updated') != expected:
        return False
    ignored = {'updated', 'artifacts'}
    return ({k:v for k,v in previous.items() if k not in ignored} ==
            {k:v for k,v in project.items() if k not in ignored})

@app.post('/api/projects/{identifier}/import')
def import_subtitles(identifier: str, body: dict):
    import pysubs2
    text = str(body.get('text', ''))
    if len(text) > 10_000_000:
        raise ValueError('Tệp phụ đề quá lớn.')
    doc = pysubs2.SSAFile.from_string(text)
    rows = [{'start': event.start / 1000, 'end': event.end / 1000, 'text': event.plaintext, 'translated': ''} for event in doc]
    changes = {'segments': rows}
    if 'expected_revision' in body:
        changes['expected_revision'] = body['expected_revision']
    return update_project(identifier, changes)

@app.get('/api/projects/{identifier}/subtitles')
def export_subtitles(identifier: str, translated: bool = True, format: str = 'srt'):
    import tempfile
    project = project_by_id(identifier)
    if format not in ('srt','ass'):
        raise ValueError('Định dạng phụ đề không được hỗ trợ.')
    target = DATA / 'frames' / f'{identifier}-export.{format}'
    save_srt(project['segments'], target, translated=translated,
             ass_style=(project['params'],project['media']) if format=='ass' else None)
    return FileResponse(target, filename='Phu-de.'+format, media_type='text/plain; charset=utf-8')

@app.get('/api/projects/{identifier}/frame')
def frame(identifier: str, time: float = 0):
    import cv2
    from worker import read_frame
    project = project_by_id(identifier)
    time = number(time, 0, max(0, project['media']['duration'] - .001))
    image = read_frame(project['media']['path'], time)
    ok, encoded = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    if not ok:
        raise ValueError('Không mã hóa được khung hình.')
    return Response(encoded.tobytes(), media_type='image/jpeg')


@app.post('/api/projects/{identifier}/audio/inspect')
def inspect_project_audio(identifier: str, body: dict):
    from audio_inputs import inspect_audio
    project = project_by_id(identifier)
    return inspect_audio(body.get('path', ''), project['media']['duration'])

@app.put('/api/settings')
def save_settings(body: dict):
    with state_lock:
        saved = settings() | validate_params(body)
        return put('settings', 'main', saved)

@app.post('/api/keys')
def save_key(body: dict):
    status = store_credential(body.get('provider'), body.get('key', ''))
    return {'saved': status['present'], 'status': status}

@app.post('/api/jobs')
def enqueue(body: dict):
    return create_job(body)


def create_job(body: dict, *, project_override=None, continuation_from=None, recovery_from=None):
    """Queue an authenticated request or a validated private continuation."""
    from contextlib import closing
    from core import connect
    from job_admission import project_lease
    project_id = {'voice_preview': 'voice-preview', 'text_to_speech': 'text-to-speech',
                  'enroll_voice': 'voice-enrollment'}.get(body.get('kind'), body.get('project_id'))
    with state_lock:
        # Initialize the normal app store before taking a durable admission
        # lease. The inspector CLI never enters this mutation path.
        with closing(connect()):
            pass
        with project_lease(DATA, project_id):
            return _create_job_locked(body, project_override=project_override,
                                      continuation_from=continuation_from, recovery_from=recovery_from)


def _create_job_locked(body: dict, *, project_override=None, continuation_from=None, recovery_from=None):
    from worker import TASKS
    with state_lock:
        from storage_manager import _ensure_no_pending
        _ensure_no_pending()
        request_id = str(body.get('request_id',''))[:128]
        if request_id:
            previous_request = get('job_request', request_id)
            if previous_request:
                previous_job = get('job', previous_request['job_id'])
                expected_project = {'voice_preview': 'voice-preview', 'text_to_speech': 'text-to-speech', 'enroll_voice': 'voice-enrollment'}.get(body.get('kind'), body.get('project_id'))
                if previous_job and previous_job['project_id']==expected_project and previous_job['kind']==body.get('kind'):
                    if continuation_from and previous_job.get('continuation_from') != continuation_from:
                        raise ValueError('Mã yêu cầu đã dùng cho quy trình khác. Tải lại trước khi tiếp tục.')
                    return previous_job
                raise ValueError('Mã yêu cầu đã dùng cho thao tác khác.')
        kind = body.get('kind')
        provider_steps = None
        if kind not in TASKS:
            raise ValueError('Tác vụ không được hỗ trợ.')
        if kind == 'enroll_voice':
            from voice_enrollment import enrollment_status
            from audio_inputs import local_audio_path
            from voices import provider_status
            name = body.get('name', body.get('voice_name', ''))
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c) < 32 for c in name):
                raise ValueError('Tên giọng riêng cần 1–80 ký tự, không có ký tự điều khiển.')
            reference_value = body.get('reference_path', body.get('ref_path', ''))
            if not isinstance(reference_value, str) or not reference_value or len(reference_value) > 32000:
                raise ValueError('Chọn một bản ghi tham chiếu trên máy.')
            reference = local_audio_path(reference_value)
            if reference.stat().st_size > 100 * 1024**2:
                raise ValueError('Bản ghi tham chiếu cần nhỏ hơn 100 MiB.')
            status = enrollment_status()
            if not status.get('available'):
                raise ValueError(status.get('message') or 'Bộ tạo giọng riêng chưa sẵn sàng.')
            if not provider_status().get('vieneu', {}).get('available'):
                raise ValueError('Bộ VieNeu của NPK chưa sẵn sàng để tạo giọng riêng.')
            options = body.get('params', {})
            if not isinstance(options, dict):
                raise ValueError('Thông số tạo giọng riêng không hợp lệ.')
            params = settings() | validate_params(options)
            params['reference_start'] = number(body.get('reference_start', options.get('reference_start', 0)), 0, 86400)
            params['reference_duration'] = number(body.get('reference_duration', options.get('reference_duration', 8)), 3, 8)
            project = {'id':'voice-enrollment', 'name':'Tạo giọng riêng · ' + name.strip(), 'updated':time.time(),
                       'segments':[], 'regions':[], 'artifacts':{}, 'media':{'duration':0}, 'params':params,
                       'voice_name':name.strip(), 'reference_path':str(reference)}
        elif kind in ('voice_preview', 'text_to_speech'):
            project = {'id':'voice-preview' if kind == 'voice_preview' else 'text-to-speech',
                'name':'Nghe thử giọng' if kind == 'voice_preview' else 'Tạo giọng từ văn bản',
                'updated':time.time(), 'segments':[], 'regions':[],
                'artifacts':{}, 'media':{'duration':0}, 'params':settings() | validate_params(body.get('params',{}))}
            text = body.get('sample_text', 'Xin chào, đây là giọng đọc thử của NPK Auto Sub.' if kind == 'voice_preview' else '')
            if not isinstance(text, str):
                raise ValueError('Nội dung đọc cần là văn bản.')
            text = text.strip()
            narration = body.get('narration_segments')
            if narration is not None:
                if kind != 'text_to_speech' or not isinstance(narration, list) or not 1 <= len(narration) <= 80:
                    raise ValueError('Kịch bản cần từ 1 đến 80 đoạn đọc.')
                from voices import resolve_voice
                speech_segments = []
                for row in narration:
                    if not isinstance(row, dict) or set(row) - {'text', 'voice_id', 'rate', 'pause_after_ms'}:
                        raise ValueError('Đoạn kịch bản không hợp lệ.')
                    spoken = row.get('text', '')
                    if not isinstance(spoken, str) or not spoken.strip() or len(spoken) > 10000 or '\x00' in spoken:
                        raise ValueError('Mỗi đoạn đọc cần có văn bản hợp lệ.')
                    voice = resolve_voice(row.get('voice_id') or project['params']['voice_id'], require_ready=True)
                    rate = number(row.get('rate', project['params'].get('voice_speed', 1)), .5, 2)
                    pause = row.get('pause_after_ms', 0)
                    if type(pause) is not int or not 0 <= pause <= 10000:
                        raise ValueError('Khoảng nghỉ giữa các đoạn cần 0–10.000 ms.')
                    speech_segments.append({'text': spoken.strip(), 'voice_id': voice['id'], 'rate': rate, 'pause_after_ms': pause})
                text = '\n'.join(row['text'] for row in speech_segments)
                project['speech_segments'] = speech_segments
            limit = 400 if kind == 'voice_preview' else 10000
            if not text or len(text) > limit:
                raise ValueError(f'Nội dung đọc cần 1–{limit} ký tự.')
            from voices import resolve_voice
            resolve_voice(project['params']['voice_id'], require_ready=True)
            project['speech_text'] = text
        else:
            project = project_by_id(body.get('project_id', ''))
            if 'expected_revision' in body and body['expected_revision'] != project['updated']:
                raise HTTPException(409, 'Dự án đã thay đổi. Tải lại trước khi chạy để giữ đúng phiên bản.')
            if kind == 'pipeline' and body.get('resume_from'):
                previous = get('job', str(body['resume_from']))
                if previous and (previous.get('continuation_from') or (DATA/'jobs'/previous['id']/'stage-journal.json').is_file()):
                    from pipeline_resume import continuation_status, prepare_continuation
                    jobs = all_items('job')
                    plan = continuation_status(previous, project, jobs, DATA/'jobs'/previous['id'])
                    if not plan['can_continue']:
                        raise HTTPException(400 if plan['requires_review'] or plan['errors'] else 409, plan['reason'])
                    project_override, continuation_from, recovery_from, selected = prepare_continuation(
                        previous, project, jobs, DATA/'jobs'/previous['id'])
                    body = dict(body)
                    body.pop('resume_from')
                    if selected is not None:
                        body['segment_indices'] = selected
            if project_override is not None:
                if (kind != 'pipeline' or not continuation_from or
                        project_override.get('id') != project['id'] or
                        project_override.get('updated') != project['updated'] or
                        project_override.get('source_revision') != project.get('source_revision')):
                    raise HTTPException(409, 'Bản chụp tiếp tục không còn khớp dự án hiện tại.')
                # The trusted continuation endpoint prepares this job-local
                # clone. The editor keeps its full OCR/ASR workflow choice.
                project = project_override
            # Idempotent request IDs protect retries of one HTTP request. A
            # second click has a new ID, so production admission must also own
            # a semantic project lease independently of the UI/history list.
            if kind in {'pipeline', 'render', 'auto_subtitles'}:
                from artifact_resolver import admission_fingerprint
                selected = body.get('segment_indices')
                if selected is not None and (not isinstance(selected, list) or not selected or
                        any(type(i) is not int or not 0 <= i < len(project['segments']) for i in selected)):
                    raise ValueError('Danh sách câu được chọn không hợp lệ.')
                wanted = admission_fingerprint(project, kind, selected)
                busy = [item for item in all_items('job') if item.get('project_id') == project['id'] and
                        item.get('state') in {'queued', 'running'} and
                        item.get('kind') in {'ocr', 'asr', 'translate', 'tts', 'clean', 'pipeline', 'render', 'auto_subtitles'}]
                identical = next((item for item in busy if item.get('kind') == kind and
                                  item.get('admission_fingerprint') == wanted), None)
                if identical:
                    if request_id:
                        put('job_request', request_id, {'job_id': identical['id']})
                    return identical
                if busy:
                    raise HTTPException(409, 'Dự án đang có tác vụ xử lý nội dung hoặc xuất video. Chờ hoàn tất hoặc hủy lượt trước để giữ đúng kết quả.')
            if kind in {'asr','ocr'} or (kind == 'pipeline' and project['params'].get('pipeline_extract') in {'asr','ocr'}):
                if any(j.get('project_id') == project['id'] and j.get('kind') in {'asr','ocr','pipeline'} and
                       j.get('state') in {'queued','running'} for j in all_items('job')):
                    raise HTTPException(409, 'Dự án đang có lượt nhận dạng hoặc xuất. Chờ hoàn tất hoặc hủy lượt trước.')
            if float(project['params'].get('extraction_start',0)) >= float(project['params'].get('extraction_end') or project['media']['duration']):
                raise ValueError('Khoảng nhận dạng phải có điểm kết thúc lớn hơn bắt đầu.')
            from execution_admission import require_admission
            from worker import pipeline_provider_steps
            provider_steps = pipeline_provider_steps(project, kind)
            require_admission(project, all_items('job'), kind=kind, automatic=body.get('automatic') is True,
                              remaining_steps=provider_steps)
            from preflight import inspect
            checks = inspect(project, kind)
            if not checks['ok']:
                raise ValueError('\n'.join(checks['errors']))
        if kind in {'asr', 'ocr', 'translate','pipeline', 'auto_subtitles'} and project['segments'] and not body.get('replace_segments'):
            raise ValueError('Tác vụ này thay đổi bảng phụ đề. Hãy xác nhận thay thế; bản chụp cũ vẫn lưu trong job.')
        identifier = uuid.uuid4().hex
        moment = number(body.get('time', 0), 0, max(0, project['media']['duration']))
        job = {'id': identifier, 'project_id': project['id'], 'project_name': project['name'], 'kind': kind,
               'created': time.time(), 'state': 'queued', 'progress': 0, 'message': 'Đang chờ', 'time': moment,
               'project_revision': project['updated'], 'stage': 'queued', 'events': [],
               'source_revision': project.get('source_revision'),
               'configuration': {key: project['params'].get(key) for key in ('method', 'workload_mode', 'mask_source', 'max_side',
                   'voice_id','provider','translation_model','video_encoder','pipeline_extract','pipeline_translate','pipeline_tts','pipeline_clean',
                   'tts_device','voice_speed','pronunciation_dictionary','tts_timing_mode','tts_auto_schedule','tts_text_source','tts_expressive','asr_compute',
                   'voice_normalize','bgm_ducking','subtitle_karaoke','subtitle_preset','subtitle_automation')}}
        if project['params'].get('provider') == 'router9' or project['params'].get('subtitle_automation') == 'router9':
            from providers import router_base_url
            job['configuration']['router9_base_url'] = router_base_url(project['params'].get('router9_base_url'))
        if kind in {'pipeline', 'render', 'auto_subtitles'}:
            from artifact_resolver import admission_fingerprint, render_fingerprint
            job['admission_fingerprint'] = admission_fingerprint(project, kind, body.get('segment_indices'))
            job['render_input_fingerprint'] = render_fingerprint(project)
        if continuation_from:
            job['continuation_from'] = continuation_from
            prior_result = get('job', continuation_from).get('result', {})
            job['completed_steps'] = list(prior_result.get('completed_steps', prior_result.get('steps', [])))
            job['message'] = 'Đang chờ chạy các bước còn lại · dùng phụ đề đã nhận dạng'
        if recovery_from:
            job['recovery_from'] = recovery_from
        if body.get('automatic') is True:
            job.update(automatic=True, execution_origin='automation')
        if kind in ('voice_preview', 'text_to_speech'):
            job.update(standalone=True, sample_text=text)
            if project.get('speech_segments'):
                job['speech_segments'] = project['speech_segments']
            from voice_samples import sample_signature
            job['sample_signature'] = sample_signature(project['params'], text)
            if kind == 'text_to_speech':
                job['sample_signature'] = 'text-to-speech:' + job['sample_signature']
                if project.get('speech_segments'):
                    import hashlib
                    from voices import audio_identity
                    voices_used = [audio_identity(resolve_voice(r['voice_id'], require_ready=True), project['params']) for r in project['speech_segments']]
                    job['sample_signature'] = hashlib.sha256(json.dumps([job['sample_signature'], project['speech_segments'], voices_used], sort_keys=True).encode()).hexdigest()
            for previous in all_items('job'):
                if previous.get('kind') != kind or previous.get('sample_signature') != job['sample_signature']:
                    continue
                if previous['state'] in ('queued', 'running'):
                    return previous
                expected_files = ('sample.wav',) if kind == 'voice_preview' else ('speech.wav', 'speech.mp3')
                if previous['state'] == 'done' and all((DATA/'jobs'/previous['id']/name).is_file() for name in expected_files):
                    return previous
        elif kind == 'enroll_voice':
            job.update(standalone=True, voice_name=project['voice_name'])
        if 'segment_indices' in body:
            indices = body['segment_indices']
            if not isinstance(indices,list) or not indices or any(type(i) is not int or i<0 or i>=len(project['segments']) for i in indices):
                raise ValueError('Danh sách câu được chọn không hợp lệ.')
            job['segment_indices'] = sorted(set(indices))
        if body.get('retry_from'):
            previous = get('job', str(body['retry_from']))
            if (kind != 'translate' or body.get('resume_from') or not previous or
                    previous.get('project_id') != project['id'] or previous.get('kind') != kind or
                    previous.get('state') not in ('error', 'cancelled')):
                raise ValueError('Không có lượt dịch dở tương ứng để thử lại đúng phạm vi.')
            from studio_api import translation_retry_indices
            retry_indices = translation_retry_indices(previous, project)
            if 'segment_indices' in job and job['segment_indices'] != retry_indices:
                raise ValueError('Phạm vi thử lại đã thay đổi. Mở lại Chi tiết để kiểm tra các câu còn thiếu.')
            job['segment_indices'] = retry_indices
            job['retry_from'] = previous['id']
        needs_translation = kind == 'translate' or kind == 'pipeline' and (
            'translate' in provider_steps if provider_steps is not None else project['params'].get('pipeline_translate', True))
        if needs_translation:
            from provider_usage import (prepare_lineage, prepare_continuation_lineage,
                                        ensure_continuation_lineage_folder, lineage_status)
            lineage_root = continuation_from or job['id']
            if kind == 'translate' and (body.get('retry_from') or body.get('resume_from')):
                previous = get('job', str(body.get('retry_from') or body.get('resume_from')))
                if (not previous or previous.get('kind') != kind or previous.get('project_id') != project['id'] or
                        previous.get('source_revision') != project.get('source_revision') or
                        previous.get('state') not in ('error', 'cancelled')):
                    raise ValueError('Không có tác vụ dịch cùng dự án/nguồn để tiếp tục ngân sách đã lưu.')
                lineage_root = (previous.get('api_usage_lineage') or {}).get('root_id') or previous['id']
                if body.get('resume_from'):
                    job['resume_from'] = previous['id']
            prepare_budget = prepare_continuation_lineage if continuation_from else prepare_lineage
            job['api_usage_lineage'] = prepare_budget(
                project, kind, lineage_root, all_items('job'),
                jobs_root=DATA/'jobs', segment_indices=job.get('segment_indices'))
            budget = lineage_status(job['api_usage_lineage'], jobs_root=DATA/'jobs')
            if not budget['execution_allowed']:
                raise HTTPException(409, budget['blocked_reason'])
            ensure_continuation_lineage_folder(job['api_usage_lineage'], jobs_root=DATA/'jobs')
        if kind == 'optimize_spoken' or needs_translation:
            import hashlib
            provider = project['params']['provider']
            secret = active_secret(provider)
            if not secret:
                raise ValueError('Chưa chọn API key cho nhà cung cấp. Mở Cài đặt trước khi gửi lời đọc để tinh chỉnh.')
            job['credential_ref'] = {'id':active_key_id(provider), 'fingerprint':hashlib.sha256(secret.encode()).hexdigest()}
        director_mode = project['params'].get('subtitle_automation')
        needs_director = kind in ('ocr','auto_subtitles')
        if kind == 'pipeline' and director_mode in ('gemini', 'router9'):
            from auto_subtitle_director import automation_status
            needs_director = project['params'].get('pipeline_extract') == 'ocr' or (
                project['params'].get('pipeline_extract','existing') == 'existing' and automation_status(project).get('can_process'))
        if director_mode in ('gemini', 'router9') and needs_director:
            import hashlib
            director_provider = director_mode
            secret = active_secret(director_provider)
            if not secret:
                raise ValueError('Chưa chọn API key ' + ('9Router' if director_provider == 'router9' else 'Gemini') + ' cho điều phối phụ đề.')
            job['director_credential_ref'] = {'id':active_key_id(director_provider), 'fingerprint':hashlib.sha256(secret.encode()).hexdigest()}
        folder = DATA / 'jobs' / identifier
        if body.get('resume_from') and kind in ('ocr', 'asr', 'translate', 'pipeline'):
            import shutil
            previous=get('job',str(body['resume_from']))
            if not previous or previous['project_id']!=project['id'] or previous['kind']!=kind or previous['state'] not in ('error','cancelled'):
                raise ValueError('Không có lượt xử lý dở tương ứng để tiếp tục.')
            filename = {'ocr':'recognition-checkpoint.json', 'asr':'asr-checkpoint.json'}.get(kind, 'translation-checkpoint.json')
            recognition = kind
            if kind == 'pipeline' and not (DATA/'jobs'/previous['id']/filename).is_file():
                recognition = previous.get('configuration', {}).get('pipeline_extract')
                filename = {'ocr':'recognition-checkpoint.json', 'asr':'asr-checkpoint.json'}.get(recognition, filename)
            checkpoint=safe_owned(DATA/'jobs'/previous['id']/filename,DATA/'jobs')
            if not checkpoint.is_file():raise ValueError('Lượt trước chưa kịp lưu tiến độ của bước này.')
            if filename == 'recognition-checkpoint.json':
                from ocr_checkpoint import validate_resume
                validate_resume(checkpoint, project)
            elif filename == 'asr-checkpoint.json':
                from asr_checkpoint import validate_resume
                validate_resume(checkpoint, project)
            else:
                from translation_checkpoint import read_checkpoint, validate_resume, validate_pipeline_resume
                validate = validate_pipeline_resume if kind == 'pipeline' and project['params'].get('pipeline_extract') in ('ocr', 'asr') else validate_resume
                validate(read_checkpoint(checkpoint), project, job.get('segment_indices'))
            folder.mkdir()
            shutil.copyfile(checkpoint,folder/filename)
            if filename == 'recognition-checkpoint.json':
                document = json.loads(checkpoint.read_text(encoding='utf-8'))
                if document.get('ocr_tracks'):
                    evidence = safe_owned(checkpoint.parent/'ocr-observations.jsonl', DATA/'jobs')
                    if not evidence.is_file():
                        raise ValueError('Checkpoint thiếu chứng cứ OCR; không tự tiếp tục bằng dữ liệu không đủ.')
                    shutil.copyfile(evidence, folder/'ocr-observations.jsonl')
            job['resume_from']=previous['id']
        else:
            if body.get('resume_from'):
                raise ValueError('Bước này chưa có checkpoint để tiếp tục tự động.')
            folder.mkdir()
        if kind == 'auto_subtitles' or (kind == 'pipeline' and project['params'].get('pipeline_extract','existing')=='existing' and
                director_mode in ('local','gemini','router9')):
            from auto_subtitle_director import evidence_for_project, automation_status
            if kind == 'auto_subtitles' or automation_status(project).get('can_process'):
                import shutil
                evidence_job, document = evidence_for_project(project)
                source = safe_owned(DATA/'jobs'/evidence_job['id']/'ocr-tracks.json', DATA/'jobs')
                shutil.copyfile(source, folder/'ocr-tracks.json')
                decision_cache = safe_owned(DATA/'jobs'/evidence_job['id']/'subtitle-automation.json', DATA/'jobs')
                if decision_cache.is_file() and decision_cache.stat().st_size <= 1024*1024:
                    shutil.copyfile(decision_cache, folder/'subtitle-automation.json')
                job['automation_evidence_job_id'] = evidence_job['id']
        (folder / 'input.json').write_text(json.dumps({'project': project, 'job': job}, ensure_ascii=False, indent=2), encoding='utf-8')
        saved = put('job', identifier, job)
        if request_id:
            put('job_request',request_id,{'job_id':identifier})
        return saved

def kill_owned_worker(identifier=None):
    global active_process
    with active_lock:
        if active_process is None or (identifier and active_id != identifier) or active_process.poll() is not None:
            return
        try:
            parent = psutil.Process(active_process.pid)
            children = parent.children(recursive=True)
            for process in reversed(children):
                try:
                    process.kill()
                except psutil.NoSuchProcess:
                    pass
            parent.kill()
        except psutil.NoSuchProcess:
            pass

@app.post('/api/jobs/{identifier}/cancel')
def cancel(identifier: str):
    with state_lock:
        job = get('job', identifier)
        if not job:
            raise HTTPException(404)
        if job['state'] in {'queued', 'running'}:
            job.update(state='cancelled', message='Đã hủy', ended=time.time())
            put('job', identifier, job)
    kill_owned_worker(identifier)
    remaining = terminate_job_workers(identifier)
    if remaining:
        job['message'] = 'Đã yêu cầu hủy; còn worker chưa dừng. Hãy thử Hủy lại hoặc xem log.'
        put('job', identifier, job)
    return job

@app.post('/api/jobs/{identifier}/open')
def open_job(identifier: str):
    if not get('job', identifier):
        raise HTTPException(404)
    path = safe_owned(DATA / 'jobs' / identifier, DATA / 'jobs')
    if not path.is_dir():
        raise ValueError('Dữ liệu tác vụ đã được dọn. Xem Dung lượng & thùng rác để khôi phục nếu còn.')
    os.startfile(path)
    return {'opened': True}

@app.get('/media/source/{identifier}')
def source_media(identifier: str):
    return FileResponse(project_by_id(identifier)['media']['path'])


@app.get('/media/external/{identifier}/{kind}')
def external_audio(identifier: str, kind: str):
    from audio_inputs import local_audio_path
    if kind not in {'voice', 'bgm'}:
        raise HTTPException(404)
    project = project_by_id(identifier)
    value = project['params'].get(kind + '_path')
    if not value:
        raise HTTPException(404, 'Dự án chưa chọn tệp âm thanh này.')
    return FileResponse(local_audio_path(value))

@app.get('/media/job/{identifier}/{filename}')
def job_media(identifier: str, filename: str):
    job = get('job', identifier)
    if not job or filename not in job.get('result', {}).get('files', []):
        raise HTTPException(404)
    if filename == 'final.mp4':
        # The viewer and open-result must resolve the same verified copy. A
        # published file can outlive, or remain intact after changes to, the
        # technical local copy. Never stream a known corrupt local result.
        from artifact_resolver import final_result_path
        try:
            return FileResponse(final_result_path(job, DATA))
        except (OSError, ValueError, TypeError) as error:
            raise HTTPException(410, str(error)) from error
    path = safe_owned(DATA / 'jobs' / identifier / filename, DATA / 'jobs')
    if not path.is_file():
        raise HTTPException(410, 'Tệp xử lý đã được dọn; khôi phục từ thùng rác hoặc tạo lại.')
    return FileResponse(path)

def _stop_job_processes(identifier):
    """Stop exact app/job command lines even before cancellation reaches SQLite."""
    from process_control import find_job_workers
    victims = {}
    for parent in find_job_workers(identifier):
        try:
            for child in parent.children(recursive=True):
                victims[child.pid] = child
            victims[parent.pid] = parent
        except psutil.NoSuchProcess:
            pass
    for process in reversed(list(victims.values())):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(victims.values()), timeout=3)
    if active_id == identifier and active_process is not None:
        try:
            active_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            return [active_process.pid]
    return [p.pid for p in alive if p.is_running()]


def _recover_abandoned_jobs():
    """Never replay an ambiguous running job after a database/process failure."""
    if active_process is not None and active_process.poll() is None:
        kill_owned_worker(active_id)
        if _stop_job_processes(active_id):
            raise RuntimeError('Bộ xử lý cũ chưa dừng; hàng đợi giữ nguyên để tránh chạy trùng.')
    for previous in all_items('job'):
        if previous.get('state') == 'running':
            if _stop_job_processes(previous['id']):
                raise RuntimeError('Bộ xử lý cũ chưa dừng; cần hủy lại trước khi tiếp tục.')
            previous.update(state='error', stage='interrupted', requires_recovery=True, ended=time.time(),
                            message='Tác vụ bị gián đoạn. Kết quả đã tạo vẫn giữ trên đĩa; kiểm tra trước khi chạy lại. Không tự chạy trùng.')
            put('job', previous['id'], previous)


def worker_loop():
    delay = .5
    while not stop_event.is_set():
        try:
            _worker_loop_run()
            return
        except Exception as exc:
            queue_runtime.update(healthy=False, last_error='Hàng đợi tạm gián đoạn (' + type(exc).__name__ + '). Đang thử kết nối lại.',
                                 consecutive_errors=queue_runtime['consecutive_errors'] + 1)
            if stop_event.wait(delay):
                return
            delay = min(10, delay * 2)


def attach_completed_result(job, result):
    """Publish only to the project revision that supplied this worker's input.

    A completed file remains a useful standalone job result after edits. It must
    not replace the editor's current TTS/clean artifact or subtitle table, and an
    artifact-only update must invalidate the old editor revision too.
    The caller holds state_lock, so checking and saving cannot race with the UI.
    """
    project = get('project', job['project_id'])
    if (not project or project.get('source_revision') != job.get('source_revision') or
            project.get('updated') != job.get('project_revision')):
        return False
    previous = json.loads(json.dumps(project))
    project.setdefault('artifacts', {})[job['kind']] = result | {'job_id': job['id']}
    if job['kind'] == 'pipeline':
        for name, value in result.get('artifacts', {}).items():
            project['artifacts'][name] = value | {'job_id': value.get('job_id', job['id'])}
    if 'segments' in result and job['kind'] != 'optimize_spoken':
        project['segments'] = result['segments']
        if result.get('coverage'):
            project['coverage'] = result['coverage']
    project['updated'] = max(time.time(), previous['updated'] + .000001)
    save_project_version(project, previous)
    return True


def _worker_loop_run():
    global active_process, active_id
    while not stop_event.wait(.5):
        with state_lock:
            _recover_abandoned_jobs()
            control = get('queue_control', 'main') or {}
            queue_runtime.update(healthy=True, last_error='', consecutive_errors=0, last_success=time.time())
            if control.get('paused'):
                continue
            queued = sorted((j for j in all_items('job') if j['state'] == 'queued'), key=lambda j: (j.get('queue_order', j['created']), j['created']))
            if not queued:
                continue
            job = queued[0]
            folder = DATA / 'jobs' / job['id']
            from execution_admission import admission_status, require_execution_contract, AdmissionError
            try:
                packet = json.loads((folder/'input.json').read_text(encoding='utf-8'))
                snapshot, queued_input = packet['project'], packet['job']
                if (snapshot.get('id') != job['project_id'] or queued_input.get('id') != job['id'] or
                        queued_input.get('project_id') != job['project_id'] or
                        queued_input.get('kind') != job['kind'] or
                        snapshot.get('updated') != job.get('project_revision') or
                        snapshot.get('source_revision') != job.get('source_revision')):
                    raise ValueError('snapshot identity mismatch')
                for key in ('provider', 'translation_model', 'pipeline_translate', 'subtitle_automation', 'pipeline_extract', 'router9_base_url'):
                    if key in (job.get('configuration') or {}) and snapshot.get('params', {}).get(key) != job['configuration'][key]:
                        raise ValueError('snapshot provider configuration mismatch')
                current_project = get('project', job['project_id'])
                from worker import pipeline_provider_steps
                provider_steps = pipeline_provider_steps(snapshot, job['kind'])
                require_execution_contract(snapshot, current_project, job['kind'], remaining_steps=provider_steps)
                if current_project:
                    if current_project.get('source_revision') != job.get('source_revision'):
                        raise ValueError('queued source was replaced')
                    if job.get('source_revision') is None:
                        if any((current_project.get('media') or {}).get(key) != snapshot.get('media', {}).get(key)
                               for key in ('path', 'duration', 'width', 'height', 'fps')):
                            raise ValueError('legacy queued source was replaced')
                admission = admission_status(snapshot, all_items('job'), kind=job['kind'],
                    current_job_id=job['id'], automatic=bool(job.get('automatic')), remaining_steps=provider_steps)
                if (admission['execution_allowed'] and job.get('api_usage_lineage') and
                        (provider_steps is None or 'translate' in provider_steps)):
                    from provider_usage import lineage_status, validate_lineage
                    validate_lineage(job['api_usage_lineage'], snapshot)
                    budget = lineage_status(job['api_usage_lineage'], jobs_root=DATA/'jobs')
                    if not budget['execution_allowed']:
                        admission.update(execution_allowed=False, blocked_code=budget['blocked_code'],
                                         blocked_reason=budget['blocked_reason'])
            except AdmissionError as error:
                admission = error.report
            except (OSError, ValueError, KeyError, TypeError):
                admission = {'execution_allowed': False, 'blocked_code': 'invalid_snapshot',
                             'blocked_reason': 'Bản chụp tác vụ không hợp lệ; chưa chạy bộ xử lý.'}
            if not admission['execution_allowed']:
                job.update(state='error', ended=time.time(), failure_code=admission['blocked_code'],
                           failure_stage='admission', next_action='inspect_execution_admission',
                           message=admission['blocked_reason'], execution_admission=admission)
                put('job', job['id'], job)
                continue
            job.update(state='running', stage='prepare', message='Đang khởi động…', started=time.time())
            put('job', job['id'], job)
        folder = DATA / 'jobs' / job['id']
        result, error = None, None
        try:
            env = os.environ.copy()
            env['PYTHONIOENCODING'] = 'utf-8'
            env['PYTHONUNBUFFERED'] = '1'
            env['HF_HOME'] = str(MODELS / 'huggingface')
            env['VIETDUB_DATA_DIR'] = str(DATA)
            env['VIETDUB_OWNER_PID'] = str(os.getpid())
            env['VIETDUB_OWNER_CREATED'] = str(psutil.Process().create_time())
            with (folder / 'worker.log').open('w', encoding='utf-8') as log:
                with active_lock:
                    if stop_event.is_set():
                        break
                    with state_lock:
                        if get('job', job['id'])['state'] != 'running':
                            continue
                    worker_python = ROOT / 'runtime/venv/Scripts/python.exe'
                    from live_clean_preview import cancel_active as cancel_live_preview
                    cancel_live_preview()
                    active_process = subprocess.Popen([str(worker_python), '-u', str(ROOT / 'worker.py'), str(folder / 'input.json')],
                        stdout=subprocess.PIPE, stderr=log, text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW, env=env)
                    active_id = job['id']
                for line in active_process.stdout:
                    try:
                        event = json.loads(line)
                    except (ValueError, TypeError):
                        log.write(line)
                        log.flush()
                        continue
                    if 'result' in event:
                        result = event['result']
                    if 'error' in event:
                        error = str(event['error'])
                    with state_lock:
                        current = get('job', job['id'])
                        if current['state'] == 'running':
                            accept_event(current, event)
                            put('job', job['id'], current)
                exit_code = active_process.wait()
            with state_lock:
                current = get('job', job['id'])
                if current['state'] != 'cancelled':
                    if exit_code == 0 and isinstance(result, dict) and result.get('verification'):
                        if job['kind'] == 'enroll_voice':
                            from voice_enrollment import register_voice
                            result['voice_id'] = register_voice(folder)
                            from vieneu_voice import entries as vieneu_entries
                            vieneu_entries(force=True)
                        review = bool(result.get('needs_review'))
                        current.update(state='review' if review else 'done', stage='review' if review else 'done',
                            progress=current.get('progress',0) if review else 1,
                            message='Cần kiểm tra phụ đề trước khi tiếp tục' if review else 'Đã xử lý xong · cần xem/nghe để duyệt', result=result)
                        if job.get('standalone'):
                            if result.get('voice_id'):
                                put('voice_test',result['voice_id'],{'ok':True,'checked_at':time.time(),'job_id':job['id']})
                        else:
                            current['project_applied'] = attach_completed_result(job, result)
                            if not current['project_applied']:
                                current['message'] = 'Hoàn tất; dự án đã thay đổi trong lúc chạy. Kết quả lưu riêng, chưa thay phụ đề hoặc giọng/video của dự án.'
                    else:
                        current.update(state='error', message=error or 'Worker kết thúc bất thường. Xem worker.log trong thư mục kết quả.')
                        from stage_journal import summary as stage_summary
                        partial = stage_summary(folder)
                        if partial:
                            current['stage_journal'] = partial
                    current['ended'] = time.time()
                    put('job', job['id'], current)
        except Exception as exception:
            kill_owned_worker(job['id'])
            if _stop_job_processes(job['id']):
                raise RuntimeError('Bộ xử lý chưa dừng hoàn toàn. Hàng đợi sẽ thử dừng lại, không chạy thêm tác vụ.') from exception
            with state_lock:
                current = get('job', job['id'])
                if current['state'] != 'cancelled':
                    current.update(state='error', message=str(exception), ended=time.time())
                    put('job', job['id'], current)
        finally:
            if active_process is not None and active_process.poll() is None:
                kill_owned_worker(job['id'])
                if _stop_job_processes(job['id']):
                    raise RuntimeError('Bộ xử lý chưa dừng hoàn toàn; giữ hàng đợi để tránh chạy trùng.')
            with active_lock:
                active_process, active_id = None, None

def start_workers():
    global queue_file_lock, queue_thread
    if queue_file_lock is not None and not queue_file_lock.closed:
        raise RuntimeError('Hàng đợi trong tiến trình này đã được khởi động.')
    import msvcrt
    queue_file_lock = (DATA / 'queue.lock').open('a+b')
    if (DATA / 'queue.lock').stat().st_size == 0:
        queue_file_lock.write(b'0')
        queue_file_lock.flush()
    queue_file_lock.seek(0)
    try:
        msvcrt.locking(queue_file_lock.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        queue_file_lock.close()
        queue_file_lock = None
        raise RuntimeError('Đã có một bộ xử lý dùng thư mục dữ liệu này. Không chạy thêm hàng đợi.')
    # Interrupted jobs are reconciled inside the resilient queue loop, after
    # their exact worker processes have stopped. Database errors must not abort
    # app startup or silently replay an ambiguous running job.
    queue_thread = threading.Thread(target=worker_loop, name='NPKQueue', daemon=True)
    queue_thread.start()

def shutdown():
    stop_event.set()
    from live_clean_preview import cancel_active as cancel_live_preview
    cancel_live_preview()
    kill_owned_worker()
    if queue_thread is not None and queue_thread is not threading.current_thread():
        queue_thread.join(timeout=5)
    if owner_heartbeat_thread is not None and owner_heartbeat_thread is not threading.current_thread():
        owner_heartbeat_thread.join(timeout=2)
    if queue_file_lock is not None and (queue_thread is None or not queue_thread.is_alive()):
        queue_file_lock.close()
    if owner_record is not None:
        from owner_connection import remove
        remove(DATA, owner_record)

from studio_api import install as install_studio
install_studio(app)
app.mount('/static', StaticFiles(directory=ROOT / 'static'), name='static')

@app.get('/')
def index():
    return FileResponse(ROOT / ('static/editor.html' if os.environ.get('NPK_LEGACY_UI')=='1' else 'static/studio.html'))

@app.get('/editor')
def previous_editor():
    return FileResponse(ROOT / 'static/editor.html')

@app.get('/legacy')
def legacy_index():
    return FileResponse(ROOT / 'static/index.html')
