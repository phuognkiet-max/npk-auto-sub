"""One time contract for still restoration, sample clips and output verification."""
import math


def video_span(media):
    duration = float(media.get('video_duration') or media['duration'])
    fps = float(media['fps'])
    if not math.isfinite(duration) or duration <= 0 or not math.isfinite(fps) or fps <= 0:
        raise ValueError('Thời lượng hoặc tốc độ khung hình của video không hợp lệ.')
    return min(duration, float(media['duration'])), fps


def checked_time(media, moment):
    moment = float(moment)
    if not math.isfinite(moment) or not 0 <= moment <= float(media['duration']):
        raise ValueError('Thời điểm xem thử nằm ngoài video.')
    return moment


def frame_time(media, moment):
    """Select the containing CFR frame, never seek past the last video frame."""
    duration, fps = video_span(media)
    moment = checked_time(media, moment)
    # Container audio padding of a fraction of a frame is not another picture.
    last = max(0, math.floor(duration * fps + 1e-5) - 1)
    return min(math.floor(moment * fps + 1e-7), last) / fps


def clip_window(media, moment, *, refresh=False):
    """Keep the requested position in a <=3s window, shifting back at EOF.

    Old projects only have container duration (often longer than the pictures).
    Workers and verifiers refresh stream timing, without updating the project.
    """
    requested = checked_time(media, moment)
    if refresh:
        from core import probe
        media = media | probe(media['path'])
    duration, fps = video_span(media)
    length = min(3., duration)
    start = math.floor(min(requested, max(0., duration - length)) * fps + 1e-5) / fps
    return start, min(length, duration - start)


def require_active_region(project, start, duration):
    if not any(float(r.get('start', 0)) < start + duration and
               float(r.get('end', project['media']['duration'])) > start for r in project['regions']):
        raise ValueError('Mẫu thử không có vùng xử lý đang hiệu lực. Tua đến vùng đã chọn rồi xem thử.')
