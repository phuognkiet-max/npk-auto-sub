"""NPK-owned VieNeu GGUF discovery and local subprocess synthesis (no HTTP)."""
from __future__ import annotations
import atexit
import functools
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid
import wave
from pronunciation import apply_pronunciation, dictionary_identity

ROOT = Path(__file__).resolve().parent
MODEL_ROOT = ROOT / 'models/vieneu-v3-turbo'
RUNTIME = ROOT / 'runtime'
_client = None
_client_lock = threading.Lock()


@functools.lru_cache(maxsize=32)
def _read_cached(path, size, modified):
    if size > 2 * 1024 * 1024:
        raise ValueError('Tệp metadata VieNeu quá lớn.')
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _read(path):
    stat = path.stat()
    return _read_cached(str(path), stat.st_size, stat.st_mtime_ns)


def _cli(backend):
    folder = RUNTIME / 'audio-cpp' / backend
    return next(iter(sorted(folder.rglob('audiocpp_cli.exe'))), folder / 'audiocpp_cli.exe')


def _installation():
    try:
        document = _read(MODEL_ROOT / 'installation.json')
        return document if isinstance(document, dict) else {}
    except (OSError, ValueError):
        return {}


@functools.lru_cache(maxsize=128)
def _small_hash(path, size, modified):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest() if size <= 2 * 1024 * 1024 else None


def _presets():
    sdk = _read(MODEL_ROOT / 'voices_v3_turbo.json')
    manifest = _read(MODEL_ROOT / 'voices/manifest.json')
    packaged = {row['name']: row for row in manifest['voices']}
    result = []
    for name, row in sdk['presets'].items():
        matches = [packaged[candidate] for candidate in [name, *row.get('aliases', [])] if candidate in packaged]
        if not matches:
            continue
        slug = matches[0]['id']
        if not isinstance(slug, str) or not slug or any(char not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for char in slug):
            raise ValueError('Mã preset VieNeu không hợp lệ.')
        result.append(dict(id='vieneu:' + name, name=name, voice_slug=slug, language='vi', provider='vieneu',
            sample_rate=48000, description=row.get('description', ''), gender=row.get('gender', ''),
            featured=row.get('featured') is not None, region={'Nam': 'South', 'Trung': 'Central', 'Bắc': 'North'}.get(
                row.get('description', '').split('·')[1].strip() if '·' in row.get('description', '') else '', ''),
            license_note='VieNeu-TTS của Phạm Nguyễn Ngọc Bảo; xem giấy phép model trong tài liệu NPK.'))
    return result


def _fingerprint(installation):
    assets = []
    for path in (MODEL_ROOT / 'model.gguf', MODEL_ROOT / 'voices_v3_turbo.json', MODEL_ROOT / 'voices/manifest.json',
                 MODEL_ROOT / 'installation.json', _cli(installation.get('backend', 'cpu'))):
        try:
            stat = path.stat()
            assets.append([str(path.relative_to(ROOT)), stat.st_size, stat.st_mtime_ns,
                _small_hash(str(path), stat.st_size, stat.st_mtime_ns) if path.suffix == '.json' else None])
        except (OSError, ValueError):
            assets.append([str(path), None])
    return dict(adapter='npk-vieneu-gguf-v1', backend=installation.get('backend', 'cpu'), precision='q8_0',
        model_revision=installation.get('model_revision'), model_sha256=installation.get('model_sha256'),
        audio_cpp_version=installation.get('audio_cpp_version'), assets=assets, sample_rate=48000,
        seed=42, phonemizer='sea-g2p-0.10.0', threads=installation.get('threads', 4))


def provider_status(force=False):
    installation = _installation()
    backend = installation.get('backend', 'cpu')
    devices = [device for device in ('cpu', 'cuda') if _cli(device).is_file()]
    missing = []
    if backend not in ('cpu', 'cuda'):
        missing.append('thiết bị hợp lệ CPU/CUDA')
        backend = 'cpu'
    if backend not in devices and devices:
        backend = 'cpu' if 'cpu' in devices else devices[0]
    for path in (RUNTIME / 'vieneu/Scripts/python.exe', _cli(backend), MODEL_ROOT / 'model.gguf'):
        if not path.is_file():
            missing.append(path.name)
    try:
        rows = _presets()
        if len(rows) != 25:
            missing.append('đủ 25 preset')
        for row in rows:
            if any(not (MODEL_ROOT / 'voices' / row['voice_slug'] / name).is_file()
                   for name in ('ref_codes.txt', 'speaker.emb.txt')):
                missing.append('dữ liệu giọng ' + row['name'])
    except (OSError, ValueError, KeyError, TypeError):
        missing.append('danh mục preset VieNeu')
    if installation.get('verified') is not True:
        missing.append('xác minh cài đặt')
    ready = not missing
    return dict(provider='vieneu', available=ready, state='ready' if ready else 'not_installed',
        engine='VieNeu-TTS v3 Turbo · GGUF Q8', backend='audio.cpp', device=backend, precision='q8_0',
        sample_rate=48000, checked_at=time.time(),
        devices=devices,
        message='Engine và giọng đã cài trong NPK.' if ready else
            'VieNeu độc lập chưa sẵn sàng: ' + ', '.join(missing[:4]) + '. Hoàn tất cài engine/model NPK trước.')


