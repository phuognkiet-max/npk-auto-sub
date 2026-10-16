"""User-facing workflows; existing authenticated engine APIs remain reusable."""
import copy
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import time
import uuid
from fastapi import HTTPException, UploadFile, File, Form, Request
from fastapi.responses import FileResponse, Response
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from core import ROOT, DATA, all_items, get, put, save_project_version, probe, connect
from subtitle_quality import inspect_subtitles, export_plan, review_signature, coverage_record


VIDEO_UPLOAD_LIMIT = 8 * 1024**3
VIDEO_UPLOAD_RESERVE = 2 * 1024**3
VIDEO_UPLOAD_OVERHEAD = 64 * 1024
VIDEO_EXTENSIONS = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.m4v'}


class _VideoUpload:
    """Stream one multipart video directly to its own data folder, never TEMP."""
    def __init__(self, directory):
        self.directory = directory
        self.path = self.output = None
        self.filename = self.name = ''
        self.field = ''
        self.headers = {}
        self.header_field = self.header_value = bytearray()
        self.field_value = bytearray()
        self.fields = set()
        self.total = 0
        self.complete = False

    def part_begin(self):
        self.headers = {}
        self.header_field = bytearray()
        self.header_value = bytearray()
        self.field_value = bytearray()
        self.field = ''

    def header_field_data(self, data, start, end):
        self.header_field.extend(data[start:end])

    def header_value_data(self, data, start, end):
        self.header_value.extend(data[start:end])

    def header_end(self):
        key = bytes(self.header_field).lower()
        if key in self.headers:
            raise ValueError('Header tải video bị lặp.')
        self.headers[key] = bytes(self.header_value)
        self.header_field = bytearray()
        self.header_value = bytearray()

    def headers_finished(self):
        from python_multipart.multipart import parse_options_header
        raw_disposition = self.headers.get(b'content-disposition', b'')
        disposition, options = parse_options_header(raw_disposition)
        if disposition != b'form-data':
            raise ValueError('Dữ liệu tải video không hợp lệ.')
        try:
            self.field = options.get(b'name', b'').decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('Tên trường tải video không hợp lệ.') from exc
        if self.field not in {'file', 'name'} or self.field in self.fields:
            raise ValueError('Chỉ nhận một video và một tên dự án mỗi lượt.')
        self.fields.add(self.field)
        if self.field == 'name':
            if b'filename' in options:
                raise ValueError('Tên dự án phải là văn bản.')
            return
        # python-multipart strips old Internet Explorer C:\\ filenames. Reject
        # that raw path too, before treating the normalized basename as safe.
        if b':\\' in raw_disposition or b':/' in raw_disposition:
            raise ValueError('Tên tệp video không được chứa đường dẫn.')
        try:
            filename = options.get(b'filename', b'').decode('utf-8')
        except UnicodeDecodeError as exc:
            raise ValueError('Tên tệp video không hợp lệ.') from exc
        if (not filename or len(filename) > 255 or filename != filename.strip() or
                any(ord(char) < 32 or char in '\\/:*?"<>|' for char in filename) or
                filename.endswith(('.', ' ')) or filename in {'.', '..'}):
            raise ValueError('Tên tệp video không được chứa đường dẫn hoặc ký tự không hợp lệ.')
        suffix = Path(filename).suffix.lower()
        if suffix not in VIDEO_EXTENSIONS:
            raise ValueError('Chọn video MP4, MKV, MOV, AVI, WebM hoặc M4V.')
        self.filename = filename
        # The supplied name is display metadata only, never an output path.
        self.path = self.directory / ('source' + suffix)
        self.output = self.path.open('xb')

    def part_data(self, data, start, end):
        block = data[start:end]
        if self.field == 'name':
            if len(self.field_value) + len(block) > 1024:
                raise ValueError('Tên dự án quá dài.')
            self.field_value.extend(block)
            return
        if self.field != 'file' or self.output is None:
            raise ValueError('Thiếu tệp video tải lên.')
        if self.total + len(block) > VIDEO_UPLOAD_LIMIT:
            raise HTTPException(413, 'Video tải lên vượt giới hạn 8 GiB. Dùng Chọn video trên máy cho tệp lớn hơn.')
        if shutil.disk_usage(DATA).free < len(block) + VIDEO_UPLOAD_RESERVE:
            raise HTTPException(507, 'Ổ dữ liệu không đủ chỗ tải video và giữ 2 GiB dự phòng.')
        self.output.write(block)
        self.total += len(block)

    def part_end(self):
        if self.field == 'file':
            self.close()
        elif self.field == 'name':
            try:
                self.name = self.field_value.decode('utf-8').strip()
            except UnicodeDecodeError as exc:
                raise ValueError('Tên dự án cần dùng văn bản UTF-8.') from exc
            if len(self.name) > 200 or any(ord(char) < 32 for char in self.name):
                raise ValueError('Tên dự án cần tối đa 200 ký tự, không chứa ký tự điều khiển.')

    def finish(self):
        self.complete = True

    def close(self):
        if self.output is not None:
            self.output.close()
            self.output = None


def translation_original_input(row, project):
    """Read the bounded original snapshot shared by recovery and scoped retry."""
    from subtitle_quality import source_signature
    folder = (DATA/'jobs'/row['id']).resolve()
    snapshot_path = (folder/'input.json').resolve()
    if snapshot_path.parent != folder or not snapshot_path.is_file() or snapshot_path.stat().st_size > 32 * 1024**2:
        raise ValueError('Thiếu snapshot đầu vào hợp lệ để kiểm tra bản dịch dở. Giữ nguyên phụ đề hiện tại.')
    try:
        with snapshot_path.open('rb') as stream:
            content = stream.read(32 * 1024**2 + 1)
        if len(content) > 32 * 1024**2:
            raise ValueError('Snapshot quá lớn.')
        snapshot = json.loads(content.decode('utf-8'))
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError('Không đọc được snapshot đầu vào của lượt dịch dở.') from exc
    original = snapshot.get('project') if isinstance(snapshot, dict) else None
    original_job = snapshot.get('job') if isinstance(snapshot, dict) else None
    media = original.get('media') if isinstance(original, dict) else None
    if (not isinstance(media, dict) or not isinstance(media.get('path'), str) or
            type(media.get('duration')) not in (int, float) or
            not isinstance(original.get('params'), dict) or
            not isinstance(original.get('segments'), list)):
        raise ValueError('Snapshot đầu vào thiếu video hoặc bảng phụ đề hợp lệ.')
    if (not isinstance(original, dict) or not isinstance(original_job, dict) or
            original.get('id') != project['id'] or original_job.get('id') != row['id'] or
            original_job.get('kind') != row['kind'] or
            original_job.get('project_id') != row.get('project_id') or
            original_job.get('segment_indices') != row.get('segment_indices') or
            original_job.get('overwrite_user_edits', False) != row.get('overwrite_user_edits', False) or
            original.get('source_revision') != project.get('source_revision') or
            source_signature(original) != source_signature(project)):
        raise ValueError('Bản dịch dở không thuộc snapshot video hiện tại; không tự áp dụng.')
    original_target = str(original['params'].get('target_language', '')).strip()
    current_target = str(project['params'].get('target_language', '')).strip()
    if original_target.casefold() != current_target.casefold():
        raise ValueError(f'Bản dịch dở thuộc ngôn ngữ “{original_target}”, còn dự án đang chọn “{current_target}”. '
                         'Giữ nguyên phụ đề hiện tại. Chọn lại ngôn ngữ gốc nếu muốn khôi phục bản dịch này.')
    return original


