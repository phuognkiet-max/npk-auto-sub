"""Lightweight subtitle cover through FFmpeg, without Python per-frame inference."""
import subprocess
import time
from core import binary, NO_WINDOW, signature
from cleaner import bounds, stretch_source
from encoding import video_encoding
from performance import ffmpeg_limits
from clean_timing import clip_window, frame_time, require_active_region

METHODS = {'blur', 'box', 'stretch'}


def patch_alpha(params):
    opacity = float(params.get('cover_opacity',1))
    feather = int(params.get('edge_feather',0))
    if opacity == 1 and not feather:
        return ''
    alpha = f'255*{opacity:.6f}'
    if feather:
        alpha += f'*min(1,min(min(X,W-1-X),min(Y,H-1-Y))/{feather})'
    return f",format=rgba,geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':a='{alpha}'"


def cover_filter(project, start=0):
    p, media = project['params'], project['media']
    method = p['method']
    if method not in METHODS or p.get('mask_source', 'manual') != 'manual':
        raise ValueError('Che nhẹ dùng vùng thủ công theo thời gian; không dùng mask OCR nét chữ.')
    if len(project['regions']) > 100:
        raise ValueError('Che nhẹ giới hạn 100 vùng mỗi tác vụ; hãy gộp các khoảng liên tiếp.')
    parts, current = [], '0:v'
    for index, region in enumerate(project['regions']):
        x1,y1,x2,y2 = bounds(region, media['width'], media['height'])
        pad = int(p.get('dilate', 4))
        x1,y1,x2,y2 = max(0,x1-pad),max(0,y1-pad),min(media['width'],x2+pad),min(media['height'],y2+pad)
        w,h = x2-x1,y2-y1
        a,b = max(0,region.get('start',0)-start), region.get('end',media['duration'])-start
        if b <= 0:
            continue
        enabled = f"gte(t,{a:.6f})*lt(t,{b:.6f})"
        next_label = f'v{index}'
        if method == 'box' and not p.get('edge_feather',0):
            color = p.get('cover_color', '#000000').replace('#', '0x')
            color += f"@{float(p.get('cover_opacity',1)):.6f}"
            parts.append(f"[{current}]drawbox=x={x1}:y={y1}:w={w}:h={h}:color={color}:t=fill:enable='{enabled}'[{next_label}]")
        else:
            parts.append(f'[{current}]split=2[base{index}][crop{index}]')
            if method == 'box':
                color = p.get('cover_color','#000000').replace('#','0x')
                effect = f'crop={w}:{h}:{x1}:{y1}:exact=1,drawbox=color={color}:t=fill'
            elif method == 'blur':
                radius = max(1,min(int(p.get('blur_radius', 16)), (w-1)//2, (h-1)//2))
                effect = f'format=yuv444p,crop={w}:{h}:{x1}:{y1}:exact=1,boxblur=luma_radius={radius}:luma_power=3:chroma_radius={radius}:chroma_power=3'
            else:
                source_y, strip = stretch_source(y1,y2,media['height'],p.get('stretch_height',12),
                    p.get('stretch_direction','above'),p.get('stretch_offset',0))
                effect = f'crop={w}:{strip}:{x1}:{source_y}:exact=1,scale={w}:{h}:flags=bilinear'
            effect += patch_alpha(p)
            parts.append(f'[crop{index}]{effect}[patch{index}]')
            parts.append(f"[base{index}][patch{index}]overlay={x1}:{y1}:enable='{enabled}':eof_action=pass[{next_label}]")
        current = next_label
    parts.append(f'[{current}]pad=ceil(iw/2)*2:ceil(ih/2)*2[vout]')
    return ';'.join(parts)


def preview_fast(project, moment, folder):
    """One-frame comparison uses exactly the export filter, not a CSS simulation."""
    from core import run
    p = project['params']
    moment = frame_time(project['media'], moment)
    common = [binary('ffmpeg'),'-v','error','-xerror','-y',*ffmpeg_limits(p),'-ss',str(moment),'-i',project['media']['path']]
    run([*common,'-frames:v','1','-q:v','2',str(folder/'before.jpg')],timeout=60)
    run([*common,'-filter_complex',cover_filter(project,moment),'-map','[vout]',
         '-frames:v','1','-q:v','2',str(folder/'after.jpg')],timeout=60)
    # Retain the diagnostic mask image used by existing preview consumers.
    import cv2
    import numpy as np
    from cleaner import make_mask
    frame = cv2.imdecode(np.frombuffer((folder/'before.jpg').read_bytes(),np.uint8),cv2.IMREAD_COLOR)
    mask = make_mask(frame,project['regions'],p | {'mask_mode':'rectangle'},moment)
    tint = frame.copy()
    tint[mask>0] = (tint[mask>0]*.45+(0,120,140)).clip(0,255).astype(np.uint8)
    ok, data = cv2.imencode('.jpg',tint,[cv2.IMWRITE_JPEG_QUALITY,95])
    if not ok: raise RuntimeError('Không lưu được ảnh phạm vi xử lý.')
    (folder/'mask.jpg').write_bytes(data.tobytes())
    return {'files':['before.jpg','after.jpg','mask.jpg'],'type':'preview','time':moment,'signature':signature(project),'method':p['method']}


def clean_fast(project, job, folder, emit):
    began = time.perf_counter()
    media,p = project['media'],project['params']
    start, length = clip_window(media, job.get('time',0), refresh=True) if job['kind']=='clip' else (0,media['duration'])
    if length <= 0 or not project['regions']:
        raise ValueError('Chọn vùng và khoảng video hợp lệ trước.')
    require_active_region(project, start, length)
    encoding,label = video_encoding(p)
    emit(stage='encode',progress=.02,message=f'Che {p["method"]} trực tiếp bằng FFmpeg · {label} · không nạp LaMa')
    graph = cover_filter(project,start)
    command = [binary('ffmpeg'), '-v','error','-xerror','-y',*ffmpeg_limits(p),'-ss',str(start),'-i',media['path'],
        '-t',str(length),'-filter_complex',graph,'-map','[vout]','-map','0:a:0?',*encoding,
        '-pix_fmt','yuv420p','-c:a','aac','-b:a','192k','-movflags','+faststart','-progress','pipe:1',
        '-nostats',str(folder/'clean.mp4')]
    with (folder/'cover.log').open('w',encoding='utf-8') as log:
        proc = subprocess.Popen(command,stdout=subprocess.PIPE,stderr=log,text=True,encoding='utf-8',creationflags=NO_WINDOW)
        try:
            for line in proc.stdout:
                if line.startswith('out_time_us='):
                    try: done = float(line.strip().split('=',1)[1])/1e6
                    except ValueError: continue
                    emit(stage='encode',progress=.02+.95*min(1,done/max(.01,length)),completed=round(done,2),total=length,
                         unit='giây',message=f'Che nhẹ / mã hóa: {done:.1f}/{length:.1f}s · {label}')
            if proc.wait() != 0:
                raise RuntimeError('FFmpeg che nhẹ không hoàn tất. Xem cover.log của tác vụ; nguồn giữ nguyên.')
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
    return {'files':['clean.mp4'],'type':job['kind'],'duration':length,'signature':signature(project),'encoder':label,
            'start':start, 'end':start+length, 'requested_time':job.get('time',0),
            'elapsed_seconds':time.perf_counter()-began, 'method':p['method'], 'ai_inference':False}
