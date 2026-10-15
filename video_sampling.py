"""Sequential, bounded-memory samples on an explicit video sample clock.

An OUTPUT -t is quantized to the low-rate output timebase and can discard a
legitimate final sample (e.g. 1.1 s at 2 fps loses the frame at 1.0 s). Limit
the INPUT instead, then request only clock ticks inside the video stream.
Do not clone/pad an early EOF: decoder errors and missing frames still fail.
"""
import math
import subprocess
import unicodedata
from pathlib import Path

from core import NO_WINDOW, binary
from performance import ffmpeg_limits, profile_for


def sample_end(media, requested_end):
    """Audio/container padding is not another frame to recognize."""
    end = min(float(requested_end), float(media['duration']))
    video_end = media.get('video_duration')
    if video_end is not None:
        video_end = float(video_end)
        if not math.isfinite(video_end) or video_end <= 0:
            raise ValueError('Thời lượng luồng hình không hợp lệ. Hãy nhập lại video.')
        end = min(end, video_end)
    if not math.isfinite(end) or end < 0:
        raise ValueError('Mốc kết thúc nhận dạng không hợp lệ.')
    return end


def sample_tail_tolerance(media, interval):
    # avg_frame_rate does not describe the final frame duration for VFR. If
    # stream timing is known, exclude only floating point timestamp noise.
    if media.get('video_duration') is not None:
        return 1e-6
    # Legacy snapshots only had a rounded container duration; retain their
    # half-frame rule instead of inventing a sample beyond the old video tail.
    return min(interval, 1 / max(1, float(media.get('fps', 30)))) / 2


def sample_count(start, end, interval, source_fps, *, tail_tolerance=None):
    if not all(math.isfinite(float(value)) for value in (start, end, interval)) or interval <= 0:
        raise ValueError('Khoảng hoặc tần suất lấy khung hình không hợp lệ.')
    tail = (min(interval, 1 / max(1, float(source_fps))) / 2
            if tail_tolerance is None else max(0, float(tail_tolerance)))
    if start >= end:
        return 0
    return max(1, math.ceil(max(0, end-start-tail) / interval))


def sampled_frames(media, params, start, end, interval, log_path):
    import numpy as np
    width, height = int(media['width']), int(media['height'])
    end = sample_end(media, end)
    count = sample_count(start, end, interval, media.get('fps', 30),
                         tail_tolerance=sample_tail_tolerance(media, interval))
    if not count:
        return
    command = [binary('ffmpeg'), '-hide_banner', '-loglevel', 'error', '-xerror',
        *ffmpeg_limits(params), '-ss', str(start), '-t', str(end-start), '-i', media['path'],
        '-map', '0:v:0', '-an', '-sn',
        '-vf', f'setpts=PTS-STARTPTS,fps=fps={1/interval}:start_time=0:round=up:eof_action=pass',
        '-frames:v', str(count), '-pix_fmt', 'bgr24', '-f', 'rawvideo',
        '-threads', str(profile_for(params)['threads']), 'pipe:1']
    with Path(log_path).open('wb') as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=log, creationflags=NO_WINDOW)
        try:
            size = width*height*3
            for index in range(count):
                pixels = process.stdout.read(size)
                if len(pixels) != size:
                    raise RuntimeError(f'Không đọc được khung hình ở {start+index*interval:.2f} giây. '
                        'Lượt nhận dạng chưa đầy đủ; video và phụ đề cũ được giữ nguyên.')
                yield start+index*interval, np.frombuffer(pixels, dtype=np.uint8).reshape(height,width,3)
            process.stdout.close()
            if process.wait(timeout=30):
                raise RuntimeError('Video có lỗi giải mã. Lượt nhận dạng chưa được xác nhận đầy đủ; xem chẩn đoán để biết vị trí lỗi.')
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()


def deduplicated_boxes(boxes):
    """Deduplicate the same physical text read by intersecting search regions.

    Text at different positions remains independent, including repeated words.
    Small OCR box jitter is allowed only when almost all of the smaller box is
    covered and the boxes have comparable height, area and centre. This is not
    semantic text deduplication. Each item is (left, top, right, bottom, text).
    """
    result = []
    def key(text):
        return ' '.join(unicodedata.normalize('NFKC', str(text)).split()).casefold()
    for box in boxes:
        if len(box) != 5 or not str(box[4]).strip():
            continue
        x, y, right, bottom = (float(value) for value in box[:4])
        if not all(math.isfinite(value) for value in (x, y, right, bottom)) or right <= x or bottom <= y:
            continue
        box = (x, y, right, bottom, str(box[4]).strip())
        area, height = (right-x)*(bottom-y), bottom-y
        duplicate = False
        for previous in result:
            if key(previous[4]) != key(box[4]):
                continue
            px, py, pr, pb = previous[:4]
            parea, pheight = (pr-px)*(pb-py), pb-py
            overlap = max(0, min(right, pr)-max(x, px))*max(0, min(bottom, pb)-max(y, py))
            if (overlap >= .85*min(area, parea) and .5 <= area/parea <= 2 and
                    .7 <= height/pheight <= 1/.7 and
                    abs((y+bottom-py-pb)/2) <= .3*max(height,pheight) and
                    abs((x+right-px-pr)/2) <= .25*min(right-x,pr-px)):
                duplicate = True
                break
        if not duplicate:
            result.append(box)
    return result


def grouped_lines(boxes):
    """Return geometric baseline groups, keeping independent lines separate."""
    lines = []
    for box in sorted(deduplicated_boxes(boxes), key=lambda b: ((b[1]+b[3])/2,b[0])):
        centre, height = (box[1]+box[3])/2, max(1,box[3]-box[1])
        match = next((line for line in lines if abs(line['y']-centre) <= .45*min(line['h'],height)),None)
        if match is None:
            match = {'y':centre,'h':height,'boxes':[]}; lines.append(match)
        match['boxes'].append(box)
    return lines


def reading_order(boxes):
    """Group physical text objects on a baseline before sorting horizontally."""
    lines = grouped_lines(boxes)
    return '\n'.join(' '.join(b[4] for b in sorted(line['boxes'])) for line in lines)
