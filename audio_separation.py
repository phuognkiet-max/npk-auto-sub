"""Optional offline UVR MDX instrumental separation, bounded by one 5.9 s window.

UVR's public Inst_HQ_4 model is used with credit to UVR and its developers.
The NumPy STFT layout follows UVR's documented MDX inference (periodic Hann,
centered reflect padding, LR real/imag channels, 1,024 hop, low three bins off).
CPUExecutionProvider is explicit: this module never installs/replaces a runtime,
falls back from a requested GPU, or downloads a model while processing a job.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
from pathlib import Path
import struct
import subprocess
import tempfile
import time

from core import MODELS, NO_WINDOW, binary
from performance import FramePacer, ffmpeg_limits, profile_for
from subtitle_quality import source_signature
from translation_checkpoint import write_document

MODEL_NAME = 'UVR-MDX-NET-Inst_HQ_4.onnx'
MODEL_URL = ('https://github.com/TRvlvr/model_repo/releases/download/'
             'all_public_uvr_models/' + MODEL_NAME)
MODEL_BYTES = 59074342
MODEL_SHA256 = '3c4b5b9b05090fdf238f38ba5046813982d50e2a652e9cb3324ea79720c3c9c8'
MODEL_UVR_HASH = '0f2a6bc5b49d87d64728ee40e23bceb1'
MODEL_DIR = MODELS / 'audio-separation'
UVR_COMMIT = 'a5f88453bfb2b38b05a965bcf67727243e0cbf19'
METADATA_COMMIT = '3826b05b570dbd4fbedbc807758803b35348ba1b'
MODEL_METADATA_URL = ('https://raw.githubusercontent.com/TRvlvr/application_data/' +
                      METADATA_COMMIT + '/mdx_model_data/model_data_new.json')
LICENSE_URL = ('https://github.com/Anjok07/ultimatevocalremovergui/blob/' +
               UVR_COMMIT + '/README.md#license')
SAMPLE_RATE, HOP, N_FFT, DIM_F, DIM_T = 44100, 1024, 5120, 2560, 256
COMPENSATE = 1.019
CHUNK_SIZE = HOP * (DIM_T - 1)
TRIM = N_FFT // 2
STRIDE = CHUNK_SIZE - N_FFT
MAX_DURATION = 3 * 3600  # float32 RIFF WAV stays below its 4 GiB data limit.
ATTRIBUTION = (
    'Ultimate Vocal Remover (UVR) and its developers: public MDX Inst_HQ_4 model.\n'
    'UVR explicitly permits third-party use of its models under MIT with credit.\n'
    'Source and license statement: ' + LICENSE_URL + '\n'
    'MDX-Net architecture credit: Kuielab / Woosung Choi.\n'
    'Inference reference: https://github.com/Anjok07/ultimatevocalremovergui/blob/' +
    UVR_COMMIT + '/separate.py\n'
    'Model source: ' + MODEL_URL + '\n'
    'Pinned model metadata: ' + MODEL_METADATA_URL + '\n'
)


def model_path():
    return MODEL_DIR / MODEL_NAME


def separation_status():
    """Cheap status. Cryptographic verification happens again when a job starts."""
    path = model_path()
    installed = path.is_file() and path.stat().st_size == MODEL_BYTES
    return {'installed': installed, 'model': MODEL_NAME, 'bytes': MODEL_BYTES,
            'device': 'cpu', 'provider': 'CPUExecutionProvider',
            'sample_rate': SAMPLE_RATE, 'license_url': LICENSE_URL,
            'setup': 'tools/setup_audio_separation.py',
            'note': 'Tách giọng/nhạc offline trên CPU; cần nghe lại vì có thể còn giọng hoặc mất nhạc.'}


def verify_model(path=None):
    path = Path(path) if path is not None else model_path()
    if not path.is_file():
        raise ValueError('Chưa cài model tách giọng/nhạc. Chạy tools/setup_audio_separation.py trước.')
    if path.stat().st_size != MODEL_BYTES:
        raise ValueError('Model tách giọng/nhạc sai kích thước; hãy cài lại bằng trình cài riêng.')
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != MODEL_SHA256:
        raise ValueError('SHA256 model tách giọng/nhạc không khớp bản đã kiểm tra.')
    return path


def separation_signature(project):
    value = ['npk-uvr-mdx-v2', source_signature(project), MODEL_SHA256,
             SAMPLE_RATE, N_FFT, HOP, DIM_F, DIM_T, COMPENSATE, 'hann-context-overlap']
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class NumpySTFT:
    """The model's transform, without Torch/librosa/scipy dependencies."""
    def __init__(self, n_fft=N_FFT, hop=HOP, dim_f=DIM_F):
        import numpy as np
        self.n_fft, self.hop, self.dim_f = n_fft, hop, dim_f
        self.window = (.5 - .5 * np.cos(2 * np.pi * np.arange(n_fft) / n_fft)).astype('float32')

    def forward(self, stereo):
        import numpy as np
        padded = np.pad(stereo, ((0, 0), (self.n_fft // 2, self.n_fft // 2)), mode='reflect')
        frames = np.lib.stride_tricks.sliding_window_view(padded, self.n_fft, axis=1)[:, ::self.hop, :]
        spectrum = np.fft.rfft(frames * self.window, axis=-1).transpose(0, 2, 1)
        packed = np.stack((spectrum.real, spectrum.imag), axis=1).reshape(1, 4, -1, spectrum.shape[-1])
        return np.ascontiguousarray(packed[:, :, :self.dim_f], dtype='float32')

    def inverse(self, packed):
        import numpy as np
        bins, frames = self.n_fft // 2 + 1, packed.shape[-1]
        values = np.zeros((2, 2, bins, frames), dtype='float32')
        values[:, :, :packed.shape[-2]] = packed.reshape(2, 2, packed.shape[-2], frames)
        spectrum = values[:, 0] + 1j * values[:, 1]
        waveframes = np.fft.irfft(spectrum.transpose(0, 2, 1), n=self.n_fft, axis=-1) * self.window
        size = self.hop * (frames - 1) + self.n_fft
        output = np.zeros((2, size), dtype='float32')
        divider = np.zeros(size, dtype='float32')
        squared = self.window * self.window
        for index in range(frames):
            start = index * self.hop
            output[:, start:start+self.n_fft] += waveframes[:, index]
            divider[start:start+self.n_fft] += squared
        left, right = self.n_fft // 2, size - self.n_fft // 2
        return output[:, left:right] / np.maximum(divider[left:right], 1e-12)


class FloatWaveWriter:
    """IEEE float32 stereo WAV keeps residual+instrumental gain without clipping."""
    def __init__(self, path):
        self.output = open(path, 'wb')
        self.frames, self.peak = 0, 0.
        self.output.write(struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 0, b'WAVE', b'fmt ', 16,
                          3, 2, SAMPLE_RATE, SAMPLE_RATE * 8, 8, 32, b'data', 0))

    def write(self, stereo):
        import numpy as np
        if not np.isfinite(stereo).all():
            raise ValueError('Model tách giọng/nhạc tạo mẫu không hữu hạn.')
        peak = float(np.max(np.abs(stereo))) if stereo.size else 0.
        if peak > 8:
            raise ValueError('Biên độ stem bất thường; không xuất âm thanh lỗi.')
        self.peak = max(self.peak, peak)
        self.output.write(np.ascontiguousarray(stereo.T, dtype='<f4').tobytes())
        self.frames += stereo.shape[-1]

    def close(self):
        if self.output.closed:
            return
        size = self.frames * 8
        if size >= 2**32 - 36:
            self.output.close()
            raise ValueError('Stem vượt giới hạn RIFF WAV 4 GiB.')
        self.output.seek(4)
        self.output.write(struct.pack('<I', 36 + size))
        self.output.seek(40)
        self.output.write(struct.pack('<I', size))
        self.output.flush()
        os.fsync(self.output.fileno())
        self.output.close()


def _read_frames(stream, count):
    import numpy as np
    blocks, remaining = [], count * 8
    while remaining:
        block = stream.read(remaining)
        if not block:
            break
        blocks.append(block)
        remaining -= len(block)
    value = b''.join(blocks)
    if len(value) % 8:
        raise ValueError('PCM giải mã bị cắt giữa mẫu âm thanh.')
    return np.frombuffer(value, dtype='<f4').reshape(-1, 2).T.copy()


def _load_session(params):
    import onnxruntime as ort
    options = ort.SessionOptions()
    options.intra_op_num_threads = profile_for(params)['threads']
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    # Default ORT arena retained ~2.1 GiB for this small model. Releasing
    # intermediates keeps the verified 1-thread peak near 1.1 GiB at a modest
    # CPU cost; neither setting changes model arithmetic or stem quality.
    options.enable_cpu_mem_arena = False
    options.enable_mem_pattern = False
    session = ort.InferenceSession(str(verify_model()), sess_options=options,
                                   providers=['CPUExecutionProvider'])
    inputs, outputs = session.get_inputs(), session.get_outputs()
    if (len(inputs) != 1 or inputs[0].name != 'input' or inputs[0].type != 'tensor(float)' or
            inputs[0].shape[1:] != [4, DIM_F, DIM_T] or len(outputs) != 1):
        raise ValueError('Shape model MDX khác bản đã kiểm tra.')
    return session


def separate_audio(project, job, folder, emit=None):
    """Stream stereo source through overlapping fixed windows; publish only on success."""
    import numpy as np
    emit = emit or (lambda **kwargs: None)
    media, params = project['media'], project['params']
    if not media.get('audio'):
        raise ValueError('Video không có âm thanh để tách giọng/nhạc.')
    duration = float(media['duration'])
    if not math.isfinite(duration) or duration <= 0 or duration > MAX_DURATION:
        raise ValueError('Tách giọng/nhạc hỗ trợ nguồn dài hơn 0 và tối đa 3 giờ mỗi lượt.')
    from audio_inputs import source_audio_clock
    source_clock = source_audio_clock(media, refresh=True)
    if source_clock['start'] >= source_clock['end']:
        raise ValueError('Luồng âm thanh nằm ngoài khoảng video; hãy nhập lại nguồn.')
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    fingerprint = separation_signature(project)
    target = folder/'audio-separation.json'
    session = _load_session(params)  # no download and no GPU allocation.
    transform, pacer = NumpySTFT(), FramePacer(params)
    window = np.hanning(CHUNK_SIZE).astype('float32')
    carry = np.zeros((2, N_FFT), dtype='float32')
    divisor_carry = np.zeros(N_FFT, dtype='float32')
    started, decoded, written, chunks = time.perf_counter(), 0, 0, 0
    temporary, writers = [], []
    process = None
    try:
        for stem in ('instrumentals', 'vocals'):
            descriptor, name = tempfile.mkstemp(prefix=stem+'.', suffix='.part.wav', dir=folder)
            os.close(descriptor)
            temporary.append(Path(name))
            writers.append(FloatWaveWriter(name))
        with tempfile.TemporaryFile() as errors:
            alignment = 'aresample=async=1:first_pts=0'
            if source_clock['known']:
                alignment += f',apad=whole_dur={duration}'
            command = [binary('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-xerror', *ffmpeg_limits(params),
                       '-i', str(media['path']), '-vn', '-map', '0:a:0', '-ac', '2', '-ar', str(SAMPLE_RATE),
                       '-af', alignment, *(['-t', str(duration)] if source_clock['known'] else []),
                       '-f', 'f32le', 'pipe:1']
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors, creationflags=NO_WINDOW)
            incoming = _read_frames(process.stdout, CHUNK_SIZE-TRIM)
            decoded += incoming.shape[-1]
            if decoded > (duration+.35)*SAMPLE_RATE:
                raise ValueError('Âm thanh dài hơn metadata nguồn; hãy nhập lại video trước khi tách.')
            current = np.concatenate((np.zeros((2, TRIM), dtype='float32'), incoming), axis=1)
            position = -TRIM
            while position < decoded:
                active = time.perf_counter()
                actual = current.shape[-1]
                padded = np.pad(current, ((0, 0), (0, CHUNK_SIZE-actual)))
                spectrum = transform.forward(padded)
                spectrum[:, :, :3] = 0  # UVR MDX preprocessing.
                prediction = session.run(None, {'input': spectrum})[0]
                if prediction.shape != spectrum.shape or not np.isfinite(prediction).all():
                    raise ValueError('Kết quả ONNX không đúng shape hoặc không hữu hạn.')
                separated = transform.inverse(prediction) * COMPENSATE
                summed = separated * window
                divider = window.copy()
                summed[:, :N_FFT] += carry
                divider[:N_FFT] += divisor_carry
                count = min(STRIDE, max(0, decoded-position))
                left = max(0, -position)
                if count > left:
                    instrumental = summed[:, left:count] / np.maximum(divider[left:count], 1e-12)
                    original = padded[:, left:count]
                    writers[0].write(instrumental)
                    writers[1].write(original-instrumental)
                    written += count-left
                carry = summed[:, STRIDE:].copy()
                divisor_carry = divider[STRIDE:].copy()
                chunks += 1
                emit(progress=min(.98, written/(duration*SAMPLE_RATE)),
                     message=f'Tách giọng/nhạc offline CPU: {written/SAMPLE_RATE:.1f}/{duration:.1f}s')
                pacer.rest(active)
                if actual < CHUNK_SIZE and position+STRIDE >= decoded:
                    break
                position += STRIDE
                incoming = _read_frames(process.stdout, STRIDE)
                decoded += incoming.shape[-1]
                if decoded > (duration+.35)*SAMPLE_RATE:
                    raise ValueError('Âm thanh dài hơn metadata nguồn; hãy nhập lại video trước khi tách.')
                current = np.concatenate((padded[:, STRIDE:], incoming), axis=1)
            process.stdout.close()
            code = process.wait()
            errors.seek(0)
            failure = errors.read(3000).decode(errors='replace').strip()
            if code:
                raise ValueError('FFmpeg không giải mã được toàn bộ âm thanh: ' + failure)
        if not decoded or written != decoded or abs(decoded/SAMPLE_RATE-duration) > .35:
            raise ValueError('Độ dài stem khác nguồn; không công bố kết quả bị cắt.')
        if separation_signature(project) != fingerprint:
            raise ValueError('Nguồn đã thay đổi trong lúc tách giọng/nhạc.')
        for writer in writers:
            writer.close()
        peaks = {stem:round(writer.peak, 6) for stem, writer in zip(('instrumentals', 'vocals'), writers)}
        warnings = ['Model có thể còn giọng gốc, mất một phần nhạc hoặc tạo âm giả; hãy nghe lại trước khi phối.']
        if source_clock['start'] > .1 or source_clock['end'] < duration - .3:
            warnings.append('Âm nguồn chỉ có trong khoảng %.2f–%.2f giây; stem giữ im lặng ngoài luồng '
                'để phối đúng mốc video, không tự tạo thêm âm thanh.' % (source_clock['start'], source_clock['end']))
        if max(peaks.values()) > 1:
            warnings.append('Stem float WAV có peak trên 0 dBFS; giảm gain khi phối để tránh clipping.')
        report = {'schema_version':1, 'signature':fingerprint, 'source_signature':source_signature(project),
                  'model':MODEL_NAME, 'model_sha256':MODEL_SHA256, 'device':'cpu',
                  'provider':'CPUExecutionProvider', 'sample_rate':SAMPLE_RATE,
                  'duration':decoded/SAMPLE_RATE, 'frames':decoded, 'chunks':chunks,
                  'timeline_duration':duration, 'source_audio_start':source_clock['start'],
                  'source_audio_end':source_clock['end'], 'source_audio_span_known':source_clock['known'],
                  'window_seconds':CHUNK_SIZE/SAMPLE_RATE, 'overlap_samples':N_FFT,
                  'wall_seconds':round(time.perf_counter()-started, 3), 'peaks':peaks,
                  'warnings':warnings, 'attribution':ATTRIBUTION}
        for temporary_path, name in zip(temporary, ('instrumentals.wav', 'vocals.wav')):
            os.replace(temporary_path, folder/name)
        write_document(target, report)
        emit(progress=1, message='Đã tách giọng/nhạc offline; nghe lại stem trước khi phối.')
        return {'type':'separate_audio', 'files':['instrumentals.wav','vocals.wav',target.name],
                'signature':fingerprint, 'source_signature':source_signature(project),
                'warnings':warnings, 'separation':report}
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        if process is not None and process.stdout is not None:
            process.stdout.close()
        for writer in writers:
            writer.close()
        for path in temporary:
            path.unlink(missing_ok=True)
