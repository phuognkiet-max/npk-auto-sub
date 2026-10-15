"""Offline reference enrollment with real CAM++ embeddings and native codec codes.

The fbank parameters follow the Apache-2.0 VieNeu upstream speaker frontend at
85344322b7258b4e25479b692e8e3396baf9db34. Native code extraction is supported by
audio.cpp cf124a67cc55d8f65a9a15eec69edbff0fb212c8. No zero-embedding fallback.
"""
from __future__ import annotations
import functools
import hashlib
import json
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import wave

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / 'models/vieneu-enrollment'
RUNTIME = ROOT / 'runtime/voice-enrollment/python'
ENCODER_SHA256 = 'a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73'


def _sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            result.update(block)
    return result.hexdigest()


@functools.lru_cache(maxsize=8)
def _verified_file(path, size, mtime, expected):
    return _sha256(path) == expected


def enrollment_status():
    encoder = MODEL / 'speaker_encoder.onnx'
    ready = False
    try:
        record = json.loads((MODEL / 'installation.json').read_text(encoding='utf-8'))
        stat = encoder.stat()
        ready = (record.get('verified') is True and _verified_file(str(encoder), stat.st_size, stat.st_mtime_ns, ENCODER_SHA256)
                 and (RUNTIME / 'kaldi_native_fbank/__init__.py').is_file())
    except (OSError, ValueError):
        pass
    return dict(available=bool(ready), state='ready' if ready else 'not_installed',
        message='Bộ tạo giọng riêng offline đã cài trong NPK.' if ready else
                'Chưa cài bộ tạo giọng riêng. Chạy tools/setup_voice_enrollment.py để tải encoder 28 MB và frontend 308 KB.',
        sample_rate=48000, min_reference_seconds=3, max_reference_seconds=8)


def _library():
    from core import DATA
    return (DATA / 'custom-voices/vieneu').resolve()


def custom_voices():
    library = _library()
    if not library.is_dir():
        return []
    result = []
    for folder in sorted(library.iterdir()):
        if len(result) >= 128:
            break
        if not folder.is_dir() or len(folder.name) != 32 or any(char not in '0123456789abcdef' for char in folder.name):
            continue
        if folder.resolve().parent != library:
            continue
        try:
            metadata_path = folder / 'voice.json'
            if metadata_path.resolve().parent != folder.resolve() or metadata_path.stat().st_size > 32768:
                continue
            metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
            if metadata.get('id') != 'vieneu:custom:' + folder.name or metadata.get('schema') != 1:
                continue
            _validate_voice_assets(folder)
            name = _validate_name(metadata['name'])
            result.append(dict(id=metadata['id'], name=name, provider='vieneu', language='vi', sample_rate=48000,
                voice_slug='custom-' + folder.name, voice_directory=str(folder), is_custom=True,
                description='Giọng riêng · tạo từ bản ghi trên máy', gender='', region='', featured=False,
                license_note='Bản ghi tham chiếu do người dùng cung cấp; lưu và xử lý trên máy.'))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def _validate_name(name):
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(char) < 32 for char in name):
        raise ValueError('Tên giọng riêng cần từ 1 đến 80 ký tự, không chứa ký tự điều khiển.')
    return name.strip()


def _validate_voice_assets(folder):
    import numpy as np
    for name in ('ref_codes.txt', 'speaker.emb.txt'):
        path = folder / name
        if path.resolve().parent != folder.resolve() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Dữ liệu giọng riêng không hợp lệ.')
    embedding = np.fromstring((folder / 'speaker.emb.txt').read_text(encoding='utf-8'), sep=',', dtype=np.float32)
    if embedding.shape != (192,) or not np.isfinite(embedding).all() or float(np.linalg.norm(embedding)) < 1e-6:
        raise ValueError('Giọng riêng chưa có speaker embedding 192 chiều hợp lệ; không dùng vector zeros.')
    codes = np.loadtxt(folder / 'ref_codes.txt', dtype=np.float64, ndmin=2)
    if codes.ndim != 2 or codes.shape[1] != 16 or not 1 <= codes.shape[0] <= 110 or \
            not np.isfinite(codes).all() or np.any(codes != np.floor(codes)) or np.any(codes < 0) or np.any(codes >= 8192):
        raise ValueError('Reference codes giọng riêng không hợp lệ.')
    return embedding, codes


@functools.lru_cache(maxsize=4)
def _speaker_session(threads, size, modified):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(MODEL / 'speaker_encoder.onnx'), sess_options=options,
                                providers=['CPUExecutionProvider'])


