"""Bounded, transient one-frame restoration. No jobs, artifacts, or media writes."""
from __future__ import annotations
import copy
import json
import subprocess
import sys
import threading
from contextlib import nullcontext
from fastapi import HTTPException
from core import ROOT, MODELS, NO_WINDOW

KEYS = {'method','blur_radius','cover_opacity','edge_feather','cover_color','dilate',
        'stretch_height','stretch_direction','stretch_offset','inpaint_radius','context',
        'max_side','mask_mode','mask_source'}
gate = threading.Lock()
process_lock = threading.Lock()
live_process = None


def kill_preview(proc):
    if proc.poll() is not None:
        return
    import psutil
    try:
        children = psutil.Process(proc.pid).children(recursive=True)
        for child in reversed(children):
            try: child.kill()
            except psutil.NoSuchProcess: pass
    except psutil.NoSuchProcess:
        pass
    proc.kill()


def cancel_active(request_id=None):
    """Production jobs and app shutdown take priority over this owned preview."""
    with process_lock:
        if live_process is not None and (request_id is None or getattr(live_process, 'preview_request_id', None) == request_id):
            live_process.preview_cancelled = True
            kill_preview(live_process)



def snapshot(project, body, validate_params, validate_regions, number):
    if set(body) - {'source_revision','params','regions','time','request_id'}:
        raise ValueError('Chỉ nhận thông số xem thử, không nhận đường dẫn hoặc tệp khác.')
    if body.get('source_revision') != project.get('source_revision'):
        raise HTTPException(409, 'Video nguồn đã đổi. Chờ khung hình mới rồi thử lại.')
    params = body.get('params', {})
    if not isinstance(params, dict) or set(params) - KEYS:
        raise ValueError('Thông số xem thử không hợp lệ.')
    p = copy.deepcopy(project)
    p['params'].update(validate_params(params))
    if p['params']['method'] not in {'telea','lama'}:
        raise ValueError('Chế độ nhẹ được xem trực tiếp trên khung video.')
    if p['params'].get('mask_source','manual') != 'manual':
        raise ValueError('Vùng bám chuyển động cần dùng Xem ảnh tại vị trí này để đối chiếu đúng mặt nạ.')
    regions = body.get('regions', [])
    if not isinstance(regions, list) or not 1 <= len(regions) <= 100:
        raise ValueError('Xem trực tiếp hỗ trợ từ 1 đến 100 vùng.')
    if any(not isinstance(r,dict) or not {'x','y','w','h'} <= r.keys() for r in regions):
        raise ValueError('Vùng xem thử không hợp lệ.')
    p['regions'] = validate_regions(regions, p['media'])
    from clean_timing import frame_time
    moment = frame_time(p['media'], number(body.get('time',0), 0, p['media']['duration']))
    if not any(r['start'] <= moment < r['end'] for r in p['regions']):
        raise ValueError('Không có vùng hiệu lực ở thời điểm này.')
    if p['media']['width'] * p['media']['height'] > 3840 * 2160:
        raise ValueError('Xem phục hồi trực tiếp hỗ trợ video đến 4K. Dùng Xem ảnh tại vị trí này cho video lớn hơn.')
    # Only media and cleaning data enter the isolated child. No keys or subtitles.
    return {'media': p['media'], 'params': {k: p['params'][k] for k in KEYS},
            'regions': p['regions'], 'time': moment}


def render_frame(spec, admission_lock=None, busy=lambda: False, request_id=None):
    """Return JPEG bytes. The app process never loads or retains an AI model."""
    global live_process
    if not gate.acquire(blocking=False):
        raise HTTPException(429, 'Một khung phục hồi khác đang được tính. Dừng chỉnh một lát rồi thử lại.')
    try:
        command = [str(ROOT/'runtime/venv/Scripts/python.exe'), '-X', 'utf8', str(ROOT/'live_clean_preview.py'), '--frame']
        # Share only startup admission with the production queue, never its long work.
        with admission_lock or nullcontext():
            if busy():
                raise HTTPException(409,'Đang có tác vụ xử lý video. Chờ tác vụ kết thúc rồi thử lại.')
            with process_lock:
                live_process = proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                                       creationflags=NO_WINDOW)
                proc.preview_request_id = request_id
        with proc:
            try:
                out, error = proc.communicate(json.dumps(spec,ensure_ascii=False).encode('utf-8'), timeout=45)
            except subprocess.TimeoutExpired:
                kill_preview(proc); proc.communicate()
                raise HTTPException(504, 'Khung phục hồi quá chậm. Giảm vùng hoặc dùng Xem ảnh tại vị trí này; không chạy cả video.')
        if getattr(proc,'preview_cancelled',False):
            raise HTTPException(409,'Đã nhường bộ xử lý cho tác vụ video. Chỉnh lại hoặc bật xem trực tiếp sau khi tác vụ kết thúc.')
        if proc.returncode or not out.startswith(b'\xff\xd8') or not out.endswith(b'\xff\xd9'):
            # Do not expose a stack trace or local path in the viewer.
            raise HTTPException(422, 'Chưa phục hồi được khung này. Kiểm tra model trong Cài đặt hoặc dùng Xem ảnh tại vị trí này.')
        return out
    finally:
        with process_lock:
            live_process = None
        gate.release()


def render_in_child(spec):
    from performance import configure_compute
    configure_compute({'workload_mode':'quiet'})
    from worker import read_frame
    from cleaner import Cleaner
    import cv2
    frame = read_frame(spec['media']['path'], spec['time'])
    # Keep source pixels/coordinates and the same restoration algorithm as export.
    # Temporal smoothing is inherently unavailable in a single-frame preview.
    cleaner = Cleaner(MODELS/'lama/big-lama.pt', spec['params'] | {'temporal':False,'workload_mode':'quiet'})
    result, _ = cleaner(frame, spec['regions'], spec['time'])
    scale = min(1., 1280 / max(result.shape[:2]))
    if scale < 1:
        result = cv2.resize(result, (round(result.shape[1]*scale),round(result.shape[0]*scale)), interpolation=cv2.INTER_AREA)
    ok, jpeg = cv2.imencode('.jpg', result, [cv2.IMWRITE_JPEG_QUALITY,92])
    if not ok: raise RuntimeError('Không mã hóa được khung xem thử.')
    return jpeg.tobytes()


if __name__ == '__main__' and sys.argv[1:] == ['--frame']:
    from contextlib import redirect_stdout
    spec = json.loads(sys.stdin.buffer.read())
    with redirect_stdout(sys.stderr):
        result = render_in_child(spec)
    sys.stdout.buffer.write(result)