def entries(force=False):
    state = provider_status(force)
    try:
        rows = _presets()
    except (OSError, ValueError, KeyError, TypeError):
        return []
    try:
        from voice_enrollment import custom_voices
        rows += custom_voices()
    except (ImportError, OSError, ValueError):
        pass
    identity = _fingerprint(_installation())
    result = []
    for row in rows:
        voice_assets = []
        for name in ('ref_codes.txt', 'speaker.emb.txt'):
            path = _voice_directory(row) / name
            try:
                stat = path.stat()
                voice_assets.append([name, stat.st_size, stat.st_mtime_ns,
                    _small_hash(str(path), stat.st_size, stat.st_mtime_ns)])
            except OSError:
                voice_assets.append([name, None])
        result.append(row | dict(available=state['available'], state=state['state'],
            cache_identity=identity | {'voice_assets': voice_assets},
            unavailable_reason='' if state['available'] else state['message']))
    return result


def _voice_directory(row):
    if row.get('is_custom'):
        from voice_enrollment import _library
        folder = Path(row['voice_directory']).resolve()
        if folder.parent != _library():
            raise ValueError('Giọng riêng không thuộc thư viện NPK.')
        return folder
    return MODEL_ROOT / 'voices' / row['voice_slug']


def resolve_voice(identifier, require_ready=False):
    rows = entries()
    for row in rows:
        if row['id'] == identifier:
            if require_ready and not row['available']:
                raise ValueError(row['unavailable_reason'])
            return row
    raise ValueError('Giọng VieNeu chưa có trong bộ model độc lập NPK. Hoàn tất cài đặt hoặc chọn lại preset.')


