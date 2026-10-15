"""Local audio-file inspection; never uploads files or changes their contents."""
import json
import math
import subprocess
from pathlib import Path
from core import binary, run

EXTENSIONS = {'.wav', '.mp3', '.flac', '.m4a', '.ogg', '.aac'}


def source_audio_clock(media, *, refresh=False):
    """Known first-audio span on the video's clock; legacy metadata stays usable."""
    if refresh and media.get('audio_duration') is None and Path(media.get('path', '')).suffix.lower() in {
            '.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v'}:
        from core import probe
        measured = probe(media['path'])
        if not measured.get('audio'):
            raise ValueError('Nguồn hiện tại không còn luồng âm thanh; hãy nhập lại video.')
        media = media | {key: measured.get(key) for key in ('audio_duration', 'audio_start_time')}
    duration = float(media['duration'])
    start = float(media.get('audio_start_time') or 0)
    length = media.get('audio_duration')
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(start):
        raise ValueError('Thời lượng hoặc mốc âm thanh nguồn không hợp lệ.')
    if length is None:
        return {'start': max(0., start), 'end': duration, 'known': False}
    length = float(length)
    if not math.isfinite(length) or length <= 0:
        raise ValueError('Thời lượng luồng âm thanh nguồn không hợp lệ.')
    if start + length > duration + .35:
        raise ValueError('Âm thanh dài hơn metadata nguồn; hãy nhập lại video trước khi xử lý.')
    return {'start': max(0., start), 'end': min(duration, max(0., start + length)), 'known': True}


def source_audio_span(media, *, refresh=False):
    clock = source_audio_clock(media, refresh=refresh)
    return clock['start'], clock['end']


def local_audio_path(value):
    raw = str(value).strip()
    # Do not let probing trigger network-share authentication or accept web URLs.
    if not raw or raw.startswith(('\\\\', '//')) or '://' in raw:
        raise ValueError('Chọn tệp audio trên ổ đĩa cục bộ, không dùng URL hay thư mục mạng.')
    path = Path(raw)
    if not path.is_absolute():
        raise ValueError('Cần đường dẫn đầy đủ đến tệp audio.')
    path = path.resolve(strict=True)
    if str(path).startswith(('\\\\', '//')) or not path.is_file() or path.suffix.lower() not in EXTENSIONS:
        raise ValueError('Chọn tệp WAV, MP3, FLAC, M4A, OGG hoặc AAC hợp lệ trên máy.')
    return path


def inspect_audio(value, video_duration=0):
    path = local_audio_path(value)
    try:
        result = run([binary('ffprobe'), '-v', 'error', '-show_entries',
                      'format=duration:stream=codec_type,codec_name,sample_rate,channels,duration',
                      '-of', 'json', path], timeout=20)
        info = json.loads(result.stdout)
        stream = next((s for s in info.get('streams', []) if s.get('codec_type') == 'audio'), None)
        if not stream:
            raise ValueError('Không có luồng âm thanh trong tệp.')
        duration = float(info.get('format', {}).get('duration') or stream.get('duration') or 0)
        sample_rate, channels = int(stream.get('sample_rate', 0)), int(stream.get('channels', 0))
        if not math.isfinite(duration) or duration <= 0 or sample_rate <= 0 or channels <= 0:
            raise ValueError('Không đo được thời lượng / định dạng âm thanh.')
    except subprocess.TimeoutExpired as exc:
        raise ValueError('Đọc audio quá thời gian chờ. Kiểm tra tệp đã tải xong rồi thử lại.') from exc
    except (ValueError, RuntimeError, OSError, StopIteration) as exc:
        raise ValueError('Không đọc được audio hợp lệ. Tệp có thể chưa tải xong hoặc bị hỏng.') from exc
    delta = duration - float(video_duration)
    warnings = []
    if video_duration and abs(delta) > .25:
        warnings.append(f'Audio {"dài" if delta > 0 else "ngắn"} hơn video {abs(delta):.2f} giây. '
                        + ('Phần vượt quá video sẽ không xuất.' if delta > 0 else 'Phần còn lại không có lồng tiếng.'))
    return {'path': str(path), 'duration': duration, 'sample_rate': sample_rate,
            'channels': channels, 'codec': stream.get('codec_name'), 'bytes': path.stat().st_size,
            'delta': delta, 'warnings': warnings}
