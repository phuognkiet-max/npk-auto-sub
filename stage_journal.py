"""Durable verified stage contracts, independent of final job success.

Recovery only fills compatible blanks or attaches exact matching artifacts to
a private snapshot. It never writes the editor or acknowledges its reviews.
"""
import copy
from functools import lru_cache
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid

NAME = 'stage-journal.json'
MAX_BYTES = 32 * 1024**2
ORDER = ('ocr', 'asr', 'auto_subtitles', 'translate', 'tts', 'clean', 'render')


def stage_signature(kind, project):
    from subtitle_quality import digest, source_signature
    from translation_checkpoint import translation_params
    if kind == 'tts':
        from worker import speech_signature
        return speech_signature(project)
    if kind == 'clean':
        from core import signature
        return signature(project)
    if kind == 'render':
        from artifact_resolver import render_fingerprint
        return render_fingerprint(project)
    if kind == 'ocr':
        from ocr_checkpoint import signature
        return signature(project)
    if kind == 'auto_subtitles':
        value = [source_signature(project), project['params'].get('subtitle_automation'),
                 project['params'].get('translation_model')]
        if project['params'].get('subtitle_automation') == 'router9':
            from providers import router_base_url
            value.extend([project['params'].get('provider'), router_base_url(project['params'].get('router9_base_url'))])
        return digest(value)
    p = project['params']
    if kind == 'asr':
        return digest([source_signature(project), {key: p.get(key) for key in
            ('source_language', 'asr_model', 'asr_device', 'asr_compute', 'extraction_start', 'extraction_end')}])
    rows = [[r.get('start'), r.get('end'), r.get('text', '')] for r in project.get('segments', [])]
    return digest([source_signature(project), rows, translation_params(project)])


@lru_cache(maxsize=256)
def _file_hash(path, size, modified, changed):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            digest.update(block)
    return digest.hexdigest()


