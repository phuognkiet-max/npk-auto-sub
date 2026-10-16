"""Versioned translation checkpoints tied to the exact input snapshot."""
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time

VERSION = 2
MAX_CHECKPOINT_BYTES = 32 * 1024 * 1024
MAX_ROWS = 10000
_PARAMS = ('provider', 'translation_model', 'target_language', 'style', 'glossary',
           'translation_scope', 'translation_batch', 'words_per_second')
_EXTRACTION_PARAMS = ('pipeline_extract', 'source_language', 'asr_model', 'asr_device',
                      'asr_compute', 'ocr_fps', 'ocr_confidence', 'ocr_similarity',
                      'ocr_scale_mode', 'extraction_start', 'extraction_end')


def translation_params(project):
    """Bind routed work to its endpoint without changing legacy signatures."""
    params = project['params']
    result = {key: params.get(key) for key in _PARAMS}
    if params.get('provider') == 'router9':
        from providers import router_base_url
        result['router9_base_url'] = router_base_url(params.get('router9_base_url'))
    return result


def translation_signature(project, segment_indices=None):
    from subtitle_quality import source_signature
    media = project.get('media') or {}
    source = source_signature(project) if media.get('path') and 'duration' in media else media
    rows = [{key: row.get(key, '') for key in ('start','end','text','translated','spoken_text','words')}
            for row in project.get('segments', [])]
    if any('user_edited' in row for row in project.get('segments', [])):
        for saved, original in zip(rows, project.get('segments', [])):
            saved['user_edited'] = original.get('user_edited', False)
    value = {'version': VERSION, 'source': source, 'rows': rows,
             'params': translation_params(project),
             'selected': None if segment_indices is None else sorted(set(segment_indices))}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def checkpoint_rows(document):
    rows = document if isinstance(document, list) else document.get('rows') if isinstance(document, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError('Checkpoint dịch không hợp lệ; bản phụ đề hiện tại vẫn được giữ.')
    return rows


def _finite_document(document):
    pending = [(document, 0)]
    count = 0
    while pending:
        value, depth = pending.pop()
        count += 1
        if depth > 24 or count > 1000000:
            raise ValueError('Checkpoint dịch quá phức tạp; giữ nguyên dự án hiện tại.')
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Checkpoint dịch có giá trị thời gian không hợp lệ.')
        if isinstance(value, dict):
            pending.extend((item, depth + 1) for item in value.values())
        elif isinstance(value, list):
            pending.extend((item, depth + 1) for item in value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Checkpoint dịch có trường dữ liệu trùng; không tự áp dụng.')
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError('Checkpoint dịch có giá trị không hữu hạn; không tự áp dụng.')


def _validate_rows(rows):
    if len(rows) > MAX_ROWS:
        raise ValueError('Checkpoint dịch vượt giới hạn số câu.')
    for row in rows:
        start, end = row.get('start'), row.get('end')
        if (type(start) not in (int, float) or type(end) not in (int, float) or
                not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start):
            raise ValueError('Checkpoint dịch có mốc câu không hợp lệ.')
        for key in ('text', 'translated', 'spoken_text'):
            value = row.get(key, '')
            if not isinstance(value, str) or len(value) > 10000 or '\x00' in value:
                raise ValueError('Checkpoint dịch có nội dung câu không hợp lệ.')
        if 'user_edited' in row and type(row['user_edited']) is not bool:
            raise ValueError('Cờ bảo vệ sửa tay trong checkpoint dịch không hợp lệ.')


def connection_identity(project):
    params = project['params']
    identity = {'provider': params.get('provider'), 'model': params.get('translation_model'),
                'target_language': params.get('target_language')}
    if params.get('provider') == 'router9':
        from providers import router_base_url
        identity['endpoint'] = router_base_url(params.get('router9_base_url'))
    return identity


def eligible_translation_indices(project, segment_indices=None, *, overwrite_user_edits=False):
    selected = set(range(len(project.get('segments', [])))) if segment_indices is None else set(segment_indices)
    return [i for i, row in enumerate(project.get('segments', [])) if i in selected and
            (overwrite_user_edits or row.get('user_edited') is not True) and
            (overwrite_user_edits and row.get('user_edited') is True or
             project['params'].get('translation_scope') != 'missing' or not row.get('translated', '').strip())]


def read_checkpoint(path):
    try:
        path = Path(path)
        if path.stat().st_size > MAX_CHECKPOINT_BYTES:
            raise ValueError('Checkpoint dịch quá lớn; không tự nạp dữ liệu.')
        with path.open('rb') as stream:
            content = stream.read(MAX_CHECKPOINT_BYTES + 1)
        if len(content) > MAX_CHECKPOINT_BYTES:
            raise ValueError('Checkpoint dịch quá lớn; không tự nạp dữ liệu.')
        document = json.loads(content.decode('utf-8'), object_pairs_hook=_unique_object,
                              parse_constant=_invalid_constant)
        _finite_document(document)
        _validate_rows(checkpoint_rows(document))
        return document
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError, OverflowError) as exc:
        raise ValueError('Không đọc được checkpoint dịch. Giữ dự án hiện tại và dùng bản checkpoint hợp lệ khác.') from exc


def validate_resume(document, project, segment_indices=None, *, overwrite_user_edits=False):
    rows = checkpoint_rows(document)
    _finite_document(document)
    _validate_rows(rows)
    if not isinstance(document, dict) or document.get('schema_version') != VERSION:
        raise ValueError('Checkpoint cũ chỉ hỗ trợ áp dụng bản dịch dở; hãy áp dụng rồi dịch các câu còn thiếu.')
    if document.get('connection') is not None and document['connection'] != connection_identity(project):
        raise ValueError('Model, nhà cung cấp, endpoint hoặc ngôn ngữ đích của checkpoint khác cấu hình hiện tại. '
                         'Giữ phần đã dịch và chọn rõ cách tiếp tục; không trộn kết quả từ model khác.')
    if (type(document.get('overwrite_user_edits', False)) is not bool or
            document.get('overwrite_user_edits', False) != overwrite_user_edits):
        raise ValueError('Quyền ghi đè sửa tay của checkpoint khác lựa chọn hiện tại; xác nhận lại phạm vi trước khi tiếp tục.')
    if document.get('signature') != translation_signature(project, segment_indices):
        raise ValueError('Checkpoint không khớp video, phụ đề hoặc cấu hình dịch hiện tại. '
                         'Áp dụng phần dịch dở rồi chọn dịch câu còn thiếu; không tự dịch lại toàn bộ.')
    completed = document.get('completed_indices')
    if (not isinstance(completed, list) or len(rows) != len(project.get('segments', [])) or
            any(type(i) is not int or not 0 <= i < len(rows) for i in completed) or
            len(completed) != len(set(completed))):
        raise ValueError('Danh sách câu đã dịch trong checkpoint không hợp lệ.')
    eligible = set(eligible_translation_indices(project, segment_indices,
                                               overwrite_user_edits=overwrite_user_edits))
    if not set(completed).issubset(eligible):
        raise ValueError('Checkpoint chứa câu ngoài phạm vi dịch; không tự áp dụng.')
    for index, (original, saved) in enumerate(zip(project['segments'], rows)):
        if any(saved.get(key, '') != original.get(key, '') for key in ('start','end','text','spoken_text','words')):
            raise ValueError('Nội dung câu trong checkpoint khác snapshot; không tự áp dụng.')
        if saved.get('user_edited', False) != original.get('user_edited', False):
            raise ValueError('Checkpoint thay đổi cờ bảo vệ sửa tay; không tự áp dụng.')
        if index not in completed and saved.get('translated', '') != original.get('translated', ''):
            raise ValueError('Checkpoint thay đổi câu chưa được xác nhận dịch xong.')
        if index in completed and not saved.get('translated','').strip():
            raise ValueError('Checkpoint đánh dấu câu đã dịch nhưng thiếu bản dịch.')
    warnings = document.get('warnings', [])
    if not isinstance(warnings, list) or len(warnings) > MAX_ROWS * 10 or any(not isinstance(w, str) for w in warnings):
        raise ValueError('Danh sách cảnh báo checkpoint dịch không hợp lệ.')
    from translation_entities import validate_entity_glossary
    validate_entity_glossary(document.get('entity_glossary'), rows)
    skipped = document.get('skipped_source_indices', [])
    from subtitle_text import is_translation_noise
    if (not isinstance(skipped, list) or
            any(type(i) is not int or not 0 <= i < len(rows) or
                not is_translation_noise(rows[i].get('text')) or rows[i].get('speech_enabled') is not False
                for i in skipped) or len(set(skipped)) != len(skipped)):
        raise ValueError('Danh sách dòng rác bỏ qua trong checkpoint không hợp lệ.')
    return completed


def validated_partial_rows(document, original_project, segment_indices=None, *, overwrite_user_edits=False):
    """Only restore translations tied to the failed job's original input.

    Current project edits are merged separately by exact source row identity,
    preserving every nonempty manual translation. Legacy lists are readable
    but may not bypass source/timing validation or selected translation scope.
    """
    if isinstance(document, dict):
        if original_project['params'].get('pipeline_extract') in ('ocr', 'asr') and document.get('pipeline_state'):
            recovered = validate_pipeline_resume(document, original_project, segment_indices,
                                                 overwrite_user_edits=overwrite_user_edits)
            validate_resume(document, recovered, segment_indices, overwrite_user_edits=overwrite_user_edits)
        else:
            validate_resume(document, original_project, segment_indices, overwrite_user_edits=overwrite_user_edits)
        return copy.deepcopy(checkpoint_rows(document))
    rows = checkpoint_rows(document)
    _finite_document(rows)
    _validate_rows(rows)
    originals = original_project.get('segments', [])
    if len(rows) != len(originals):
        raise ValueError('Checkpoint dịch cũ không khớp số câu của tác vụ gốc.')
    selected = set(range(len(rows))) if segment_indices is None else set(segment_indices)
    for index, (saved, original) in enumerate(zip(rows, originals)):
        if any(saved.get(key, '') != original.get(key, '') for key in ('start', 'end', 'text', 'spoken_text', 'words')):
            raise ValueError('Checkpoint dịch cũ không khớp phụ đề đầu vào của tác vụ.')
        eligible = index in selected and (overwrite_user_edits or original.get('user_edited') is not True) and (
            overwrite_user_edits and original.get('user_edited') is True or
            original_project['params'].get('translation_scope') != 'missing' or
            not original.get('translated', '').strip())
        if not eligible and saved.get('translated', '') != original.get('translated', ''):
            raise ValueError('Checkpoint dịch cũ có bản dịch ngoài phạm vi tác vụ.')
    return copy.deepcopy(rows)


def pipeline_input_signature(project, segment_indices=None):
    value = {'translation': translation_signature(project, segment_indices),
             'extraction': {key: project['params'].get(key) for key in _EXTRACTION_PARAMS},
             'regions': project.get('regions', []),
             'extraction_regions': project.get('extraction_regions')}
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def make_pipeline_state(initial_project, recognized_project, segment_indices=None):
    kind = initial_project['params'].get('pipeline_extract', 'existing')
    if kind not in ('asr', 'ocr'):
        raise ValueError('Chỉ lưu snapshot nhận dạng cho quy trình OCR/ASR.')
    return {'input_signature': pipeline_input_signature(initial_project, segment_indices),
            'extract_kind': kind,
            'recognition_snapshot': {'segments': copy.deepcopy(recognized_project['segments']),
                                     'coverage': copy.deepcopy(recognized_project.get('coverage', {}))}}


def validate_pipeline_resume(document, initial_project, segment_indices=None, *, overwrite_user_edits=False):
    """Restore only verified recognition input from the same initial pipeline."""
    if not isinstance(document, dict) or document.get('schema_version') != VERSION:
        raise ValueError('Checkpoint cũ không có snapshot nhận dạng để tiếp tục quy trình.')
    state = document.get('pipeline_state')
    kind = initial_project['params'].get('pipeline_extract', 'existing')
    if (not isinstance(state, dict) or kind not in ('asr','ocr') or state.get('extract_kind') != kind or
            state.get('input_signature') != pipeline_input_signature(initial_project, segment_indices)):
        raise ValueError('Video, phạm vi nhận dạng hoặc cấu hình quy trình đã đổi; không tự dùng lại snapshot cũ.')
    snapshot = state.get('recognition_snapshot')
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get('segments'), list):
        raise ValueError('Checkpoint quy trình thiếu bảng nhận dạng hợp lệ.')
    recovered = copy.deepcopy(initial_project)
    recovered['segments'] = copy.deepcopy(snapshot['segments'])
    recovered['coverage'] = copy.deepcopy(snapshot.get('coverage', {}))
    validate_resume(document, recovered, segment_indices, overwrite_user_edits=overwrite_user_edits)
    return recovered


