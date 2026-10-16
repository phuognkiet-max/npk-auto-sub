"""Cheap local checks before work is queued. No external provider calls."""
import importlib.util
import math
from pathlib import Path
import shutil
from core import MODELS, binary


def inspect(project, kind, *, check_credentials=True):
    # Resolve DATA at call time. Import-time binding can retain a temporary
    # test/app data root after its owner has switched or cleaned that root.
    from core import DATA
    from credentials import active_secret
    from voices import resolve_voice
    p, rows = project['params'], project.get('segments', [])
    errors, warnings = [], []
    pipeline = kind == 'pipeline'
    auto_pending = False
    automation = None
    if kind in ('pipeline', 'auto_subtitles'):
        from auto_subtitle_director import automation_status
        automation = automation_status(project)
        auto_pending = bool(pipeline and automation.get('can_process') and p.get('pipeline_extract','existing')=='existing')
        if kind == 'auto_subtitles' and not automation.get('available'):
            errors.append(automation['reason'])
        if kind == 'auto_subtitles' and p.get('subtitle_automation','manual')=='manual':
            errors.append('Chọn Tự động trên máy, Hỗ trợ Gemini hoặc 9Router để tự xử lý phụ đề.')
    director_provider = p.get('subtitle_automation')
    uses_director = director_provider in ('gemini', 'router9') and (
        kind in ('ocr','auto_subtitles') or pipeline and (p.get('pipeline_extract')=='ocr' or auto_pending))
    if uses_director:
        label = '9Router' if director_provider == 'router9' else 'Gemini'
        if p.get('provider') != director_provider:
            errors.append('Chọn kết nối ' + label + ' để dùng điều phối phụ đề AI.')
        elif check_credentials and not active_secret(director_provider):
            errors.append('Chưa chọn API key ' + label + ' để điều phối phụ đề.')
        warnings.append(label + ' nhận chữ OCR và tối đa 8 ảnh mẫu để phân loại phụ đề; cần model hỗ trợ ảnh, có thể tính phí. Giữ model/key đã chọn.')
    standalone_speech = kind == 'text_to_speech'
    extract = p.get('pipeline_extract', 'existing') if pipeline else kind
    if not standalone_speech and not Path(project['media']['path']).is_file():
        errors.append('Không còn video nguồn tại đường dẫn đã lưu.')
    if extract == 'asr' and not project['media'].get('audio'):
        errors.append('Video không có âm thanh để nhận dạng lời nói; chọn OCR hoặc nhập SRT.')
    if kind == 'separate_audio':
        import psutil
        from audio_separation import separation_status, MAX_DURATION
        status = separation_status()
        if not project['media'].get('audio'):
            errors.append('Video không có âm thanh để tách giọng/nhạc.')
        duration = float(project['media'].get('duration', 0))
        if not math.isfinite(duration) or not 0 < duration <= MAX_DURATION:
            errors.append('Tách giọng/nhạc hỗ trợ nguồn dài hơn 0 và tối đa 3 giờ mỗi lượt.')
        if not status.get('installed'):
            errors.append('Chưa cài model tách giọng/nhạc. Chạy tools/setup_audio_separation.py trước.')
        if psutil.virtual_memory().available < 3 * 1024**3:
            errors.append('Tách giọng/nhạc cần ít nhất 3 GiB RAM đang trống để dành chỗ cho ONNX, decoder và runtime. '
                          'Đóng bớt ứng dụng rồi thử lại.')
        warnings.append('Tách giọng/nhạc offline trên CPU; nghe lại stem vì có thể còn giọng gốc, mất nhạc hoặc có âm giả.')
    needs_text=p.get('burn_subtitles') or (p.get('voice_gain',0)>0 and not p.get('voice_path'))
    if (kind in ('translate','tts') or (pipeline and extract == 'existing' and needs_text)) and not rows:
        errors.append('Chưa có phụ đề. Chọn OCR/ASR hoặc nhập SRT trước.')
    from processing_plan import content_requirements
    content = content_requirements(project, kind)
    if auto_pending:
        content = dict(content, future_rows=True, will_translate=bool(p.get('pipeline_translate',True)),
                       speech_text_pending=bool(p.get('pipeline_translate',True)))
    translation = content['will_translate']
    delivery = None
    if p.get('provider') == 'router9' and (translation or uses_director or kind == 'optimize_spoken'):
        from providers import router_base_url, safe_model
        try:
            router_base_url(p.get('router9_base_url'))
            safe_model(p.get('translation_model', ''), provider='router9')
        except ValueError as exc:
            errors.append('Kết nối 9Router: ' + str(exc) + ' Tải danh mục và chọn model trước khi chạy.')
    if translation and check_credentials and not active_secret(p['provider']):
        errors.append('Chưa chọn API key đã lưu cho ' + p['provider'] + '. Mở API / key.')
    if translation:
        warnings.append('Phụ đề và hướng dẫn sẽ gửi đến ' + p['provider'] + '; có thể tính phí. Không tự đổi key/model khi hết hạn mức.')
    tts = kind in ('tts', 'text_to_speech') or (pipeline and p.get('pipeline_tts', True))
    if tts and pipeline and extract == 'existing' and not translation:
        from subtitle_quality import speech_ready
        if speech_ready(project):
            tts = False
    if tts:
        # TTS admission must inspect the same effective rows as worker/cache.
        # Keep the authored caption table above for coverage/translation QA.
        speech_rows = rows
        if not standalone_speech and extract not in ('ocr', 'asr') and not auto_pending:
            from speech_delivery import effective_speech_rows
            try:
                speech_rows = effective_speech_rows(rows, p)
            except ValueError as exc:
                errors.append(str(exc))
        used_voice_ids = {r.get('voice_id') or p.get('voice_id','piper:vais1000') for r in speech_rows
                          if r.get('speech_enabled', True) is not False}
        if not speech_rows:
            used_voice_ids = {p.get('voice_id','piper:vais1000')}
        elif not used_voice_ids and extract not in ('ocr','asr'):
            errors.append('Không có câu bật lồng tiếng. Bật Lồng tiếng câu này hoặc tắt bước lồng tiếng để chỉ xuất phụ đề.')
        if extract in ('ocr','asr'):
            used_voice_ids = {p.get('voice_id','piper:vais1000')}
        try:
            for voice_id in used_voice_ids:
                resolve_voice(voice_id, require_ready=True)
        except ValueError as exc:
            errors.append(str(exc))
        if any(str(voice_id).startswith(('vieneu:', 'cit:')) for voice_id in used_voice_ids):
            warnings.append('VieNeu tạo giọng offline bằng model và runtime của NPK; không cần mở ứng dụng khác. '
                            'Bộ xử lý được nạp khi chạy tác vụ; chưa có tự phân tích cảm xúc video.')
        if standalone_speech:
            text = str(project.get('speech_text', '')).strip()
            if not text or len(text) > 10000:
                errors.append('Văn bản tạo giọng cần từ 1 đến 10.000 ký tự.')
        if not standalone_speech and not auto_pending:
            from speech_delivery import require_speech_text, text_source
            try:
                source = text_source(p)
                # Recognition and an enabled translation step create the text
                # later in a pipeline; direct TTS must validate its choice now.
                if not translation:
                    if extract in ('ocr','asr') and source != 'source':
                        errors.append('Nguồn lồng tiếng cần bản dịch/lời đã biên tập. Bật bước dịch hoặc chọn Phụ đề gốc.')
                    elif extract not in ('ocr','asr'):
                        require_speech_text(speech_rows, p)
                if source == 'source':
                    warnings.append('Lồng tiếng sẽ đọc Phụ đề gốc theo lựa chọn của bạn; chọn giọng hỗ trợ ngôn ngữ nguồn.')
            except ValueError as exc:
                errors.append(str(exc))
        if not standalone_speech and extract not in ('ocr','asr') and not auto_pending:
            from speech_delivery import delivery_window
            for index, row in enumerate(speech_rows):
                try:
                    delivery_window(row, p, index + 1, project['media']['duration'])
                except ValueError as exc:
                    errors.append(str(exc))
            if not content['speech_text_pending'] and not errors:
                from speech_preparation import inspect_cached_delivery, require_delivery
                try:
                    delivery = inspect_cached_delivery(project)
                    require_delivery(delivery)
                except ValueError as exc:
                    errors.append(str(exc))
        if not standalone_speech and p.get('tts_timing_mode', 'legacy') == 'natural':
            warnings.append('Tự bố trí lời đọc: đo đủ mọi câu, tận dụng khoảng trống, giữ toàn bộ nội dung và mốc phụ đề; căn nhanh tối đa 1,25×.'
                            if p.get('tts_auto_schedule', False) else
                            'Ưu tiên giọng tự nhiên: chỉ căn nhanh tối đa 1,15× sau tổng hợp. Câu quá dài cần rút gọn lời đọc hoặc đổi mốc thời gian; chưa có tự phân tích cảm xúc.')
    clean = kind in ('preview','clip','clean','scan') or (pipeline and p.get('pipeline_clean',False))
    if clean and not project.get('regions'):
        errors.append('Chưa vẽ vùng che/xóa phụ đề.')
    if clean and p.get('method') in ('box','blur','stretch'):
        if p.get('mask_source') == 'tracked':
            errors.append('Ba kiểu che nhẹ dùng vùng thủ công theo thời gian. Chọn nguồn mặt nạ Thủ công; không bỏ qua tracking ngầm.')
        if len(project.get('regions', [])) > 100:
            errors.append('Che nhẹ hỗ trợ tối đa 100 vùng trong một tác vụ.')
    if clean and p.get('method') == 'lama':
        warnings.append('LaMa phục hồi nền từng khung, nặng hơn che Blur/Hộp/Kéo nền; nên thử 3 giây trước.')
        if kind != 'scan' and not (MODELS/'lama/big-lama.pt').is_file():
            errors.append('Chưa có model phục hồi nền AI. Kiểm tra bộ xử lý trong Cài đặt trước khi chạy.')
    if extract == 'asr':
        warnings.append('Whisper có thể tải model còn thiếu về kho riêng trên D; không phải FunASR của app khác.')
        from hardware_admission import inspect_asr
        admission = inspect_asr(p)
        errors.extend(admission['errors'])
        warnings.extend(admission['warnings'])
    if DATA.drive and shutil.disk_usage(DATA).free < 2*1024**3:
        errors.append('Ổ chứa dữ liệu còn dưới 2 GiB; giải phóng chỗ trước khi xử lý.')
    if pipeline and p.get('pipeline_tts') and p.get('original_gain',0)>0:
        warnings.append('Âm nguồn vẫn bật: giọng gốc có thể chồng lên lồng tiếng. Chọn stem nhạc đã tách để phối nếu cần.')
    if kind == 'render' or pipeline:
        from subtitle_quality import inspect_subtitles
        needs_text = p.get('burn_subtitles') or (p.get('voice_gain',0)>0 and not p.get('voice_path'))
        if needs_text and extract not in ('ocr','asr') and not auto_pending:
            quality = inspect_subtitles(project)
            errors.extend(quality['errors'])
            if quality['requires_review']:
                errors.append('Cần kiểm tra phạm vi phụ đề trước khi xuất. Mở Phụ đề → Kiểm tra; nhận dạng lại hoặc xác nhận đã xem các khoảng trống.')
        from core import signature
        from worker import speech_signature
        will_clean = pipeline and p.get('pipeline_clean',False)
        will_tts = pipeline and p.get('pipeline_tts',True)
        from stage_journal import artifact_integrity
        artifacts = project.get('artifacts',{})
        if p.get('use_clean') and not will_clean:
            artifact = artifacts.get('clean')
            state = artifact_integrity(project, 'clean', artifact)
            if state['status'] == 'legacy_unanchored':
                warnings.append('Bản xử lý chữ cũ sẽ được kiểm lại toàn bộ tệp trước khi dùng để xuất.')
            elif state['status'] != 'verified':
                errors.append('Đang bật Dùng bản đã xóa chữ nhưng chưa có bản còn khớp cấu hình. Bật bước che của quy trình hoặc bỏ Dùng bản đã xóa chữ ở Âm thanh / xuất.')
        if will_tts and p.get('voice_path'):
            errors.append('Đang chọn tệp giọng ngoài: xóa đường dẫn Giọng ngoài để dùng giọng mới tạo, hoặc bỏ bước tạo lồng tiếng.')
        artifact = artifacts.get('tts')
        if not will_tts and not p.get('voice_path') and p.get('voice_gain',0)>0 and not artifact:
            errors.append('Đang bật âm lượng lồng tiếng nhưng chưa có giọng. Tạo giọng, chọn audio ngoài hoặc đặt âm lượng giọng bằng 0.')
        if not will_tts and not p.get('voice_path') and p.get('voice_gain',0)>0 and artifact:
            if (pipeline and (translation or extract in ('ocr','asr'))) or artifact.get('speech_signature') != speech_signature(project):
                errors.append('Giọng đã tạo không khớp bảng phụ đề hiện tại/sắp thay. Bật tạo lại giọng, dùng tệp giọng ngoài hoặc đặt gain giọng bằng 0.')
            elif not (DATA/'jobs'/artifact.get('job_id', '')/'voice.wav').is_file():
                errors.append('Không còn tệp giọng đã tạo. Tạo lại lồng tiếng trước khi xuất.')
            else:
                state = artifact_integrity(project, 'tts', artifact)
                if state['status'] == 'legacy_unanchored':
                    warnings.append('Giọng cũ sẽ được kiểm lại toàn bộ tệp trước khi dùng để xuất.')
                elif state['status'] != 'verified':
                    errors.append('Tệp giọng đã tạo không còn khớp lần kiểm chứng. Tạo lại lồng tiếng trước khi xuất.')
        for key in ('voice_path','bgm_path'):
            if p.get(key):
                from audio_inputs import inspect_audio
                try:
                    audio = inspect_audio(p[key], project['media']['duration'])
                    if key == 'voice_path':
                        warnings.extend(audio['warnings'])
                except (ValueError, FileNotFoundError) as exc:
                    errors.append('Tệp âm thanh ' + key + ': ' + str(exc))
    from resource_budget import estimate_space
    space = estimate_space(project, kind)
    if space['free_bytes'] < space['required_bytes']:
        errors.append('Ổ dữ liệu không đủ chỗ dự phòng cho tác vụ này. Cần khoảng %.1f GiB.' % (space['required_bytes']/1024**3))
    return {'ok': not errors, 'errors': list(dict.fromkeys(errors)), 'warnings': warnings, 'space': space,
            'content': content, 'delivery': delivery, 'automation':automation}


