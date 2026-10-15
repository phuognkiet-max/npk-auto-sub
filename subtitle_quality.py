"""Evidence-aware subtitle review. A gap is a review request, not invented speech."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def source_signature(project):
    media = project['media']
    path = Path(media['path'])
    try:
        stat = path.stat()
        identity = [str(path.resolve()), stat.st_size, stat.st_mtime_ns]
    except OSError:
        identity = [str(path), 'missing']
    return digest([identity, media['duration']])


def review_signature(project):
    # Coverage review concerns source content/time, not the later translation/font.
    rows = [[r.get('start'),r.get('end'),r.get('text','')] for r in project.get('segments',[])]
    return digest([source_signature(project), rows, project.get('coverage')])


def coverage_record(project, method, start, end, **details):
    return {'source_signature': source_signature(project), 'method': method,
            'analyzed_start': float(start), 'analyzed_end': float(end), **details}


def inspect_subtitles(project, require_translation=False):
    rows, duration = project.get('segments', []), float(project['media']['duration'])
    issues = []
    def add(code, message, severity='warning', index=None, start=None, end=None):
        issues.append(dict(code=code, message=message, severity=severity, index=index, start=start, end=end))
    if not rows:
        add('empty', 'Chưa có phụ đề. Tạo từ video hoặc nhập tệp phụ đề.', 'error')
    previous = None
    from subtitle_fonts import font_warnings
    if project['params'].get('burn_subtitles'):
        for message in font_warnings(project):add('font_missing',message,'error')
    for i, row in enumerate(rows):
        start, end = float(row['start']), float(row['end'])
        text = str(row.get('translated') or row.get('text', '')).strip()
        if start < 0 or end <= start or end > duration + .05:
            add('timing', f'Câu {i+1}: mốc thời gian nằm ngoài video hoặc không hợp lệ.', 'error', i, start, end)
        if not text:
            add('blank', f'Câu {i+1}: chưa có nội dung.', 'error', i, start, end)
        source_text=str(row.get('text',''))
        if project.get('coverage',{}).get('method')=='ocr' and len(''.join(c for c in source_text if c.isalnum()))<=1 and end-start<=1:
            # A short character may also be a real interjection. This OCR hint
            # is not evidence that recognition is incomplete and must not stop
            # an otherwise complete unattended pipeline or delete the text.
            add('brief_fragment',f'Câu {i+1}: chỉ có một ký tự xuất hiện rất ngắn; có thể nhận nhầm họa tiết. Xem khung gốc trước khi giữ hoặc xóa.','warning',i,start,end)
        if require_translation and not (str(row.get('translated', '')).strip() or str(row.get('spoken_text', '')).strip()):
            add('untranslated', f'Câu {i+1}: chưa có bản dịch hoặc lời đọc riêng.', 'error', i, start, end)
        if previous and start < float(previous['end']) - .03:
            add('overlap', f'Câu {i+1}: chồng thời gian với câu trước.', index=i, start=start, end=end)
        if previous and row.get('text')==previous.get('text') and start<=float(previous['end'])+.03:
            add('duplicate','Hai câu liên tiếp có cùng lời gốc; kiểm tra có cần gộp hay không.',index=i,start=start,end=end)
        style=project['params']|row.get('subtitle_style',{})
        from subtitle_layout import wrap_caption
        single = style.get('subtitle_flow', 'wrap') == 'single'
        if single:
            from subtitle_flow import caption_chunks
            pages = caption_chunks(row, text, style, {'width': 1920, 'height': 1080} | project['media'])
            if project['params'].get('burn_subtitles') and any(page.get('layout_overflow') for page in pages):
                add('caption_fit', f'Câu {i+1}: có từ quá dài để vừa một dòng ở cỡ chữ đọc được. Giảm lề, sửa khoảng trắng hoặc chọn Cả câu · xuống dòng.', 'error', i, start, end)
        elif len(wrap_caption(text,style).splitlines()) > int(style.get('subtitle_max_lines',2)):
            add('line_budget',f'Câu {i+1}: vượt số dòng mong muốn. Chia câu hoặc tăng chiều dài dòng; không tự cắt bỏ chữ.',index=i,start=start,end=end)
        if 2*style.get('subtitle_margin_x',40)>=project['media'].get('width',1920) or style.get('margin',40)+style.get('font_size',46)>project['media'].get('height',1080):
            add('unsafe_position',f'Câu {i+1}: lề/cỡ chữ có thể đưa phụ đề ra khỏi khung hình.','error',i,start,end)
        if text and len(text.split()) / max(.05, end-start) > float(project['params'].get('words_per_second', 3.6)):
            add('reading', f'Câu {i+1}: nhiều chữ trong thời gian ngắn; nghe kiểm tra nhịp đọc và điều chỉnh mốc nếu cần. Nội dung được giữ đầy đủ.', index=i, start=start, end=end)
        if len(text) > 110 and not single:
            add('line_length', f'Câu {i+1}: nhiều chữ, cần xem lại cách xuống dòng.', index=i, start=start, end=end)
        previous = row
    coverage = project.get('coverage') or {}
    selection = coverage.get('ocr_selection') or {}
    from ocr_tracks import selection_quality_issues
    for issue in selection_quality_issues(selection):
        add(issue['code'], issue['message'], issue['severity'])
    known = coverage.get('source_signature') == source_signature(project)
    if known and coverage.get('clipped_samples'):
        moment=coverage['clipped_samples'][0]
        # Touching a rectangle edge is a geometric suspicion, not proof of
        # missing words. Keep it visible without requiring a manual approval.
        add('cropped_text','Có chữ chạm mép vùng đọc, có thể bị cắt mất đầu/cuối câu. Nới vùng rồi nhận dạng lại hoặc kiểm các mốc trong chi tiết.','warning',start=moment,end=min(duration,moment+2))
    if rows and (not known or coverage.get('method') == 'import'):
        add('coverage_unknown', 'Chưa có bằng chứng đã kiểm toàn bộ video. Xem lại các đoạn không có phụ đề hoặc nhận dạng lại.', 'review', start=0, end=duration)
    elif rows and (coverage.get('analyzed_start', 0) > .1 or coverage.get('analyzed_end', 0) < duration - .25):
        add('coverage_partial', 'Lượt nhận dạng mới chỉ phân tích một phần video.', 'review',
            start=coverage.get('analyzed_end', 0), end=duration)
    # A long trailing gap is suspicious, but may be deliberate silence/music.
    if rows:
        last = max(float(row['end']) for row in rows)
        if duration - last > max(10, duration * .15):
            add('trailing_gap', f'Phụ đề kết thúc ở {last:.2f} giây, video dài {duration:.2f} giây. Kiểm tra phần còn lại; có thể là nhạc hoặc nội dung chưa nhận ra.',
                'review', start=last, end=duration)
        cursor=0.0
        for row in sorted(rows,key=lambda r:r['start']):
            if row['start']-cursor > max(12,duration*.15):
                add('internal_gap','Có một khoảng dài không có phụ đề. Xem lại để xác định nhạc/im lặng hay nội dung chưa nhận dạng.',
                    'review',start=cursor,end=row['start'])
            cursor=max(cursor,row['end'])
    reviewed = project.get('subtitle_review', {}).get('signature') == review_signature(project)
    return {'issues': issues, 'count': len(rows), 'duration': duration, 'coverage': coverage,
            'reviewed': reviewed, 'requires_review': not reviewed and any(x['severity'] == 'review' for x in issues),
            'errors': [x['message'] for x in issues if x['severity'] == 'error'],
            'signature': review_signature(project)}


def clean_ready(project):
    from stage_journal import artifact_integrity
    return artifact_integrity(project, 'clean')['status'] == 'verified'


def speech_ready(project):
    from stage_journal import artifact_integrity
    return artifact_integrity(project, 'tts')['status'] == 'verified'


def clean_region_warnings(project):
    """Geometric hint only: a reading rectangle is not proof of text everywhere."""
    if not project['params'].get('use_clean'):
        return []
    reading=project.get('extraction_regions',[])
    cleaning=project.get('regions',[])
    uncovered=[]
    for i,r in enumerate(reading):
        if not any(c['x']<=r['x'] and c['y']<=r['y'] and
                   c['x']+c['w']>=r['x']+r['w'] and c['y']+c['h']>=r['y']+r['h'] and
                   c.get('start',0)<=r.get('start',0) and
                   c.get('end',project['media']['duration'])>=r.get('end',project['media']['duration'])
                   for c in cleaning):
            uncovered.append(str(i+1))
    return ([f'Vùng đọc {", ".join(uncovered)} rộng hơn hoặc khác thời gian vùng che. '
             'Có thể còn chữ gốc ở mép/giữa hình. Xem mẫu tại các mốc có chữ; '
             'kiểm tra hoặc sao chép vùng trong Chữ gốc. Đây là gợi ý hình học, không phải kết luận tự động về chất lượng.'] if uncovered else [])


def export_plan(project):
    p = project['params']
    from processing_plan import content_requirements
    content = content_requirements(project, 'pipeline')
    qa = inspect_subtitles(project)
    if content['needs_voice_text'] and not content['speech_text_pending'] and not content['future_rows']:
        from speech_delivery import require_speech_text
        try:
            require_speech_text(project.get('segments', []), p)
        except ValueError as error:
            qa['errors'].append(str(error))
    need_text = p.get('burn_subtitles') or (p.get('voice_gain', 0)>0 and not p.get('voice_path'))
    steps = []
    missing_translation = any(not r.get('translated', '').strip() for r in project.get('segments', []))
    if need_text and content['will_translate']:
        steps.append({'id':'translate', 'label':'Dịch những câu còn thiếu', 'reuse':False})
    if p.get('voice_gain', 0)>0 and not p.get('voice_path'):
        steps.append({'id':'tts','label':'Tạo lồng tiếng', 'reuse':speech_ready(project) and not content['speech_text_pending']})
    if p.get('use_clean'):
        steps.append({'id':'clean','label':'Xử lý chữ gốc', 'reuse':clean_ready(project)})
    steps.append({'id':'render','label':'Ghép phụ đề, âm thanh và xuất video', 'reuse':False})
    from resource_budget import estimate_space
    return {'steps': steps, 'subtitles': qa, 'needs_subtitles': bool(need_text), 'space':estimate_space(project,'pipeline'),
            'clean_warnings':clean_region_warnings(project), 'content':content}