def write_checkpoint(path, project, rows, completed_indices, segment_indices=None, warnings=None,
                     pipeline_state=None, expected_signature=None, *, entity_glossary=None,
                     overwrite_user_edits=False, skipped_source_indices=None):
    path = Path(path)
    current_signature = translation_signature(project, segment_indices)
    if expected_signature is not None and current_signature != expected_signature:
        raise ValueError('Nguồn hoặc snapshot đã thay đổi trong khi dịch; giữ checkpoint hợp lệ trước đó.')
    document = {'schema_version': VERSION, 'signature': current_signature,
                'rows': copy.deepcopy(rows), 'completed_indices': sorted(set(completed_indices)),
                'warnings': list(warnings or []), 'updated_at': time.time()}
    document['connection'] = connection_identity(project)
    if overwrite_user_edits:
        document['overwrite_user_edits'] = True
    if entity_glossary is not None:
        from translation_entities import validate_entity_glossary
        document['entity_glossary'] = validate_entity_glossary(entity_glossary, rows)
    if skipped_source_indices:
        document['skipped_source_indices'] = sorted(set(skipped_source_indices))
    if pipeline_state is not None:
        if (not isinstance(pipeline_state, dict) or pipeline_state.get('extract_kind') not in ('asr','ocr') or
                not re.fullmatch('[0-9a-f]{64}', str(pipeline_state.get('input_signature', ''))) or
                not isinstance(pipeline_state.get('recognition_snapshot', {}).get('segments'), list)):
            raise ValueError('Snapshot nhận dạng quy trình không hợp lệ.')
        document['pipeline_state'] = copy.deepcopy(pipeline_state)
    # Validate before touching the previous valid checkpoint.
    validate_resume(document, project, segment_indices, overwrite_user_edits=overwrite_user_edits)
    write_document(path, document)
    return document


def write_document(path, document):
    """Atomically replace a JSON checkpoint after durable, valid serialization."""
    path = Path(path)
    payload = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False)
    handle, name = tempfile.mkstemp(prefix=path.name+'.', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8', newline='\n') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return document