def translation_retry_indices(row, project):
    """A first-batch failure must retain its scope even without a checkpoint."""
    original = translation_original_input(row, project)
    rows, current = original['segments'], project['segments']
    if len(rows) != len(current) or any(
            not isinstance(saved, dict) or
            any(saved.get(key) != cue.get(key) for key in ('start', 'end', 'text'))
            for saved, cue in zip(rows, current)):
        raise ValueError('Bảng phụ đề đã đổi so với lượt dịch trước. Chọn lại các câu cần dịch; không tự mở rộng phạm vi.')
    indices = row.get('segment_indices')
    if indices is None:
        indices = list(range(len(rows)))
    if (not isinstance(indices, list) or any(type(index) is not int or not 0 <= index < len(rows) for index in indices)):
        raise ValueError('Không xác minh được phạm vi dịch trước; chọn lại các câu cần dịch.')
    missing = sorted({index for index in indices if not current[index].get('translated', '').strip() and
        (current[index].get('user_edited') is not True or row.get('overwrite_user_edits') is True)})
    if not missing:
        raise ValueError('Các câu thuộc lượt dịch trước đã có bản dịch. Không tự dịch lại hoặc ghi đè câu đã sửa.')
    return missing


def translation_restore_candidates(row, project):
    """Validate original input before filling only newer editor's missing cues."""
    from translation_checkpoint import read_checkpoint, validated_partial_rows
    original = translation_original_input(row, project)
    folder = (DATA/'jobs'/row['id']).resolve()
    checkpoint = (folder/'translation-checkpoint.json').resolve()
    if checkpoint.parent != folder:
        raise ValueError('Checkpoint dịch nằm ngoài thư mục tác vụ.')
    document = read_checkpoint(checkpoint)
    rows = validated_partial_rows(document, original, row.get('segment_indices'),
                                  overwrite_user_edits=row.get('overwrite_user_edits') is True)
    if isinstance(document, dict):
        # Other rows may contain translations that existed BEFORE this job.
        # Only completed_indices prove this job actually translated the cue.
        eligible = set(document['completed_indices'])
    else:
        selected = row.get('segment_indices')
        selected = set(range(len(rows))) if selected is None else set(selected)
        eligible = {index for index, cue in enumerate(original['segments']) if index in selected and
                    (original['params'].get('translation_scope') != 'missing' or
                     not cue.get('translated', '').strip())}
    confirmed = {index:rows[index]['translated'].strip() for index in eligible
                 if rows[index].get('translated', '').strip()}
    current = project['segments']
    identity = lambda cue: (cue['start'], cue['end'], cue['text'])
    if len(rows) == len(current) and all(identity(saved) == identity(cue) for saved, cue in zip(rows, current)):
        # Exact ordering preserves the selected row even when identities repeat.
        return [(index, text) for index, text in sorted(confirmed.items())
                if not current[index].get('translated', '').strip() and current[index].get('user_edited') is not True]
    from collections import Counter
    saved_counts = Counter(identity(cue) for cue in rows)
    current_counts = Counter(identity(cue) for cue in current)
    values = {}
    for index, text in confirmed.items():
        key = identity(rows[index])
        if saved_counts[key] != 1 or current_counts[key] > 1:
            raise ValueError('Bảng phụ đề đã đổi và có câu trùng nội dung/mốc thời gian. '
                             'Không xác định được câu thuộc phạm vi dịch cũ; cần kiểm tra thủ công.')
        values[key] = text
    return [(index, values[key]) for index, cue in enumerate(current)
            if not cue.get('translated', '').strip() and cue.get('user_edited') is not True and
            (key := identity(cue)) in values]