def describe_file(folder, name):
    base, path = Path(folder).resolve(), Path(folder) / name
    if (not isinstance(name, str) or Path(name).name != name or path.is_symlink() or
            path.resolve().parent != base or not path.is_file()):
        raise ValueError('Artifact của bước xử lý thiếu hoặc không thuộc thư mục tác vụ.')
    stat = path.stat()
    identity = {'name': name, 'bytes': stat.st_size,
                'sha256': _file_hash(str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)}
    after = path.stat()
    if (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError('Artifact thay đổi trong lúc kiểm tra; chưa dùng lại kết quả này.')
    return identity


def artifact_integrity(project, kind, artifact=None):
    """Check current bytes against an identity captured at successful verification.

    This is a content/hash check, never a media decoder. Hashes are cached only
    for the current file stat. Missing legacy anchors are a different state
    from a known mismatch, so corruption cannot be migrated into a new truth.
    """
    if kind not in ('tts', 'clean'):
        raise ValueError('Loại artifact dùng lại không hợp lệ.')
    artifact = artifact if artifact is not None else (project.get('artifacts') or {}).get(kind) or {}
    field = 'speech_signature' if kind == 'tts' else 'signature'
    try:
        if artifact.get(field) != stage_signature(kind, project):
            return {'status': 'stale', 'files': []}
        if not isinstance(artifact.get('job_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', artifact['job_id']):
            return {'status': 'missing', 'files': []}
        owner = _entry_folder(Path(), {'owner_job_id': artifact.get('job_id')})
        primary = 'voice.wav' if kind == 'tts' else 'clean.mp4'
        if not (owner / primary).is_file() or (owner / primary).is_symlink():
            return {'status': 'missing', 'files': []}
        verification = artifact.get('verification') or {}
        if not isinstance(verification, dict):
            return {'status': 'unverified', 'files': []}
        expected = verification.get('file_integrity')
        if expected is None:
            # Pipeline results from before file_integrity already have a
            # durable ledger. That original ledger remains authoritative.
            try:
                document = load(owner)
                entry = document['stages'].get(kind) or {}
                if (document.get('project_id') == project.get('id') and
                        document.get('source_revision') == project.get('source_revision') and
                        entry.get('signature') == stage_signature(kind, project)):
                    expected = entry.get('files')
            except (OSError, ValueError, KeyError, TypeError):
                pass
        if expected is None:
            return {'status': 'legacy_unanchored' if verification and
                    primary in artifact.get('files', []) else 'unverified', 'files': []}
        if (not isinstance(expected, list) or not expected or
                any(not isinstance(item, dict) or set(item) != {'name', 'bytes', 'sha256'} for item in expected) or
                len({item['name'] for item in expected}) != len(expected) or
                primary not in {item['name'] for item in expected}):
            return {'status': 'corrupt', 'files': []}
        for item in expected:
            if describe_file(owner, item['name']) != item:
                return {'status': 'corrupt', 'files': []}
        return {'status': 'verified', 'files': copy.deepcopy(expected)}
    except (OSError, ValueError, KeyError, TypeError):
        return {'status': 'corrupt', 'files': []}


def require_reusable_artifact(project, kind, *, allow_legacy_verification=False):
    """Worker admission: reverify legacy media, reject an anchored mismatch."""
    artifact = (project.get('artifacts') or {}).get(kind) or {}
    state = artifact_integrity(project, kind, artifact)
    if state['status'] == 'legacy_unanchored' and allow_legacy_verification:
        from job_progress import verify_outputs
        owner = _entry_folder(Path(), {'owner_job_id': artifact.get('job_id')})
        artifact['verification'] = verify_outputs(artifact, project, {'kind': kind}, owner)
        # Retain the migration in the original owner ledger. Future UI polls
        # can compare hashes without decoding again or trusting a new export.
        record_stage(project, project, {'id': artifact['job_id']}, owner, kind, artifact)
        state = artifact_integrity(project, kind, artifact)
    if state['status'] != 'verified':
        name = 'giọng đã tạo' if kind == 'tts' else 'video đã xử lý chữ'
        raise ValueError(f'Tệp {name} thiếu hoặc không còn khớp lần kiểm chứng. Chạy lại bước này trước khi xuất.')
    return state['files']


def load(folder):
    path = Path(folder) / NAME
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Chưa có journal bước xử lý hợp lệ để tiếp tục.')
    from translation_checkpoint import _unique_object, _invalid_constant, _finite_document
    with path.open(encoding='utf-8') as stream:
        document = json.load(stream, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    _finite_document(document)
    if not isinstance(document, dict) or document.get('schema_version') != 1 or not isinstance(document.get('stages'), dict):
        raise ValueError('Journal bước xử lý không hợp lệ.')
    return document


def _write(folder, document):
    path = Path(folder) / NAME
    encoded = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
    if len(encoded) > MAX_BYTES:
        raise ValueError('Journal vượt giới hạn; các file đã kiểm vẫn được giữ.')
    temporary = path.with_name(NAME + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def record_stage(initial_project, project, job, folder, kind, result):
    from subtitle_quality import source_signature
    if kind not in ORDER or not result.get('verification'):
        raise ValueError('Chỉ ghi journal sau khi bước xử lý đã được kiểm chứng.')
    path = Path(folder) / NAME
    document = load(folder) if path.is_file() else {
        'schema_version': 1, 'job_id': job.get('id', Path(folder).name),
        'project_id': initial_project.get('id'), 'source_revision': initial_project.get('source_revision'),
        'source_signature': source_signature(initial_project),
        'initial_rows': copy.deepcopy(initial_project.get('segments', [])), 'stages': {}}
    document['stages'][kind] = {'signature': stage_signature(kind, project),
        'files': [describe_file(folder, name) for name in result.get('files', [])],
        'result': copy.deepcopy(result), 'checked_at': time.time()}
    if kind == 'auto_subtitles':
        # Selection can change the corpus. Bind the transition to the exact
        # recognized input rather than assuming its output has the same cues.
        document['stages'][kind]['input_rows'] = copy.deepcopy(project.get('segments', []))
    _write(folder, document)
    return NAME


def _entry_folder(folder, entry):
    owner = entry.get('owner_job_id')
    if owner is None:
        return Path(folder)
    if not isinstance(owner, str) or not re.fullmatch(r'[0-9a-f]{32}', owner):
        raise ValueError('Danh tính artifact dùng lại không hợp lệ.')
    from core import DATA
    path = DATA/'jobs'/owner
    if path.is_symlink() or path.resolve().parent != (DATA/'jobs').resolve():
        raise ValueError('Artifact dùng lại nằm ngoài kho tác vụ.')
    return path


def record_reuse(initial_project, project, job, folder, kind, artifact):
    """Carry verified owner references into every successor's durable journal."""
    if kind not in ('tts', 'clean'):
        return None
    state = artifact_integrity(project, kind, artifact)
    if state['status'] != 'verified':
        raise ValueError('Artifact dùng lại không còn khớp payload đã kiểm; chưa ghi journal mới.')
    entry = {'owner_job_id': artifact.get('job_id'), 'signature': stage_signature(kind, project),
             'result': copy.deepcopy(artifact), 'checked_at': time.time()}
    owner = _entry_folder(folder, entry)
    # Carry the original verified identities. Never bless the bytes currently
    # on disk by replacing the historical anchor with freshly computed hashes.
    entry['files'] = state['files']
    if not entry['files']:
        raise ValueError('Stage dùng lại không có danh sách file đã kiểm.')
    from subtitle_quality import source_signature
    path = Path(folder)/NAME
    document = load(folder) if path.is_file() else {
        'schema_version': 1, 'job_id': job.get('id', Path(folder).name),
        'project_id': initial_project.get('id'), 'source_revision': initial_project.get('source_revision'),
        'source_signature': source_signature(initial_project),
        'initial_rows': copy.deepcopy(initial_project.get('segments', [])), 'stages': {}}
    document['stages'][kind] = entry
    _write(folder, document)
    return NAME


def _valid_entry(folder, entry):
    result = entry.get('result')
    if not isinstance(result, dict) or not result.get('verification'):
        return False
    files = entry.get('files')
    if not isinstance(files, list) or not files:
        return False
    try:
        owner = _entry_folder(folder, entry)
        return all(isinstance(item, dict) and describe_file(owner, item['name']) == item for item in files)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _manual_coverage_is_bound(project, folder, entry):
    coverage = project.get('coverage') or {}
    selection = coverage.get('ocr_selection') or {}
    if selection.get('mode') != 'user_selected_tracks':
        return False
    from subtitle_quality import digest, source_signature
    if coverage.get('source_signature') != source_signature(project):
        return False
    if not any(item.get('name') == 'ocr-tracks.json' for item in entry['files']):
        return False
    from ocr_tracks import read_manifest
    try:
        evidence = read_manifest(_entry_folder(folder, entry)/'ocr-tracks.json', project)
        return selection.get('manifest_key') == digest(evidence)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def recover(project, folder):
    """Restore compatible stage state into a clone; return completed stage IDs."""
    from subtitle_quality import source_signature
    document = load(folder)
    if (document.get('project_id') != project.get('id') or
            document.get('source_revision') != project.get('source_revision') or
            document.get('source_signature') != source_signature(project)):
        raise ValueError('Journal thuộc video nguồn khác; không dùng lại cho dự án hiện tại.')
    snapshot, completed = copy.deepcopy(project), []
    for kind in ORDER:
        entry = document['stages'].get(kind)
        if not isinstance(entry, dict) or not _valid_entry(folder, entry):
            continue
        try:
            if entry.get('signature') != stage_signature(kind, snapshot):
                continue
        except (ValueError, OSError, KeyError):
            continue
        result = entry['result']
        if kind in ('ocr', 'asr', 'auto_subtitles'):
            saved = result.get('segments')
            if not isinstance(saved, list) or not saved:
                continue
            identities = lambda rows: [(r.get('start'), r.get('end'), r.get('text')) for r in rows]
            current = snapshot.get('segments', [])
            keep_manual_coverage = _manual_coverage_is_bound(snapshot, folder, entry)
            if kind == 'auto_subtitles':
                if identities(current) == identities(saved):
                    pass  # Keep edits made to the already selected table.
                elif keep_manual_coverage:
                    continue  # Explicit manual selection supersedes this proposal.
                elif (isinstance(entry.get('input_rows'), list) and
                      identities(current) == identities(entry['input_rows'])):
                    edited = {identity: row for identity, row in zip(identities(current), current)}
                    snapshot['segments'] = copy.deepcopy(saved)
                    for row in snapshot['segments']:
                        original = edited.get((row.get('start'), row.get('end'), row.get('text')))
                        if original is not None:
                            for key, value in original.items():
                                if key not in ('start', 'end', 'text', 'id'):
                                    row[key] = copy.deepcopy(value)
                else:
                    continue
            elif current == document.get('initial_rows', []):
                snapshot['segments'] = copy.deepcopy(saved)
            elif identities(current) != identities(saved):
                # The OCR/ASR artifact remains complete even when the user
                # later edited their subtitle table. Keep the evidence bound
                # to this project so another step can use it without replacing
                # their current rows.
                snapshot.setdefault('artifacts', {})[kind] = copy.deepcopy(result) | {
                    'job_id': entry.get('owner_job_id') or document['job_id']}
                completed.append(kind)
                continue
            if not keep_manual_coverage:
                snapshot['coverage'] = copy.deepcopy(result.get('coverage') or {})
            snapshot.setdefault('artifacts', {})[kind] = copy.deepcopy(result) | {
                'job_id': entry.get('owner_job_id') or document['job_id']}
        elif kind == 'translate':
            saved = result.get('segments') or []
            current = snapshot.get('segments', [])
            identity = lambda row: (row.get('start'), row.get('end'), row.get('text'))
            if [identity(r) for r in saved] != [identity(r) for r in current]:
                continue
            for original, target in zip(saved, current):
                if not str(target.get('translated') or '').strip():
                    target['translated'] = original.get('translated', '')
        else:
            snapshot.setdefault('artifacts', {})[kind] = copy.deepcopy(result) | {
                'job_id': entry.get('owner_job_id') or document['job_id']}
        completed.append(kind)
    return snapshot, completed


def summary(folder):
    """Expose valid stage progress without changing any project."""
    try:
        document = load(folder)
        return {'schema_version': 1, 'job_id': document.get('job_id'),
                'completed_steps': [kind for kind in ORDER if isinstance(document['stages'].get(kind), dict)
                                    and _valid_entry(folder, document['stages'][kind])]}
    except (ValueError, OSError, KeyError, TypeError):
        return None