def engines():
    from voices import catalog, provider_status
    voices = catalog()
    vieneu = provider_status().get('vieneu', {})
    from audio_separation import separation_status
    separation = separation_status()
    rate = vieneu.get('sample_rate')
    vieneu_label = 'VieNeu · NPK offline' + (f' · {rate/1000:g} kHz' if isinstance(rate, (int,float)) and math.isfinite(rate) and rate > 0 else '')
    return {'voices': len([v for v in voices if v['installed']]), 'items': [
        {'name':'FFmpeg', 'available':Path(binary('ffmpeg')).is_file(), 'note':'Mã hóa, che nhẹ, trộn âm và xuất video.'},
        {'name':'Piper / ONNX Runtime', 'available':bool(importlib.util.find_spec('piper') and importlib.util.find_spec('onnxruntime')), 'note':'Offline CPU; tải từng giọng khi dùng, không nạp cả thư viện.'},
        {'name':vieneu_label, 'available':bool(vieneu.get('available')),
         'note':str(vieneu.get('message') or 'Dùng model và runtime riêng của NPK, không cần ứng dụng khác hoặc Internet. Chỉ nạp khi tạo giọng.')},
        {'name':'RapidOCR', 'available':bool(importlib.util.find_spec('rapidocr_onnxruntime')), 'note':'OCR CPU; lấy mẫu theo FPS và ngưỡng đã chọn.'},
        {'name':'Whisper', 'available':bool(importlib.util.find_spec('faster_whisper')), 'note':'Có runtime; model được tải khi dùng lần đầu nếu chưa có.'},
        {'name':'LaMa', 'available':(MODELS/'lama/big-lama.pt').is_file(), 'note':'Có model không đồng nghĩa đã đo chất lượng hay nhiệt độ.'},
        {'name':'UVR MDX · tách giọng/nhạc offline', 'available':bool(separation['installed']),
         'note':separation['note'] + ' Chỉ nạp model khi chạy tác vụ.'},
        {'name':'FunASR / PaddleOCR / Demucs / giọng online', 'available':False, 'note':'Chưa tích hợp trong NPK. Không sử dụng runtime/tài khoản của Xtrans.'}]}