def install(app):
    import server as s

    @app.post('/api/studio/import-video')
    async def import_video(request: Request):
        from python_multipart.multipart import MultipartParser, parse_options_header
        from python_multipart.exceptions import MultipartParseError
        from storage_manager import owned, _ensure_no_pending
        media_type, options = parse_options_header(request.headers.get('content-type', ''))
        boundary = options.get(b'boundary')
        if media_type != b'multipart/form-data' or not boundary or len(boundary) > 200:
            raise ValueError('Thả một tệp video hoặc chọn video để tải lên.')
        declared = request.headers.get('content-length')
        if declared is not None:
            try:
                size = int(declared)
            except ValueError as exc:
                raise ValueError('Kích thước tải video không hợp lệ.') from exc
            if size < 0:
                raise ValueError('Kích thước tải video không hợp lệ.')
            if size > VIDEO_UPLOAD_LIMIT + VIDEO_UPLOAD_OVERHEAD:
                raise HTTPException(413, 'Video tải lên vượt giới hạn 8 GiB.')
            if shutil.disk_usage(DATA).free < size + VIDEO_UPLOAD_RESERVE:
                raise HTTPException(507, 'Ổ dữ liệu không đủ chỗ tải video và giữ 2 GiB dự phòng.')
        with s.state_lock:
            _ensure_no_pending()
        directory = owned(Path('sources') / uuid.uuid4().hex)
        directory.mkdir(parents=True, exist_ok=False)
        upload = _VideoUpload(directory)
        accepted = False
        try:
            parser = MultipartParser(boundary, callbacks={
                'on_part_begin': upload.part_begin,
                'on_header_field': upload.header_field_data,
                'on_header_value': upload.header_value_data,
                'on_header_end': upload.header_end,
                'on_headers_finished': upload.headers_finished,
                'on_part_data': upload.part_data,
                'on_part_end': upload.part_end,
                'on_end': upload.finish,
            })
            received = 0
            async for block in request.stream():
                received += len(block)
                if received > VIDEO_UPLOAD_LIMIT + VIDEO_UPLOAD_OVERHEAD:
                    raise HTTPException(413, 'Video tải lên vượt giới hạn 8 GiB.')
                try:
                    await run_in_threadpool(parser.write, block)
                except MultipartParseError as exc:
                    raise ValueError('Dữ liệu tải video bị hỏng. Thử thả lại tệp video.') from exc
            parser.finalize()
            if not upload.complete or not upload.path or not upload.total:
                raise ValueError('Tệp video tải lên rỗng hoặc chưa tải đầy đủ. Thử lại trước khi tạo dự án.')
            upload.close()
            # Reject an unreadable upload before holding the editor's state lock.
            try:
                media = await run_in_threadpool(probe, upload.path)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                raise ValueError('Không đọc được video tải lên. Kiểm tra tệp phát được và thử lại.') from exc
            if (not math.isfinite(media['duration']) or media['duration'] <= 0 or
                    not media['width'] > 0 or not media['height'] > 0):
                raise ValueError('Video tải lên cần có hình ảnh và thời lượng hợp lệ.')

            def commit():
                with s.state_lock:
                    _ensure_no_pending()
                    return s.create_project(str(upload.path), upload.name or Path(upload.filename).stem)

            project = await run_in_threadpool(commit)
            accepted = True
            return project
        finally:
            upload.close()
            if not accepted:
                # Never recurse, overwrite an old source, or remove through links.
                if upload.path is not None and owned(upload.path.relative_to(DATA)).resolve() == upload.path.resolve():
                    upload.path.unlink(missing_ok=True)
                if owned(directory.relative_to(DATA)).resolve() == directory.resolve():
                    try:
                        directory.rmdir()
                    except OSError:
                        pass

    @app.get('/studio')
    def studio():
        return FileResponse(ROOT/'static/studio.html')

    @app.get('/api/studio/state')
    def state(after: float = 0, project_id: str | None = None):
        from contextlib import closing
        # The cursor precedes the reads: a job completing during the response
        # is either present now or included by the next poll, never skipped.
        moment = time.time()
        with closing(connect()) as db:
            records = db.execute("""SELECT id, json_extract(value,'$.name'), json_extract(value,'$.updated'),
                json_extract(value,'$.media.duration'), json_array_length(value,'$.segments'),
                json_extract(value,'$.archived') FROM objects WHERE kind='project'
                ORDER BY json_extract(value,'$.updated') DESC""").fetchall()
            job_fields=('id','project_id','project_name','kind','state','stage','progress','message','created','started','ended','source_revision','queue_order','project_applied',
                        'stage_journal','failure_code','failure_stage','http_status','next_action',
                        'api_usage','quota_scope','retry_after')
            columns=','.join("json_extract(value,'$."+field+"')" for field in job_fields)
            columns += ",CASE WHEN json_extract(value,'$.kind')='pipeline' THEN json_extract(value,'$.configuration.pipeline_extract') ELSE json_extract(value,'$.kind') END"
            job_fields += ('extraction_method',)
            job_rows=db.execute("SELECT "+columns+" FROM objects WHERE kind='job' ORDER BY json_extract(value,'$.created') DESC LIMIT 60").fetchall()
            active_rows=db.execute("SELECT "+columns+" FROM objects WHERE kind='job' AND json_extract(value,'$.state') IN ('queued','running') ORDER BY json_extract(value,'$.created') DESC").fetchall()
            changed_rows = db.execute("SELECT "+columns+" FROM objects WHERE kind='job' AND json_extract(value,'$.ended')>=? ORDER BY json_extract(value,'$.ended') DESC", (after,)).fetchall() if after > 0 and math.isfinite(after) else []
            queued_count = db.execute("SELECT COUNT(*) FROM objects WHERE kind='job' AND json_extract(value,'$.state')='queued'").fetchone()[0]
            # A busy/last recognition must remain visible even after many voice previews.
            recognition = None
            source = db.execute("SELECT json_extract(value,'$.source_revision') FROM objects WHERE kind='project' AND id=?", (project_id,)).fetchone() if project_id else None
            if source:
                row = db.execute("SELECT "+columns+""" FROM objects WHERE kind='job'
                    AND json_extract(value,'$.project_id')=?
                    AND json_extract(value,'$.source_revision') IS ?
                    AND (json_extract(value,'$.kind') IN ('ocr','asr') OR
                      (json_extract(value,'$.kind')='pipeline' AND json_extract(value,'$.configuration.pipeline_extract') IN ('ocr','asr')))
                    ORDER BY CASE json_extract(value,'$.state') WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,
                        json_extract(value,'$.created') DESC LIMIT 1""", (project_id, source[0])).fetchone()
                if row:
                    recognition = dict(zip(job_fields, row))
        jobs = [dict(zip(job_fields,row)) for row in job_rows]
        active_jobs = [dict(zip(job_fields,row)) for row in active_rows]
        job_updates = [dict(zip(job_fields,row)) for row in changed_rows]
        for item in jobs + active_jobs + job_updates + ([recognition] if recognition else []):
            if item.get('project_applied') is not None:
                item['project_applied'] = bool(item['project_applied'])
            if isinstance(item.get('stage_journal'), str):
                try:
                    item['stage_journal'] = json.loads(item['stage_journal'])
                except (ValueError, TypeError):
                    item['stage_journal'] = None
        try:
            from hardware_admission import hardware_info
            hardware = hardware_info()
        except ImportError:
            hardware = None
        return {'projects':[dict(id=r[0],name=r[1],updated=r[2],duration=r[3],count=r[4],archived=bool(r[5])) for r in records],
                'jobs':jobs, 'active_jobs':active_jobs, 'job_updates':job_updates, 'recognition':recognition, 'version':'0.8.5-rc6', 'now':moment,
                'queue_health':s.queue_status() | {'queued_count': queued_count}, 'hardware': hardware}

    @app.get('/api/studio/preferences')
    def preferences():
        from studio_preferences import snapshot
        return snapshot()

    @app.patch('/api/studio/preferences')
    @app.put('/api/studio/preferences')
    def update_preferences(body: dict):
        from studio_preferences import update
        with s.state_lock:
            return update(body)

    @app.post('/api/studio/queue')
    def control_queue(body: dict):
        return s.queue_control(body)

    @app.post('/api/studio/queue/reorder')
    def order_queue(body: dict):
        return s.reorder_queue(body)

    @app.get('/api/studio/speech')
    def speech(query: str = '', offset: int = 0, limit: int = 30):
        from speech_library import listing
        return listing(query, offset, limit)

    @app.post('/api/studio/speech/{identifier}/rename')
    def rename_speech(identifier: str, body: dict):
        from speech_library import rename
        with s.state_lock:
            return rename(identifier, body.get('name'))

    @app.post('/api/studio/storage/speech/{identifier}/plan')
    def plan_speech(identifier: str):
        from storage_manager import preview, public_plan
        with s.state_lock:
            return public_plan(preview('speech', identifier))

    @app.get('/api/studio/projects/{identifier}/backup')
    def project_backup(identifier: str):
        from project_transfer import export_document
        return export_document(s.project_by_id(identifier))

    @app.post('/api/studio/restore-project')
    def restore_project(body: dict):
        from project_transfer import restore_document
        with s.state_lock:
            return restore_document(body.get('document'),body.get('source',''))

    @app.get('/api/studio/projects/{identifier}/bundle')
    def project_bundle(identifier: str, source: bool = True, audio: bool = True, final: bool = False):
        from project_bundle import export_bundle, cleanup_export
        with s.state_lock:
            project = copy.deepcopy(s.project_by_id(identifier))
        path = export_bundle(project, source, audio, final)
        return FileResponse(path, media_type='application/zip', filename='NPK-du-an.npk.zip',
                            background=BackgroundTask(cleanup_export, path))

    @app.post('/api/studio/restore-bundle')
    def restore_bundle(file: UploadFile = File(...), source: str = Form('')):
        from project_bundle import import_bundle, cleanup_export, MAX_EXPANDED, RESERVE
        from storage_manager import owned
        directory = owned('bundles/uploads')
        directory.mkdir(parents=True, exist_ok=True)
        path = directory/(uuid.uuid4().hex + '.upload')
        try:
            total = 0
            with path.open('xb') as output:
                for block in iter(lambda: file.file.read(1024 * 1024), b''):
                    total += len(block)
                    if total > MAX_EXPANDED or shutil.disk_usage(DATA).free < len(block) + RESERVE:
                        raise ValueError('Gói tải lên quá lớn hoặc ổ dữ liệu không đủ chỗ.')
                    output.write(block)
            with s.state_lock:
                return import_bundle(path, source)
        finally:
            file.file.close()
            cleanup_export(path)

    @app.get('/api/studio/diagnostics')
    def diagnostics():
        import platform
        import psutil
        # Deliberately omit free-form logs, file names, paths, texts, provider
        # responses and settings: redaction regexes alone are not sufficient.
        return {'application':'NPK Auto Sub','version':'0.8.5-rc6','os':platform.system(),
            'os_release':platform.release(),'python':platform.python_version(),
            'cpu_logical':psutil.cpu_count(),'memory_bytes':psutil.virtual_memory().total,
            'disk_free_bytes':shutil.disk_usage(DATA).free,
            'jobs':[{k:j.get(k) for k in ('kind','state','created','ended')} for j in all_items('job')[:30]],
            'privacy':'Không gồm khóa, log tự do, đường dẫn, tên dự án, nội dung phụ đề hay media.'}

    @app.get('/api/studio/projects/{identifier}')
    def project(identifier: str):
        return s.project_by_id(identifier)

    @app.get('/api/studio/projects/{identifier}/quality')
    def quality(identifier: str):
        project = s.project_by_id(identifier)
        quality = inspect_subtitles(project)
        from auto_subtitle_director import automation_status
        quality['automation'] = automation_status(project)
        if project.get('segments') and project['params'].get('voice_gain', 0) > 0 and not project['params'].get('voice_path'):
            from speech_preparation import inspect_cached_delivery
            try:
                report = inspect_cached_delivery(project)
                quality['delivery'] = report
                for issue in report['issues']:
                    quality['issues'].append(issue | {'severity': 'error', 'code': 'speech_timing_constraint'})
                    quality['errors'].append(issue['message'])
            except ValueError as error:
                quality['delivery_error'] = str(error)
        return quality

    def _ocr_track_evidence(project,requested_job=None):
        from ocr_tracks import read_manifest
        from storage_manager import owned,ID
        candidates=[row for row in all_items('job') if row.get('project_id')==project['id'] and
            row.get('source_revision')==project.get('source_revision') and row.get('kind') in ('ocr','pipeline','auto_subtitles') and
            row.get('state') in ('done','review','error','cancelled')]
        if requested_job is not None:
            if not isinstance(requested_job,str) or not ID.fullmatch(requested_job):
                raise ValueError('Mã lượt nhận dạng lớp chữ không hợp lệ.')
            candidates=[row for row in candidates if row['id']==requested_job]
        last_error='Lượt nhận dạng cũ chưa lưu lớp chữ. Nhận dạng lại để chọn riêng phụ đề trên hình.'
        for row in candidates:
            if not ID.fullmatch(str(row.get('id',''))):
                continue
            path=owned('jobs/'+row['id']+'/ocr-tracks.json')
            if not path.is_file():
                continue
            try:
                document=read_manifest(path,project)
            except ValueError as error:
                last_error=str(error)
                continue
            from subtitle_quality import digest
            return row,document,digest(document)
        raise ValueError(last_error)

    @app.get('/api/studio/projects/{identifier}/ocr-tracks')
    def ocr_tracks(identifier: str,job_id: str|None=None):
        with s.state_lock:
            project=s.project_by_id(identifier)
            try:
                row,document,key=_ocr_track_evidence(project,job_id)
            except ValueError as error:
                return {'available':False,'reason':str(error),'expected_revision':project['updated']}
            current_selection=project.get('coverage',{}).get('ocr_selection') or {}
            selection=(current_selection if current_selection.get('job_id')==row['id'] and
                       current_selection.get('manifest_key')==key else document.get('selection',{}))
            selected=selection.get('selected_tracks',[])
            summaries=[]
            for track in document['tracks']:
                runs=track['runs']
                summaries.append({'id':track['id'],'bbox':track['bbox'],'lines':track['max_lines'],
                    'run_count':len(runs),'start':runs[0]['start'],'end':runs[-1]['end'],
                    'selected':track['id'] in selected,
                    'preview':[{'start':run['start'],'end':run['end'],'text':run['text']}
                               for run in (runs if len(runs)<=3 else runs[:2]+runs[-1:])],
                    'word_count':sum(len(run['text'].split()) for run in runs)})
            return {'available':bool(summaries),'expected_revision':project['updated'],'job_id':row['id'],
                'source_revision':project.get('source_revision'),'source_signature':document['source_signature'],
                'manifest_key':key,'tracks':summaries,'selection':selection,
                'legacy_prefix_rows':document.get('legacy_prefix_rows',0)}

    @app.post('/api/studio/projects/{identifier}/ocr-tracks')
    def select_ocr_tracks(identifier: str,body: dict):
        from ocr_tracks import rows_from_manifest
        from subtitle_quality import source_signature
        with s.state_lock:
            project=s.project_by_id(identifier)
            expected=body.get('expected_revision')
            if (type(expected) not in (int,float) or not math.isfinite(expected) or expected!=project['updated'] or
                    body.get('source_revision')!=project.get('source_revision') or
                    body.get('source_signature')!=source_signature(project)):
                raise HTTPException(409,'Dự án hoặc nguồn đã thay đổi; nạp lại lớp chữ trước khi chọn.')
            if any(row.get('project_id')==identifier and row.get('state') in ('queued','running') and
                   row.get('kind') in ('ocr','asr','translate','tts','clean','pipeline','render','auto_subtitles') for row in all_items('job')):
                raise HTTPException(409,'Chờ hoặc hủy tác vụ đang chạy trước khi thay lớp nội dung phụ đề.')
            try:
                row,document,key=_ocr_track_evidence(project,body.get('job_id'))
                if body.get('manifest_key')!=key:
                    raise HTTPException(409,'Bằng chứng lớp chữ đã thay đổi; nạp lại trước khi chọn.')
                rows=rows_from_manifest(document,project,body.get('track_ids'))
                if not rows:
                    raise ValueError('Lớp chữ đã chọn không có nội dung có thể đưa vào bảng.')
                # Retain a user's translation/voice/style only for an exactly
                # unchanged source row and bounds; other edits remain in history.
                originals={}
                for old in project.get('segments',[]):
                    originals.setdefault((old['start'],old['end'],old['text']),[]).append(old)
                for index,item in enumerate(rows):
                    matches=originals.get((item['start'],item['end'],item['text']),[])
                    if len(matches)==1:
                        rows[index]=copy.deepcopy(matches[0])
                rows=s.validate_segments(rows,project['media']['duration'])
            except (TypeError,ValueError,KeyError) as error:
                raise HTTPException(400,str(error)) from error
            before=copy.deepcopy(project)
            selected=list(body['track_ids'])
            legacy=document.get('legacy_prefix_rows',0)
            issues=([{'code':'legacy_untracked_prefix','severity':'review'}] if legacy else [])
            issues.extend({'code':'overlapping_text_variants','severity':'review','track_id':track['id'],
                           'count':track['overlap_conflicts']} for track in document['tracks']
                          if track['id'] in selected and track.get('overlap_conflicts'))
            selection={'version':document['version'],'mode':'user_selected_tracks','selected_tracks':selected,
                'requires_review':bool(issues),'issues':issues,'job_id':row['id'],'manifest_key':key,
                'selected_at':time.time(),'raw_observation_file':'ocr-observations.jsonl','track_file':'ocr-tracks.json'}
            project['segments']=rows
            project['coverage']=coverage_record(project,'ocr',document['range'][0],document['range'][1],
                sampling_interval=document.get('sampling_interval'),clipped_samples=document.get('clipped_samples',[]),
                ocr_selection=selection)
            project.pop('subtitle_review',None)
            from speech_word_alignment import prune_stale_timings
            prune_stale_timings(project)
            # Signatures, rather than destructive artifact deletion, make old
            # audio/finals stale. Clean video and version history remain usable.
            project['updated']=max(time.time(),before['updated']+.000001)
            return save_project_version(project,before)

    @app.get('/api/studio/projects/{identifier}/dialogue-review')
    def dialogue_review(identifier: str):
        """Local proposals only: no provider, inference or project mutation."""
        from dialogue_review import build_dialogue_review
        with s.state_lock:
            return build_dialogue_review(s.project_by_id(identifier))

    @app.post('/api/studio/projects/{identifier}/dialogue-review/apply')
    def apply_dialogue_review(identifier: str, body: dict):
        from dialogue_review import apply_dialogue_review as apply_proposals
        with s.state_lock:
            project = s.project_by_id(identifier)
            expected = body.get('expected_revision')
            if (type(expected) not in (int, float) or not math.isfinite(expected) or
                    expected != project['updated']):
                raise HTTPException(409, 'Dự án đã thay đổi; kiểm tra lại lời đọc trước khi áp dụng.')
            if any(row.get('project_id') == identifier and row.get('state') in ('queued', 'running')
                   for row in all_items('job')):
                raise HTTPException(409, 'Chờ hoặc hủy tác vụ của video trước khi đổi nội dung lồng tiếng.')
            revised = apply_proposals(project, body.get('signature'), body.get('proposal_ids'))
            # The ordinary editor path validates rows, records an undoable
            # revision and invalidates derived speech timings by identity.
            return s.update_project(identifier, {'expected_revision': expected,
                                                 'segments': revised['segments']})

    @app.post('/api/studio/projects/{identifier}/caption-layout')
    def caption_layout(identifier: str, body: dict):
        """Read-only draft layout using the exact page planner used by ASS export."""
        from subtitle_flow import caption_chunks
        from subtitle_quality import digest
        from speech_word_alignment import prune_stale_timings
        with s.state_lock:
            draft = copy.deepcopy(s.project_by_id(identifier))
            if body.get('expected_revision') != draft['updated']:
                raise HTTPException(409, 'Dự án đã thay đổi. Nạp lại bố cục phụ đề hiện tại.')
        try:
            if 'params' in body:
                draft['params'].update(s.validate_params(body['params']))
            if 'segments' in body:
                draft['segments'] = s.validate_segments(body['segments'], draft['media']['duration'])
            prune_stale_timings(draft)
            cues = []
            for index, row in enumerate(draft['segments']):
                params = draft['params'] | row.get('subtitle_style', {})
                text = str(row.get('translated') or row.get('text', '')).strip()
                chunks = caption_chunks(row, text, params, draft['media'])
                cues.append({'index': index,
                    'source': chunks[0]['word_timing_source'] if chunks else 'estimated', 'chunks': chunks})
            return {'project_id': identifier, 'layout_key': digest([draft['media'], draft['params'], draft['segments']]),
                    'cues': cues}
        except (TypeError, ValueError, KeyError) as error:
            raise HTTPException(400, str(error)) from error

    @app.get('/api/studio/projects/{identifier}/export-plan')
    def plan(identifier: str):
        return export_plan(s.project_by_id(identifier))

    @app.get('/api/studio/projects/{identifier}/artifacts-status')
    def artifacts_status(identifier: str):
        from subtitle_quality import clean_ready, speech_ready
        from core import signature
        p=s.project_by_id(identifier)
        return {'clean_ready':clean_ready(p),'speech_ready':speech_ready(p),
                'preview_current':p.get('artifacts',{}).get('preview',{}).get('signature')==signature(p),
                'clip_current':p.get('artifacts',{}).get('clip',{}).get('signature')==signature(p)}

    @app.get('/api/studio/projects/{identifier}/final-result')
    def final_result(identifier: str):
        from artifact_resolver import resolve_final
        with s.state_lock:
            project = s.project_by_id(identifier)
            return resolve_final(project, all_items('job'), DATA) | {'expected_revision': project['updated']}

    @app.get('/api/studio/projects/{identifier}/audio-insights')
    def current_audio_insights(identifier: str):
        from audio_insights import insights_signature
        from storage_manager import owned, ID
        project = s.project_by_id(identifier)
        artifact = project.get('artifacts', {}).get('analyze_audio') or {}
        current_signature = insights_signature(project)
        if artifact.get('signature') != current_signature or not ID.fullmatch(str(artifact.get('job_id', ''))):
            return {'ready':False}
        path = owned('jobs/' + artifact['job_id'] + '/audio-insights.json')
        if not path.is_file() or path.stat().st_size > 16 * 1024**2:
            return {'ready':False}
        try:
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return {'ready':False}
        if not isinstance(document, dict) or document.get('signature') != current_signature or document.get('schema_version') != 1:
            return {'ready':False}
        return {'ready':True, 'insights':document}

    @app.get('/api/studio/jobs/{identifier}/speaker-groups')
    def current_speaker_groups(identifier: str):
        from speaker_groups import speaker_groups_signature
        from storage_manager import owned, ID
        job = get('job', identifier) if ID.fullmatch(identifier) else None
        if not job or job.get('kind') != 'speaker_groups' or job.get('state') != 'done' or job.get('storage_removed'):
            return {'ready':False}
        project = s.project_by_id(job['project_id'])
        result = job.get('result') or {}
        indices = result.get('segment_indices')
        if not isinstance(indices, list):
            return {'ready':False}
        try:
            current_signature = speaker_groups_signature(project, indices)
            if result.get('source_signature') != current_signature:
                return {'ready':False}
            path = owned('jobs/' + identifier + '/speaker-groups.json')
            if not path.is_file() or path.stat().st_size > 2 * 1024**2:
                return {'ready':False}
            document = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return {'ready':False}
        if not isinstance(document, dict) or document.get('source_signature') != current_signature or document.get('schema') != 1:
            return {'ready':False}
        return {'ready':True, 'result':document}

    def separation_review(identifier):
        """Review only verified, current stems within their original owned job."""
        from audio_separation import separation_signature, source_signature, MODEL_SHA256
        from storage_manager import owned, ID
        row = get('job', identifier) if ID.fullmatch(identifier) else None
        if not row or row.get('kind') != 'separate_audio' or row.get('state') != 'done' or row.get('storage_removed'):
            return None
        project_id = row.get('project_id', '')
        project = get('project', project_id) if isinstance(project_id, str) and ID.fullmatch(project_id) else None
        result = row.get('result') or {}
        required = {'instrumentals.wav', 'vocals.wav', 'audio-separation.json'}
        files = result.get('files')
        if not project or not result.get('verification') or not isinstance(files, list) or \
                any(not isinstance(name,str) for name in files) or not required.issubset(files):
            return None
        if row.get('source_revision') != project.get('source_revision'):
            return None
        try:
            fingerprint, original = separation_signature(project), source_signature(project)
            if result.get('signature') != fingerprint or result.get('source_signature') != original:
                return None
            report_path = owned('jobs/' + identifier + '/audio-separation.json')
            if not report_path.is_file() or report_path.stat().st_size > 128 * 1024:
                return None
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if not isinstance(report, dict) or report.get('schema_version') != 1 or report.get('signature') != fingerprint or \
                    report.get('source_signature') != original or report.get('model_sha256') != MODEL_SHA256:
                return None
            frames = report.get('frames')
            if type(frames) is not int or not 0 < frames <= 44100 * 3 * 3600:
                return None
            import math
            duration = float(report.get('duration',0))
            if not math.isfinite(duration) or report.get('sample_rate') != 44100 or \
                    abs(frames / 44100 - float(project['media']['duration'])) > .35 or abs(frames / 44100 - duration) > 1 / 44100:
                return None
            stems = {name:owned('jobs/' + identifier + '/' + name) for name in ('instrumentals.wav','vocals.wav')}
            # Current separator writes a fixed 44-byte float32 stereo WAV header.
            # Reject missing/truncated stems without decoding hours of audio.
            if any(not path.is_file() or path.stat().st_size != 44 + frames * 8 for path in stems.values()):
                return None
            import struct
            header = struct.pack('<4sI4s4sIHHIIHH4sI',b'RIFF',36+frames*8,b'WAVE',b'fmt ',16,
                                 3,2,44100,44100*8,8,32,b'data',frames*8)
            for path in stems.values():
                with path.open('rb') as source:
                    if source.read(44) != header:
                        return None
        except (OSError, ValueError, TypeError):
            return None
        return row, project, report, stems

    @app.get('/api/studio/jobs/{identifier}/audio-separation')
    def current_audio_separation(identifier: str):
        with s.state_lock:
            checked = separation_review(identifier)
            if checked is None:
                return {'ready':False}
            row, project, report, stems = checked
            return {'ready':True, 'separation':report, 'files':list(stems), 'job_id':row['id'], 'project_id':project['id']}

    @app.post('/api/studio/jobs/{identifier}/audio-separation/apply')
    def apply_audio_separation(identifier: str, body: dict):
        if set(body) - {'expected_revision'}:
            raise ValueError('Chỉ nhận phiên bản dự án để áp dụng bản nhạc đã duyệt.')
        with s.state_lock:
            checked = separation_review(identifier)
            if checked is None:
                raise HTTPException(409,'Bản tách nhạc đã cũ, thiếu tệp hoặc đã dọn. Tách lại trên video hiện tại trước khi dùng.')
            row, project, report, stems = checked
            if body.get('expected_revision') != project['updated']:
                raise HTTPException(409,'Dự án đã thay đổi. Tải lại trước khi dùng bản nhạc đã tách.')
            before = copy.deepcopy(project)
            project['params'].update(bgm_path=str(stems['instrumentals.wav']), original_gain=0., bgm_gain=1.)
            project['updated'] = time.time()
            return save_project_version(project,before)

    @app.post('/api/studio/projects/{identifier}/live-clean-frame')
    def live_clean_frame(identifier: str, body: dict):
        from live_clean_preview import snapshot, render_frame
        request_id = body.get('request_id')
        if request_id is not None:
            try:
                if str(uuid.UUID(request_id)) != request_id:
                    raise ValueError()
            except (ValueError, TypeError, AttributeError):
                raise ValueError('Mã yêu cầu xem thử không hợp lệ.') from None
        with s.state_lock:
            project = s.project_by_id(identifier)
            spec = snapshot(project,body,s.validate_params,s.validate_regions,s.number)
        # Live previews do not compete with an active production worker for GPU/RAM.
        with s.active_lock:
            if s.active_process is not None and s.active_process.poll() is None:
                raise HTTPException(409,'Đang có tác vụ xử lý video. Xem phục hồi trực tiếp sẽ dùng được khi tác vụ kết thúc.')
        return Response(render_frame(spec, admission_lock=s.active_lock,
            busy=lambda: s.stop_event.is_set() or (s.active_process is not None and s.active_process.poll() is None), request_id=request_id),
            media_type='image/jpeg')

    @app.post('/api/studio/live-clean/cancel')
    def cancel_live_clean(body: dict):
        from live_clean_preview import cancel_active
        request_id = body.get('request_id')
        try:
            if not isinstance(request_id, str) or str(uuid.UUID(request_id)) != request_id:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Mã yêu cầu xem thử không hợp lệ.') from None
        cancel_active(request_id)
        return {'cancelled': True}

    @app.post('/api/studio/projects/{identifier}/review')
    def review(identifier: str, body: dict):
        with s.state_lock:
            project = s.project_by_id(identifier)
            if body.get('signature') != review_signature(project):
                raise HTTPException(409, 'Phụ đề đã thay đổi. Kiểm lại trước khi xác nhận.')
            reason = str(body.get('reason','')).strip()
            if len(reason)<5:
                raise ValueError('Ghi lý do đã kiểm tra: ví dụ phần còn lại là nhạc, không có lời.')
            before = copy.deepcopy(project)
            project['subtitle_review'] = {'signature':review_signature(project),'reason':reason[:1000],'at':time.time()}
            project['updated'] = time.time()
            return save_project_version(project,before)

    @app.post('/api/studio/projects/{identifier}/import')
    def import_text(identifier: str, body: dict):
        with s.state_lock:
            project = s.import_subtitles(identifier,body)
            before = copy.deepcopy(project)
            if body.get('target_language'):
                for row in project['segments']:
                    row['translated'] = row['text']
            project['coverage'] = coverage_record(project,'import',0,0,note='Tệp nhập chưa chứng minh đã khớp toàn video.')
            project['updated'] = time.time()
            return save_project_version(project,before)

    @app.post('/api/studio/projects/{identifier}/duplicate')
    def duplicate(identifier: str):
        with s.state_lock:
            project = copy.deepcopy(s.project_by_id(identifier))
            # Shared media remains readable by an explicit project copy. This
            # provenance is server-owned; source replacement clears artifacts.
            for artifact in project.get('artifacts', {}).values():
                if isinstance(artifact, dict) and artifact.get('job_id'):
                    artifact.setdefault('source_project_id', identifier)
            project.update(id=uuid.uuid4().hex, name=project['name']+' — bản sao', created=time.time(), updated=time.time(),archived=False)
            return save_project_version(project)

    @app.post('/api/studio/projects/{identifier}/archive')
    def archive(identifier: str, body: dict):
        with s.state_lock:
            project = s.project_by_id(identifier)
            before = copy.deepcopy(project)
            project.update(archived=bool(body.get('archived',True)),updated=time.time())
            return save_project_version(project,before)

    @app.get('/api/studio/projects/{identifier}/revisions')
    def revisions(identifier: str):
        s.project_by_id(identifier)
        return [{'updated':p['updated'],'name':p['name'],'count':len(p.get('segments',[]))}
                for p in all_items('project_revision') if p['id']==identifier][:30]

    @app.post('/api/studio/projects/{identifier}/restore')
    def restore(identifier: str, body: dict):
        with s.state_lock:
            current = s.project_by_id(identifier)
            if body.get('expected_revision') != current['updated']:
                raise HTTPException(409,'Dự án đã đổi. Tải lại trước khi khôi phục.')
            previous = next((p for p in all_items('project_revision') if p['id']==identifier and p['updated']==body.get('revision')),None)
            if not previous:
                raise ValueError('Không tìm thấy phiên bản đã chọn.')
            previous['updated'] = time.time()
            return save_project_version(previous,current)

    @app.post('/api/studio/projects/{identifier}/relink')
    def relink(identifier: str, body: dict):
        return replace_source(identifier, {**body, 'mode':'keep'})

    @app.post('/api/studio/projects/{identifier}/replace-source')
    def replace_source(identifier: str, body: dict):
        mode = body.get('mode', 'reset')
        if mode not in ('reset', 'keep'):
            raise ValueError('Chọn cách thay video nguồn hợp lệ.')
        media = probe(body.get('path',''))
        with s.state_lock:
            project = s.project_by_id(identifier)
            if body.get('expected_revision') != project['updated']:
                raise HTTPException(409,'Dự án đã đổi. Tải lại trước khi chọn nguồn.')
            if any(j.get('project_id') == identifier and
                   (j.get('state') in ('queued','running') or j['id'] == s.active_id)
                   for j in all_items('job')):
                raise HTTPException(409,'Dự án đang có tác vụ. Chờ hoàn tất hoặc hủy tác vụ trước khi thay video nguồn.')
            if mode == 'keep':
                if abs(media['duration']-project['media']['duration'])>.2 or (media['width'],media['height'])!=(project['media']['width'],project['media']['height']):
                    raise ValueError('Video khác thời lượng/kích thước. Chọn “Bắt đầu lại nội dung” để thay video này.')
                s.validate_segments(project.get('segments', []), media['duration'])
            before = copy.deepcopy(project)
            project.update(media=media, updated=time.time(), artifacts={}, source_revision=uuid.uuid4().hex)
            project.pop('coverage', None)
            project.pop('subtitle_review', None)
            if mode == 'reset':
                project.update(segments=[], regions=[], extraction_regions=[])
                project['params'].update(extraction_start=0, extraction_end=0, voice_path='', bgm_path='',
                    voice_offset=0, use_clean=False, pipeline_clean=False, pipeline_extract='existing',
                    mask_source='manual', translation_scope='missing', original_gain=1, voice_gain=0,
                    pipeline_tts=False)
            return save_project_version(project,before)

    def latest_pre_render_review(project):
        return max((row for row in all_items('job') if row.get('project_id') == project['id'] and
            row.get('source_revision') == project.get('source_revision') and row.get('kind') == 'pipeline' and
            row.get('state') == 'review' and (row.get('result') or {}).get('review_stage') == 'pre_render'),
            key=lambda row: row.get('created', 0), default=None)

    def pre_render_plan(project):
        from pipeline_resume import pre_render_review_status, pre_render_continuation_status
        row = latest_pre_render_review(project)
        if row:
            plan = pre_render_continuation_status(row, project, all_items('job'), DATA/'jobs'/row['id'])
        else:
            plan = pre_render_review_status(project) | {'can_approve': False, 'can_continue': False,
                'completed_steps': [], 'errors': [], 'warnings': [], 'next_action': 'prepare_before_review',
                'reason': 'Chạy chuẩn bị phụ đề, lời đọc và xử lý hình trước khi xem và duyệt bản xuất.'}
        # The review must show the same selected/cleaned narration the worker
        # signs and caches, not a JavaScript approximation of raw captions.
        # This is a pure derivation: displayed captions and authored roles stay
        # intact, and no inference, credential access or provider is needed.
        from speech_delivery import effective_speech_rows, spoken_text
        try:
            narration = effective_speech_rows(project.get('segments', []), project['params'])
            plan['narration'] = []
            for index, cue in enumerate(narration):
                text = spoken_text(cue, project['params'])
                plan['narration'].append({'index': index, 'text': text,
                    'enabled': cue.get('speech_enabled', True) is not False and bool(text)})
        except (ValueError, TypeError, KeyError) as error:
            plan.update(narration=[], can_approve=False, can_continue=False, requires_review=True,
                        reason=str(error))
            plan['errors'] = list(dict.fromkeys(plan.get('errors', []) + [str(error)]))
        return plan

    @app.get('/api/studio/projects/{identifier}/pre-render-review')
    def pre_render_review(identifier: str):
        with s.state_lock:
            return pre_render_plan(s.project_by_id(identifier))

    @app.post('/api/studio/projects/{identifier}/pre-render-review')
    def approve_pre_render_review(identifier: str, body: dict):
        """Record an explicit user decision; this endpoint never queues work."""
        with s.state_lock:
            s.require_owner_instance(body)
            project = s.project_by_id(identifier)
            if body.get('approved') is not True or body.get('automatic') is True:
                raise HTTPException(400, 'Cần thao tác duyệt rõ ràng sau khi đã xem và nghe kết quả.')
            if body.get('expected_revision') != project['updated']:
                raise HTTPException(409, 'Dự án đã thay đổi; tải lại và kiểm tra kết quả hiện tại trước khi duyệt.')
            plan = pre_render_plan(project)
            if body.get('signature') != plan['signature'] or body.get('job_id') != plan.get('job_id'):
                raise HTTPException(409, 'Kết quả chuẩn bị đã thay đổi; xem và duyệt đúng phiên bản hiện tại.')
            if not plan.get('enabled') or not plan.get('can_approve'):
                raise HTTPException(409, plan['reason'])
            before = copy.deepcopy(project)
            project['pre_render_review'] = {'schema_version': 1, 'approved': True,
                'signature': plan['signature'], 'job_id': plan['job_id'], 'approved_at': time.time()}
            project['updated'] = max(time.time(), before['updated'] + .000001)
            saved = save_project_version(project, before)
            return pre_render_plan(saved) | {'project': saved}

    def continuation_plan(row, project):
        from pipeline_resume import continuation_status
        jobs = all_items('job')
        plan = continuation_status(row, project, jobs, DATA/'jobs'/row['id'] if row else DATA/'jobs')
        content_ready = bool(plan.get('can_continue'))
        from execution_admission import admission_status
        admission = admission_status(project, jobs, kind='pipeline', remaining_steps=plan.get('remaining_steps'))
        if row and content_ready and 'translate' in plan.get('remaining_steps', []):
            try:
                from pipeline_resume import prepare_continuation
                from provider_usage import prepare_continuation_lineage, lineage_status
                snapshot, root, _, selected = prepare_continuation(row, project, jobs, DATA/'jobs'/row['id'])
                budget = lineage_status(prepare_continuation_lineage(snapshot, 'pipeline', root, jobs,
                    jobs_root=DATA/'jobs', segment_indices=selected), jobs_root=DATA/'jobs')
                plan['request_budget'] = budget
                if admission['execution_allowed'] and not budget['execution_allowed']:
                    admission.update(execution_allowed=False, blocked_code=budget['blocked_code'],
                                     blocked_reason=budget['blocked_reason'])
            except (ValueError, OSError, KeyError, TypeError):
                if admission['execution_allowed']:
                    admission.update(execution_allowed=False, blocked_code='invalid_usage_history',
                        blocked_reason='Chưa xác minh được lịch sử lượt gọi API của quy trình; cần kiểm tra trước khi chạy.')
        plan.update(admission, content_ready=content_ready,
                    can_continue=content_ready and admission['execution_allowed'])
        if content_ready and not admission['execution_allowed']:
            plan.update(reason=admission['blocked_reason'], next_action='inspect_execution_admission')
        return plan

    @app.get('/api/studio/projects/{identifier}/pipeline-continuation')
    def project_continuation(identifier: str):
        with s.state_lock:
            project = s.project_by_id(identifier)
            jobs = all_items('job')
            completed = {row.get('continuation_from') for row in jobs if row.get('project_id') == identifier and
                         row.get('source_revision') == project.get('source_revision') and row.get('state') == 'done' and
                         row.get('result', {}).get('primary_file') == 'final.mp4'}
            final_created = max((row.get('created', 0) for row in jobs if row.get('project_id') == identifier and
                                 row.get('source_revision') == project.get('source_revision') and
                                 row.get('state') == 'done' and row.get('result', {}).get('primary_file') == 'final.mp4'), default=-1)
            candidates = [row for row in jobs if row.get('id') not in completed and row.get('project_id') == identifier and
                          row.get('source_revision') == project.get('source_revision') and
                          row.get('created', 0) > final_created and row.get('kind') == 'pipeline' and
                          row.get('state') in ('review', 'error', 'cancelled')]
            row = max(candidates, key=lambda item: item.get('created', 0), default=None)
            return continuation_plan(row, project)

    @app.get('/api/studio/jobs/{identifier}/continuation')
    def job_continuation(identifier: str):
        with s.state_lock:
            row = get('job', identifier)
            if not row:
                raise HTTPException(404, 'Không tìm thấy tác vụ.')
            return continuation_plan(row, s.project_by_id(row['project_id']))

    @app.post('/api/studio/jobs/{identifier}/continue-pipeline')
    def continue_pipeline(identifier: str, body: dict):
        with s.state_lock:
            s.require_owner_instance(body)
            row = get('job', identifier)
            if not row:
                raise HTTPException(404, 'Không tìm thấy tác vụ.')
            project = s.project_by_id(row['project_id'])
            if body.get('expected_revision') != project['updated']:
                raise HTTPException(409, 'Dự án đã thay đổi; lưu và tải lại trước khi tiếp tục.')
            # Repeated clicks return the same successor, never another export.
            jobs = all_items('job')
            root_identifier = row.get('continuation_from') or identifier
            previous = next((item for item in jobs if item.get('continuation_from') == root_identifier and
                             item.get('state') in ('queued', 'running') and
                             item.get('project_revision') == project['updated'] and
                             item.get('source_revision') == project.get('source_revision')), None)
            if previous:
                return previous
            plan = continuation_plan(row, project)
            if not plan['can_continue']:
                raise HTTPException(400 if plan['requires_review'] or plan['errors'] else 409, plan['reason'])
            from pipeline_resume import prepare_continuation
            snapshot, root_id, recovered_id, selected = prepare_continuation(row, project, jobs, DATA/'jobs'/identifier)
            validated = s.validate_segments(snapshot['segments'], snapshot['media']['duration'])
            if plan.get('review_stage') != 'pre_render':
                snapshot['segments'] = validated
            # A reviewed final-render snapshot must remain byte-for-byte the
            # semantic table the user approved, including track IDs and other
            # metadata that the generic editor's whitelist does not serialize.
            from execution_admission import require_admission
            require_admission(snapshot, jobs, kind='pipeline', automatic=body.get('automatic') is True)
            request = {'kind': 'pipeline', 'project_id': project['id'],
                                 'expected_revision': project['updated'], 'replace_segments': True,
                                 'request_id': body.get('request_id', '')}
            if body.get('automatic') is True:
                request['automatic'] = True
            if 'overwrite_user_edits' in body:
                request['overwrite_user_edits'] = body['overwrite_user_edits']
            if selected is not None:
                request['segment_indices'] = selected
            return s.create_job(request, project_override=snapshot, continuation_from=root_id, recovery_from=recovered_id)

    @app.get('/api/studio/jobs/{identifier}')
    def job(identifier: str):
        row = get('job',identifier)
        if not row:
            raise HTTPException(404)
        row['can_resume'] = False
        if row['kind'] in ('asr', 'pipeline') and row['state'] in ('error', 'cancelled'):
            method = row['kind'] if row['kind'] == 'asr' else row.get('configuration', {}).get('pipeline_extract')
            if method in ('ocr', 'asr'):
                try:
                    if method == 'ocr':
                        from ocr_checkpoint import validate_resume
                        filename = 'recognition-checkpoint.json'
                    else:
                        from asr_checkpoint import validate_resume
                        filename = 'asr-checkpoint.json'
                    validate_resume(DATA/'jobs'/identifier/filename, s.project_by_id(row['project_id']))
                    row['can_resume'] = True
                except (ValueError, OSError, HTTPException):
                    pass
        if row['kind'] == 'ocr' and row['state'] in ('error', 'cancelled'):
            try:
                from ocr_checkpoint import validate_resume
                checkpoint = s.safe_owned(DATA/'jobs'/identifier/'recognition-checkpoint.json', DATA/'jobs')
                validate_resume(checkpoint, s.project_by_id(row['project_id']))
                row['can_resume'] = True
            except (ValueError, OSError, HTTPException):
                pass
        if row['kind'] in ('translate', 'pipeline') and row['state'] in ('error', 'cancelled'):
            checkpoint = DATA/'jobs'/identifier/'translation-checkpoint.json'
            if checkpoint.is_file():
                try:
                    from translation_checkpoint import read_checkpoint, validate_resume, validate_pipeline_resume
                    project = s.project_by_id(row['project_id'])
                    validate = validate_pipeline_resume if row['kind'] == 'pipeline' and project['params'].get('pipeline_extract') in ('ocr', 'asr') else validate_resume
                    validate(read_checkpoint(checkpoint), project, row.get('segment_indices'),
                             overwrite_user_edits=row.get('overwrite_user_edits') is True)
                    row['can_resume'] = True
                except (ValueError, OSError, HTTPException):
                    row['can_resume'] = False
        row['can_restore_translation'] = False
        row['restorable_translation_count'] = 0
        if row['kind'] in ('translate', 'pipeline') and row['state'] in ('error', 'cancelled'):
            try:
                candidates = translation_restore_candidates(row, s.project_by_id(row['project_id']))
                row['restorable_translation_count'] = len(candidates)
                row['can_restore_translation'] = bool(candidates)
            except (ValueError, OSError, HTTPException) as exc:
                if (DATA/'jobs'/identifier/'translation-checkpoint.json').is_file():
                    row['translation_recovery_error'] = str(exc)
        if row['kind'] == 'translate' and row['state'] in ('error', 'cancelled') and not row['can_resume']:
            try:
                row['retry_segment_indices'] = translation_retry_indices(row, s.project_by_id(row['project_id']))
            except (ValueError, OSError, HTTPException) as exc:
                row['translation_retry_error'] = str(exc)
        if row.get('kind') == 'pipeline' and row.get('state') in ('review', 'error', 'cancelled'):
            with s.state_lock:
                row['continuation'] = continuation_plan(row, s.project_by_id(row['project_id']))
                if row.get('state') in ('error', 'cancelled'):
                    row['can_resume'] = row['can_resume'] or row['continuation']['can_continue']
        if row.get('kind') in ('translate', 'pipeline') and row.get('state') in ('done', 'error', 'cancelled', 'review'):
            from provider_usage import read_summary
            # A stopped job's final durable ledger is newer than its last
            # throttled progress event. Reading diagnostics never rewrites it.
            saved_usage = read_summary(DATA/'jobs'/identifier)
            if saved_usage is not None:
                row['api_usage'] = saved_usage
        return row

    def final_result_path(row):
        from artifact_resolver import final_result_path as resolve_path
        return resolve_path(row, DATA)

    @app.post('/api/studio/jobs/{identifier}/open-result')
    def open_result(identifier: str):
        path = final_result_path(job(identifier))
        os.startfile(str(path))
        return {'opened':True, 'path':str(path)}

    @app.post('/api/studio/jobs/{identifier}/open-result-folder')
    def open_result_folder(identifier: str):
        path = final_result_path(job(identifier))
        os.startfile(str(path.parent))
        return {'opened':True, 'path':str(path), 'directory':str(path.parent)}

    @app.post('/api/studio/jobs/{identifier}/apply')
    def apply_result(identifier: str, body: dict):
        row=job(identifier)
        with s.state_lock:
            project=s.project_by_id(row['project_id'])
            if body.get('expected_revision')!=project['updated']:
                raise HTTPException(409,'Dự án đã thay đổi; tải lại trước khi áp dụng kết quả.')
            if row.get('source_revision') != project.get('source_revision'):
                raise HTTPException(409,'Kết quả thuộc video nguồn trước. Chạy lại tác vụ trên video hiện tại.')
            before=copy.deepcopy(project)
            result=row.get('result',{})
            if row['kind']=='detect' and row['state']=='done':
                return s.update_project(project['id'],{'expected_revision':project['updated'],'regions':result.get('regions',[])})
            if 'segments' in result and row['state'] in ('done','review'):
                project['segments']=s.validate_segments(result['segments'],project['media']['duration'])
                if result.get('coverage'):project['coverage']=result['coverage']
            elif row['state'] in ('error','cancelled'):
                if row['kind'] not in ('translate', 'pipeline'):
                    raise ValueError('Tác vụ này không có checkpoint dịch để khôi phục.')
                candidates = translation_restore_candidates(row, project)
                if not candidates:raise ValueError('Không có câu dịch còn thiếu khớp nội dung hiện tại. Không ghi đè câu đã sửa tay.')
                for index, text in candidates:
                    project['segments'][index]['translated'] = text
            else:
                raise ValueError('Không có bảng phụ đề có thể áp dụng từ tác vụ này.')
            project['updated']=max(time.time(), before['updated'] + .000001)
            return save_project_version(project,before)

    @app.post('/api/studio/jobs/{identifier}/publish')
    def publish(identifier: str, body: dict):
        row = job(identifier)
        if row['state']!='done' or 'final.mp4' not in row.get('result',{}).get('files',[]):
            raise ValueError('Chưa có video thành phẩm đã kiểm tra.')
        params = s.validate_params({'export_directory':body.get('directory',''),'export_name':body.get('name','Video-thanh-pham')})
        from exports import publish_verified
        published = publish_verified({'params':params}, DATA/'jobs'/identifier, unique=False)
        with s.state_lock:
            current = get('job', identifier)
            current.setdefault('result', {})['published'] = published
            current['result'].pop('delivery_pending', None)
            put('job', identifier, current)
            project = get('project', current.get('project_id', ''))
            if project:
                before = copy.deepcopy(project)
                for artifact in project.get('artifacts', {}).values():
                    if isinstance(artifact, dict) and artifact.get('job_id') == identifier:
                        artifact['published'] = published
                        artifact.pop('delivery_pending', None)
                save_project_version(project, before)
        return published

    @app.get('/api/studio/fonts')
    def fonts():
        from subtitle_fonts import installed_fonts
        return sorted(installed_fonts())

    @app.get('/api/studio/storage')
    def storage():
        cache = DATA/'speech-cache'
        files = [p for p in cache.glob('*.wav') if p.is_file() and not p.is_symlink() and p.resolve().parent==cache.resolve()] if cache.is_dir() else []
        from subtitle_quality import digest
        manifest = [[p.name,p.stat().st_size,p.stat().st_mtime_ns] for p in sorted(files)]
        return {'root':str(DATA),'free_bytes':shutil.disk_usage(DATA).free,
                'cache_bytes':sum(x[1] for x in manifest),'cache_files':len(files), 'manifest':manifest,'signature':digest(manifest)}

    @app.get('/api/studio/storage/overview')
    def storage_overview():
        from storage_manager import overview
        return overview()

    @app.get('/api/studio/storage/plan')
    def storage_plan(action: str, project_id: str | None = None):
        from storage_manager import preview, public_plan
        with s.state_lock:
            return public_plan(preview(action, project_id))

    @app.post('/api/studio/storage/execute')
    def storage_execute(body: dict):
        from storage_manager import execute
        with s.state_lock:
            identifier = body.get('speech_id', body.get('project_id')) if body.get('action') == 'speech' else body.get('project_id')
            return execute(body.get('action'), identifier, body)

    @app.post('/api/studio/storage/trash/{identifier}/restore')
    def storage_restore(identifier: str, body: dict):
        from storage_manager import restore
        if body.get('confirmed') is not True:
            raise ValueError('Xác nhận khôi phục trước khi tiếp tục.')
        with s.state_lock:
            return restore(identifier)

    @app.get('/api/studio/storage/trash/{identifier}/purge-plan')
    def storage_purge_plan(identifier: str):
        from storage_manager import purge_preview
        with s.state_lock:
            return purge_preview(identifier)

    @app.post('/api/studio/storage/trash/{identifier}/purge')
    def storage_purge(identifier: str, body: dict):
        from storage_manager import purge
        with s.state_lock:
            return purge(identifier, body)

    @app.post('/api/studio/storage/clear-speech-cache')
    def clear_cache(body: dict):
        raise ValueError('Mở Quản lý dung lượng trong giao diện mới để xem danh sách và dọn có thể khôi phục.')
