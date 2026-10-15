"""Select only an encoder that completes an actual small encode probe."""
from functools import lru_cache
import subprocess
from core import binary, NO_WINDOW
from performance import profile_for


@lru_cache(maxsize=1)
def nvenc_ready():
    try:
        result = subprocess.run([binary('ffmpeg'), '-v', 'error', '-f', 'lavfi', '-i',
            'color=c=black:s=128x128:r=10:d=0.2', '-an', '-c:v', 'h264_nvenc', '-f', 'null', '-'],
            capture_output=True, timeout=15, creationflags=NO_WINDOW)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def video_encoding(params):
    mode = params.get('video_encoder', 'cpu')
    quality = str(int(params.get('video_quality', 20)))
    use_gpu = mode in ('auto', 'nvenc') and nvenc_ready()
    if mode == 'nvenc' and not use_gpu:
        raise ValueError('NVIDIA NVENC chưa dùng được. Chọn CPU hoặc Tự chọn.')
    if use_gpu:
        return ['-c:v', 'h264_nvenc', '-preset', 'p4', '-rc', 'vbr', '-cq', quality, '-b:v', '0'], 'NVIDIA NVENC'
    return ['-c:v', 'libx264', '-threads', str(profile_for(params)['threads']), '-preset', 'fast', '-crf', quality], 'H.264 CPU'