def extract_embedding(reference_16k, threads=1):
    """Verified upstream CAM++ fbank frontend; input already resampled by FFmpeg."""
    if str(RUNTIME) not in sys.path:
        sys.path.insert(0, str(RUNTIME))
    import numpy as np
    import kaldi_native_fbank as knf
    with wave.open(str(reference_16k), 'rb') as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth()) != (16000, 1, 2):
            raise ValueError('Bản ghi cho speaker encoder cần PCM16 mono 16 kHz.')
        audio = np.frombuffer(source.readframes(source.getnframes()), dtype='<i2').astype(np.float32) / 32768
    options = knf.FbankOptions()
    options.frame_opts.samp_freq = 16000
    options.frame_opts.frame_shift_ms = 10
    options.frame_opts.frame_length_ms = 25
    options.frame_opts.dither = 0
    options.frame_opts.snip_edges = True
    options.frame_opts.window_type = 'povey'
    options.frame_opts.remove_dc_offset = True
    options.frame_opts.preemph_coeff = .97
    options.mel_opts.num_bins = 80
    fbank = knf.OnlineFbank(options)
    fbank.accept_waveform(16000, audio)
    fbank.input_finished()
    if fbank.num_frames_ready < 10:
        raise ValueError('Bản ghi quá ngắn để tạo đặc trưng giọng.')
    features = np.stack([fbank.get_frame(index) for index in range(fbank.num_frames_ready)]).astype(np.float32)
    features -= features.mean(axis=0, keepdims=True)
    stat = (MODEL / 'speaker_encoder.onnx').stat()
    session = _speaker_session(threads, stat.st_size, stat.st_mtime_ns)
    value = session.run([session.get_outputs()[0].name], {session.get_inputs()[0].name: features[None]})[0]
    embedding = np.asarray(value[0], dtype=np.float32)
    if embedding.shape != (192,) or not np.isfinite(embedding).all() or float(np.linalg.norm(embedding)) < 1e-6:
        raise ValueError('Speaker encoder chưa trả embedding hợp lệ; chưa tạo giọng.')
    return embedding