class _EngineClient:
    def __init__(self):
        self.owner = os.getpid()
        self.results = queue.Queue()
        self.lock = threading.Lock()
        env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1',
                   PYTHONUTF8='1', PYTHONPATH='', HF_HOME=str(MODEL_ROOT / 'offline-cache'))
        self.process = subprocess.Popen([str(RUNTIME / 'vieneu/Scripts/python.exe'), '-u',
            str(ROOT / 'tools/vieneu_engine.py'), '--serve'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, encoding='utf-8', env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

        def read():
            try:
                for line in self.process.stdout:
                    try:
                        self.results.put(json.loads(line))
                    except ValueError:
                        pass
            finally:
                self.results.put({'error': 'Tiến trình VieNeu đã dừng. Kiểm tra cài đặt engine NPK rồi thử lại.'})

        threading.Thread(target=read, daemon=True).start()

    def request(self, payload, progress=None):
        with self.lock:
            if self.process.poll() is not None:
                raise ValueError('Engine VieNeu đã dừng; chạy lại tác vụ để khởi động lại engine NPK.')
            self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + '\n')
            self.process.stdin.flush()
            deadline = time.monotonic() + 900
            while True:
                try:
                    result = self.results.get(timeout=max(.01, deadline - time.monotonic()))
                except queue.Empty:
                    self.close()
                    raise ValueError('VieNeu xử lý quá lâu; đã dừng engine. Rút ngắn văn bản hoặc kiểm tra tải máy.') from None
                if result.get('event') == 'progress':
                    if progress:
                        try:
                            progress({key: value for key, value in result.items() if key != 'event'})
                        except BaseException:
                            self.close()
                            raise
                    continue
                if result.get('error'):
                    raise ValueError(result['error'])
                return result

    def close(self):
        if self.process.poll() is None:
            # Native inference children must not survive this broker.
            try:
                import psutil
                try:
                    for child in psutil.Process(self.process.pid).children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                except psutil.NoSuchProcess:
                    pass
            except (ImportError, OSError):
                pass
            self.process.kill()
        self.process.wait(timeout=10)


def _engine():
    global _client
    with _client_lock:
        if _client is None or _client.owner != os.getpid() or _client.process.poll() is not None:
            _client = _EngineClient()
        return _client


def _close():
    if _client is not None:
        try:
            _client.close()
        except (OSError, subprocess.TimeoutExpired):
            pass


atexit.register(_close)


def runtime_configuration(params=None):
    """Explicit project device/profile overrides packaged defaults without mutating them."""
    installation = _installation()
    configured = installation.get('backend', 'cpu')
    requested = (params or {}).get('tts_device', 'auto')
    if requested not in ('auto', 'cpu', 'cuda'):
        raise ValueError('Thiết bị tạo giọng cần là Tự động, CPU hoặc CUDA.')
    backend = configured if requested == 'auto' else requested
    if backend not in ('cpu', 'cuda'):
        raise ValueError('Thiết bị VieNeu trong cấu hình cài đặt không hợp lệ.')
    if not _cli(backend).is_file():
        if requested == 'auto' and _cli('cpu').is_file():
            backend = 'cpu'
        else:
            raise ValueError(f'Chưa cài bộ xử lý VieNeu {backend.upper()}. Chọn thiết bị đã cài hoặc cài bổ sung runtime.')
    if params is None:
        return dict(backend=backend, threads=installation.get('threads', 4), duty=1., batch_size=64)
    from performance import profile_for
    profile = profile_for(params)
    return dict(backend=backend, threads=profile['threads'], duty=profile['duty'],
                batch_size={'quiet': 24, 'balanced': 64, 'fast': 128}.get(params.get('workload_mode', 'quiet'), 24))


def synthesis_identity(entry, params=None):
    configuration = runtime_configuration(params)
    cli = _cli(configuration['backend'])
    stat = cli.stat()
    identity = dict(version='npk-vieneu-batch-v2', voice=entry['cache_identity'],
                configuration=configuration, cli=[stat.st_size, stat.st_mtime_ns],
                pronunciation=dictionary_identity((params or {}).get('pronunciation_dictionary', '')))
    if (params or {}).get('tts_expressive', False):
        from expressive_text import EXPRESSIVE_VERSION
        identity['expressive'] = EXPRESSIVE_VERSION
    return identity


def _prepare(entry, text, path, rate, params):
    text = str(text).strip()
    try:
        speed = float(rate)
    except (TypeError, ValueError):
        raise ValueError('Tốc độ đọc VieNeu cần là số từ 0,5 đến 2.') from None
    if not text or len(text) > 10000 or not math.isfinite(speed) or not .5 <= speed <= 2:
        raise ValueError('Lời đọc VieNeu cần 1–10000 ký tự, tốc độ từ 0,5 đến 2.')
    current = resolve_voice(entry['id'], require_ready=True)
    if current['cache_identity'] != entry.get('cache_identity'):
        raise ValueError('Model hoặc cấu hình VieNeu đã đổi. Làm mới giọng rồi tạo lại.')
    from core import binary
    configuration = runtime_configuration(params)
    target = Path(path).resolve()
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp.wav')
    voice_directory = _voice_directory(current)
    from expressive_text import prepare_expressive_text
    spoken = prepare_expressive_text(text, (params or {}).get('tts_expressive', False))
    spoken = apply_pronunciation(spoken, (params or {}).get('pronunciation_dictionary', ''))
    common = dict(model=str((MODEL_ROOT / 'model.gguf').resolve()), cli=str(_cli(configuration['backend']).resolve()),
            voice_codes=str((voice_directory / 'ref_codes.txt').resolve()),
            speaker_embedding=str((voice_directory / 'speaker.emb.txt').resolve()),
            ffmpeg=binary('ffmpeg'), **configuration)
    return dict(current=current, common=common, text=spoken, rate=speed, target=target, temporary=temporary,
                identity=synthesis_identity(current, params), params=params)


def _validate_result(item):
    current = resolve_voice(item['current']['id'], require_ready=True)
    if synthesis_identity(current, item['params']) != item['identity']:
        raise ValueError('Model hoặc cấu hình VieNeu thay đổi trong lúc tạo; chưa lưu âm thanh. Làm mới giọng và thử lại.')
    try:
        with wave.open(str(item['temporary']), 'rb') as audio:
            if audio.getframerate() != 48000 or audio.getsampwidth() != 2 or audio.getnchannels() != 1 or audio.getnframes() <= 0:
                raise ValueError('Engine VieNeu chưa trả WAV PCM16 mono 48 kHz hợp lệ.')
            duration = audio.getnframes() / 48000
            remaining = audio.getnframes() * 2
            while remaining:
                block = audio.readframes(min(48000, remaining // 2))
                if not block:
                    raise ValueError('WAV VieNeu bị thiếu dữ liệu; chưa lưu bản đọc.')
                remaining -= len(block)
        from audio_verification import verify_pcm_speech
        verify_pcm_speech(item['temporary'], expected_duration=duration, expected_rate=48000)
        return duration
    except (wave.Error, EOFError):
        raise ValueError('Không đọc được WAV VieNeu hợp lệ; chưa lưu bản đọc.') from None


def _metadata(item, native, duration):
    return dict(provider='vieneu', voice_id=item['current']['id'], voice_name=item['current']['name'], sample_rate=48000,
        duration=duration, device=item['common']['backend'], threads=item['common']['threads'],
        audio_verification='pcm-complete', lexical_verification='unverified',
        **{key: native[key] for key in ('elapsed_seconds', 'speech_parts', 'chunks', 'native_batches') if key in native})


def synthesize_vieneu(entry, text, path, rate, params=None, progress=None):
    item = _prepare(entry, text, path, rate, params)
    try:
        payload = item['common'] | dict(text=item['text'], output=str(item['temporary']), rate=item['rate'])
        expanded = len(item['text']) > 10000
        if expanded:
            payload = item['common'] | {'items': [dict(text=item['text'], output=str(item['temporary']), rate=item['rate'])]}
        metadata = _engine().request(payload, progress=progress) if progress else _engine().request(payload)
        if expanded:
            metadata = metadata['items'][0] | {key: metadata[key] for key in ('elapsed_seconds', 'native_batches') if key in metadata}
        duration = _validate_result(item)
        os.replace(item['temporary'], item['target'])
        return _metadata(item, metadata, duration)
    finally:
        item['temporary'].unlink(missing_ok=True)


def synthesize_vieneu_batch(requests, params=None, progress=None):
    """Batch by voice/device; publish each complete, identity-matched utterance.

    requests: [{entry, text, path, rate, params?}], 1..256. Optional per-item
    params override project params, allowing multi-voice paragraph callers.
    On failure, valid earlier targets survive. The raised exception's
    partial_results maps request indices to metadata; an interrupted broker's
    complete WAV has unknown engine metadata and never claims lexical proof.
    """
    if not isinstance(requests, (list, tuple)) or not 1 <= len(requests) <= 256:
        raise ValueError('Batch VieNeu cần 1–256 câu.')
    prepared, groups = [], {}
    results = [None] * len(requests)
    try:
        for request in requests:
            options = params
            if 'params' in request:
                options = dict(params or {}) | request['params']
            item = _prepare(request['entry'], request['text'], request['path'], request.get('rate', 1), options)
            if any(prior['target'] == item['target'] for prior in prepared):
                raise ValueError('Các câu trong batch cần đường dẫn đầu ra riêng.')
            prepared.append(item)
            key = json.dumps(item['common'], sort_keys=True)
            groups.setdefault(key, []).append(len(prepared) - 1)
        native_results = [None] * len(prepared)
        completed = 0
        for indices in groups.values():
            first = prepared[indices[0]]
            payload = first['common'] | {'items': [dict(text=prepared[index]['text'], rate=prepared[index]['rate'],
                output=str(prepared[index]['temporary'])) for index in indices]}
            def report(event):
                if progress:
                    progress(event | {'progress': (completed + float(event.get('progress', 0)) * len(indices)) / len(prepared),
                        'cue_completed': completed, 'cue_total': len(prepared)})
            native = _engine().request(payload, progress=report) if progress else _engine().request(payload)
            if not isinstance(native.get('items'), list) or len(native['items']) != len(indices):
                raise ValueError('Engine VieNeu chưa trả đủ kết quả batch; chưa lưu giọng.')
            for index, result in zip(indices, native['items']):
                native_results[index] = result | {key: native[key] for key in ('elapsed_seconds', 'native_batches') if key in native}
                duration = _validate_result(prepared[index])
                os.replace(prepared[index]['temporary'], prepared[index]['target'])
                results[index] = _metadata(prepared[index], native_results[index], duration) | {'engine_metadata_complete': True}
            completed += len(indices)
            if progress:
                progress(dict(progress=completed / len(prepared), completed=completed, total=len(prepared),
                              message=f'Đã tạo giọng {completed}/{len(prepared)} câu'))
        return results
    except Exception as error:
        # A broker exception may follow independently published cue files.
        # Verify payload and current model identity before promoting any of
        # those files. An absent/partial/corrupt file is never a cache success.
        for index, item in enumerate(prepared):
            if results[index] is not None or not item['temporary'].is_file():
                continue
            try:
                duration = _validate_result(item)
                os.replace(item['temporary'], item['target'])
                native = native_results[index] if 'native_results' in locals() else None
                results[index] = _metadata(item, native or {}, duration) | {
                    'engine_metadata_complete': native is not None, 'recovered_after_batch_error': True}
            except (ValueError, OSError):
                continue
        error.partial_results = {index: result for index, result in enumerate(results) if result is not None}
        raise
    finally:
        for item in prepared:
            item['temporary'].unlink(missing_ok=True)