def enroll_voice(name, reference_path, folder, params=None, progress=None):
    """Produce private job artifacts. register_voice publishes after job verification."""
    import numpy as np
    from core import binary, NO_WINDOW
    from vieneu_voice import runtime_configuration, MODEL_ROOT, _cli
    name = _validate_name(name)
    if not enrollment_status()['available']:
        raise ValueError(enrollment_status()['message'])
    source = Path(reference_path).resolve()
    if not source.is_file() or source.stat().st_size > 100 * 1024 * 1024:
        raise ValueError('Chọn bản ghi giọng có thật, dung lượng tối đa 100 MB.')
    folder = Path(folder).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    options = params or {}
    start = float(options.get('reference_start', 0))
    duration = float(options.get('reference_duration', 8))
    if not math.isfinite(start) or start < 0 or not math.isfinite(duration) or not 3 <= duration <= 8:
        raise ValueError('Chọn một đoạn giọng sạch dài 3–8 giây, mốc bắt đầu không âm.')
    config = runtime_configuration(params)
    notify = progress or (lambda event: None)
    def run(command):
        result = subprocess.run([str(value) for value in command], capture_output=True, text=True,
            encoding='utf-8', errors='replace', timeout=180, creationflags=NO_WINDOW)
        if result.returncode:
            raise ValueError('Không tạo được giọng riêng: ' + (result.stderr or result.stdout)[-1500:])
    with tempfile.TemporaryDirectory(prefix='npk-enroll-', dir=folder) as temporary:
        stage = Path(temporary)
        reference, reference_16k = stage / 'reference.wav', stage / 'speaker16k.wav'
        notify(dict(progress=.05, message='Chuẩn hóa đoạn giọng tham chiếu trên máy'))
        run([binary('ffmpeg'), '-v', 'error', '-xerror', '-y', '-ss', str(start), '-i', source, '-t', str(duration),
             '-threads', str(config['threads']), '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', reference])
        with wave.open(str(reference), 'rb') as audio:
            actual_duration = audio.getnframes() / audio.getframerate()
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768
        if not 2.9 <= actual_duration <= 8.1 or samples.size == 0 or float(np.sqrt(np.mean(samples ** 2))) < .001:
            raise ValueError('Đoạn tham chiếu cần ít nhất 3 giây và có tiếng nói rõ; bản ghi quá ngắn hoặc im lặng.')
        run([binary('ffmpeg'), '-v', 'error', '-y', '-i', reference, '-threads', str(config['threads']),
             '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', reference_16k])
        notify(dict(progress=.2, message='Trích đặc trưng người nói bằng CAM++ offline'))
        embedding = extract_embedding(reference_16k, config['threads'])
        np.savetxt(stage / 'speaker.emb.txt', embedding[None], delimiter=',', fmt='%.9g')
        notify(dict(progress=.5, message='Mã hóa chất giọng tham chiếu bằng codec của NPK'))
        # CPU enrollment avoids adding a second CUDA context; it runs once per voice.
        cli = _cli('cpu')
        run([cli, '--task', 'tts', '--family', 'vieneu_v3_turbo', '--model', MODEL_ROOT / 'model.gguf',
             '--backend', 'cpu', '--threads', str(config['threads']), '--voice-ref', reference,
             '--request-option', 'encode_reference_only=true', '--out-dir', stage / 'encoded'])
        encoded = stage / 'encoded/vieneu_v3_turbo_reference_codes.txt'
        if not encoded.is_file():
            raise ValueError('Codec chưa trả reference codes; chưa tạo giọng riêng.')
        shutil.copyfile(encoded, stage / 'ref_codes.txt')
        _validate_voice_assets(stage)
        identifier = 'vieneu:custom:' + uuid.uuid4().hex
        metadata = dict(schema=1, id=identifier, name=name, created=time.time(), sample_rate=48000,
            reference_duration=actual_duration, reference_sha256=_sha256(reference),
            model_revision='61b85e3d937fbbacb387714180e8182823512523', encoder_sha256=ENCODER_SHA256,
            assets_sha256={filename: _sha256(stage / filename) for filename in ('reference.wav', 'ref_codes.txt', 'speaker.emb.txt')})
        (stage / 'voice.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        for filename in ('reference.wav', 'ref_codes.txt', 'speaker.emb.txt', 'voice.json'):
            shutil.copyfile(stage / filename, folder / filename)
        notify(dict(progress=1, message='Đã tạo dữ liệu giọng riêng, chờ xác minh và lưu thư viện'))
        return metadata | {'type': 'enroll_voice', 'voice_id': identifier,
            'files': ['reference.wav', 'ref_codes.txt', 'speaker.emb.txt', 'voice.json']}


def verify_enrollment(folder):
    """Validate names, real nonzero embedding, codec tokens, reference WAV and hashes."""
    folder = Path(folder).resolve()
    metadata_path = folder / 'voice.json'
    if metadata_path.resolve().parent != folder or metadata_path.stat().st_size > 32768:
        raise ValueError('Metadata giọng riêng không hợp lệ.')
    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
    identifier = metadata.get('id', '')
    suffix = identifier.removeprefix('vieneu:custom:')
    if metadata.get('schema') != 1 or not identifier.startswith('vieneu:custom:') or \
            len(suffix) != 32 or any(char not in '0123456789abcdef' for char in suffix):
        raise ValueError('Mã giọng riêng không hợp lệ.')
    _validate_name(metadata.get('name'))
    _validate_voice_assets(folder)
    hashes = metadata.get('assets_sha256', {})
    if not isinstance(hashes, dict) or set(hashes) != {'reference.wav', 'ref_codes.txt', 'speaker.emb.txt'}:
        raise ValueError('Thiếu xác minh dữ liệu giọng riêng.')
    for filename, expected in hashes.items():
        if (folder / filename).resolve().parent != folder or _sha256(folder / filename) != expected:
            raise ValueError('Dữ liệu giọng riêng thay đổi trước khi lưu thư viện.')
    with wave.open(str(folder / 'reference.wav'), 'rb') as reference:
        if (reference.getframerate(), reference.getnchannels(), reference.getsampwidth()) != (48000, 1, 2):
            raise ValueError('Bản ghi giọng riêng chưa có PCM16 mono 48 kHz hợp lệ.')
        duration = reference.getnframes() / 48000
        if not 2.9 <= duration <= 8.1:
            raise ValueError('Bản ghi giọng riêng cần 3–8 giây.')
        remaining = reference.getnframes()
        while remaining:
            block = reference.readframes(min(remaining, 48000))
            if not block:
                raise ValueError('Bản ghi giọng riêng thiếu dữ liệu.')
            remaining -= len(block) // 2
    if abs(float(metadata.get('reference_duration', 0)) - duration) > .01:
        raise ValueError('Thời lượng tham chiếu giọng riêng không khớp metadata.')
    return metadata


def register_voice(folder):
    """Publish a verified successful job to the private voice library atomically."""
    folder = Path(folder).resolve()
    metadata = verify_enrollment(folder)
    identifier = metadata['id']
    suffix = identifier.removeprefix('vieneu:custom:')
    library = _library()
    library.mkdir(parents=True, exist_ok=True)
    target = library / suffix
    if target.exists():
        if (target / 'voice.json').read_bytes() == (folder / 'voice.json').read_bytes():
            return identifier
        raise ValueError('Mã giọng riêng đã tồn tại với dữ liệu khác.')
    if len(custom_voices()) >= 128:
        raise ValueError('Thư viện đã có 128 giọng riêng. Xóa một giọng không dùng trước khi thêm.')
    with tempfile.TemporaryDirectory(prefix='.enroll-', dir=library) as temporary:
        temporary_root = Path(temporary).resolve()
        stage = temporary_root / suffix
        stage.mkdir()
        for filename in ('reference.wav', 'ref_codes.txt', 'speaker.emb.txt', 'voice.json'):
            shutil.copyfile(folder / filename, stage / filename)
        stage.replace(target)
    return identifier
