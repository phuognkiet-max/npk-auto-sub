"""Self-contained project ZIPs with a strict manifest and streaming checksums.

No secrets, executable content, models or worker snapshots are exported. Imports
are validated in an owned staging directory before a project becomes visible.
"""
from contextlib import closing
import copy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
import uuid
import zipfile

from core import DATA, DEFAULTS, get, put, probe, save_project_version, all_items
from project_transfer import export_document
from storage_manager import owned, is_link

MAX_EXPANDED = 30 * 1024**3
MAX_ENTRIES = 20
RESERVE = 256 * 1024**2


def _local_file(value):
    path = Path(value)
    if not path.is_absolute() or str(path).startswith(('\\\\', '//')):
        raise ValueError('Gói dự án chỉ nhận tệp trên máy, không nhận đường dẫn mạng.')
    if any(p.exists() and is_link(p) for p in [path, *path.parents]):
        raise ValueError('Không đóng gói qua liên kết tệp hoặc thư mục.')
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError('Thiếu tệp cần đóng gói.')
    return path


def _hash(path):
    before = path.stat()
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
        raise ValueError('Tệp nguồn thay đổi khi đóng gói; chạy lại sau khi lưu xong.')
    return {'bytes': after.st_size, 'sha256': digest.hexdigest()}


def _remove_owned(path, parent):
    resolved, root = path.resolve(), parent.resolve()
    if resolved == root or root not in resolved.parents or is_link(root) or (path.exists() and is_link(path)):
        raise ValueError('Không xóa đường dẫn ngoài vùng tạm của gói dự án.')
    if path.is_dir():
        # These are our private UUID staging directories, but still refuse
        # unexpected links instead of following a path replaced externally.
        from storage_manager import inventory
        inventory(path)
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def cleanup_export(path):
    _remove_owned(Path(path), owned('bundles'))


def export_bundle(project, include_source=True, include_audio=True, include_final=False):
    source = _local_file(project['media']['path'])
    assets = []
    if include_source:
        assets.append(('source', source, 'media/source' + source.suffix.lower()))
    if include_audio:
        for role, key in (('voice', 'voice_path'), ('bgm', 'bgm_path')):
            if project['params'].get(key):
                path = _local_file(project['params'][key])
                assets.append((role, path, 'audio/' + role + path.suffix.lower()))
    if include_final:
        from artifact_resolver import resolve_final
        resolved = resolve_final(project, all_items('job'), DATA)
        final = resolved['current']
        if not final:
            raise ValueError('Chưa có video thành phẩm khớp nội dung/cấu hình hiện tại. Xuất lại video hoặc bỏ chọn kèm thành phẩm; bản xuất cũ vẫn được giữ.')
        assets.append(('final', _local_file(final['path']), 'artifacts/final.mp4'))
    total = sum(p.stat().st_size for _, p, _ in assets)
    if total > MAX_EXPANDED or shutil.disk_usage(DATA).free < total + RESERVE:
        raise ValueError('Không đủ chỗ để tạo gói dự án, hoặc gói vượt giới hạn 30 GiB.')
    base = owned('bundles')
    base.mkdir(exist_ok=True)
    identifier = uuid.uuid4().hex
    pending, destination = base/(identifier + '.partial'), base/(identifier + '.npk.zip')
    files = []
    document = export_document(project)
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True).encode('utf-8')
    if len(encoded) > 4 * 1024**2:
        raise ValueError('Bản chỉnh sửa quá lớn để đóng gói an toàn.')
    try:
        with zipfile.ZipFile(pending, 'x', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.writestr('project.json', encoded)
            files.append({'path': 'project.json', 'role': 'project', 'bytes': len(encoded), 'sha256': hashlib.sha256(encoded).hexdigest()})
            for role, path, name in assets:
                before = path.stat()
                digest, length = hashlib.sha256(), 0
                with path.open('rb') as source_stream, archive.open(name, 'w', force_zip64=True) as output:
                    for block in iter(lambda: source_stream.read(1024 * 1024), b''):
                        output.write(block)
                        digest.update(block)
                        length += len(block)
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or length != before.st_size:
                    raise ValueError('Tệp thay đổi khi đóng gói; không phát hành gói chưa đầy đủ.')
                files.append({'path': name, 'role': role, 'bytes': length, 'sha256': digest.hexdigest()})
            source_info = next(({'bytes': f['bytes'], 'sha256': f['sha256']} for f in files if f['role'] == 'source'), None) or _hash(source)
            manifest = {'format': 'npk-project-bundle', 'version': 1, 'created': time.time(), 'files': files,
                        'source_reference': source_info, 'note': 'Không kèm khóa, runtime hoặc model. Nhập tạo dự án mới, không thay bản gốc.'}
            archive.writestr('manifest.json', json.dumps(manifest, ensure_ascii=False).encode('utf-8'))
        with pending.open('r+b') as saved:
            os.fsync(saved.fileno())
        os.rename(pending, destination)
        return destination
    except Exception:
        _remove_owned(pending, base)
        raise


def _safe_archive_name(name):
    if not isinstance(name, str) or '\\' in name or ':' in name or '\x00' in name or name.startswith('/'):
        raise ValueError('Gói chứa đường dẫn không hợp lệ.')
    parts = PurePosixPath(name).parts
    if not parts or any(p in ('', '.', '..') or p.endswith((' ', '.')) or p.upper().split('.')[0] in {'CON', 'PRN', 'AUX', 'NUL', *('COM' + str(i) for i in range(1, 10)), *('LPT' + str(i) for i in range(1, 10))} for p in parts):
        raise ValueError('Gói chứa đường dẫn không an toàn trên Windows.')
    if str(PurePosixPath(name)) != name:
        raise ValueError('Gói chứa đường dẫn trùng hoặc chưa chuẩn hóa.')
    return name


def import_bundle(archive_path, source=''):
    import server
    base = owned('imports')
    base.mkdir(exist_ok=True)
    identifier = uuid.uuid4().hex
    staging, destination = base/('.staging-' + identifier), base/identifier
    staging.mkdir()
    committed = False
    moved = False
    try:
        with zipfile.ZipFile(archive_path, 'r') as archive:
            entries = archive.infolist()
            if not 2 <= len(entries) <= MAX_ENTRIES:
                raise ValueError('Gói có quá nhiều mục hoặc thiếu manifest.')
            by_name = {}
            for info in entries:
                name = _safe_archive_name(info.filename)
                key = name.casefold()
                if key in by_name or info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    raise ValueError('Gói có mục trùng, liên kết hoặc mã hóa không được hỗ trợ.')
                if info.file_size > MAX_EXPANDED or info.file_size > max(1024**2, info.compress_size * 200):
                    raise ValueError('Gói có kích thước giải nén không an toàn.')
                by_name[key] = info
            total = sum(i.file_size for i in entries)
            if total > MAX_EXPANDED or shutil.disk_usage(DATA).free < total + RESERVE:
                raise ValueError('Ổ dữ liệu không đủ chỗ giải nén gói dự án.')
            manifest_info = by_name.get('manifest.json')
            if not manifest_info or manifest_info.file_size > 1024**2:
                raise ValueError('Thiếu manifest hợp lệ của NPK.')
            manifest = json.loads(archive.read(manifest_info).decode('utf-8'))
            if not isinstance(manifest, dict) or manifest.get('format') != 'npk-project-bundle' or manifest.get('version') != 1 or not isinstance(manifest.get('files'), list):
                raise ValueError('Không phải gói dự án NPK được hỗ trợ.')
            files, roles = {}, {}
            for item in manifest['files']:
                if not isinstance(item, dict):
                    raise ValueError('Manifest không hợp lệ.')
                name = _safe_archive_name(item.get('path'))
                role = item.get('role')
                if role not in ('project', 'source', 'voice', 'bgm', 'final') or role in roles or name.casefold() in files:
                    raise ValueError('Vai trò tệp trong gói không hợp lệ hoặc bị trùng.')
                prefixes = {'project': 'project.json', 'source': 'media/source.', 'voice': 'audio/voice.', 'bgm': 'audio/bgm.', 'final': 'artifacts/final.mp4'}
                if (name != prefixes[role] if role in ('project', 'final') else not name.startswith(prefixes[role])):
                    raise ValueError('Đường dẫn tệp không khớp vai trò trong gói.')
                if role in ('source', 'voice', 'bgm') and not re.fullmatch(r'[a-z0-9]{1,10}', name.rsplit('.', 1)[-1]):
                    raise ValueError('Đuôi tệp trong gói không hợp lệ.')
                info = by_name.get(name.casefold())
                if not info or type(item.get('bytes')) is not int or info.file_size != item['bytes'] or not re.fullmatch(r'[a-f0-9]{64}', str(item.get('sha256', ''))):
                    raise ValueError('Kích thước hoặc checksum trong manifest không hợp lệ.')
                if role == 'project' and info.file_size > 4 * 1024**2:
                    raise ValueError('Bản chỉnh sửa trong gói quá lớn.')
                files[name.casefold()] = item
                roles[role] = name
            if 'project' not in roles or set(by_name) != set(files) | {'manifest.json'}:
                raise ValueError('Gói chứa tệp ngoài danh sách manifest.')
            for name, item in files.items():
                info = by_name[name]
                target = staging.joinpath(*PurePosixPath(info.filename).parts)
                if staging.resolve() not in target.resolve().parents:
                    raise ValueError('Đường dẫn giải nén nằm ngoài vùng tạm.')
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, length = hashlib.sha256(), 0
                with archive.open(info, 'r') as payload, target.open('xb') as output:
                    for block in iter(lambda: payload.read(1024 * 1024), b''):
                        length += len(block)
                        if length > item['bytes']:
                            raise ValueError('Tệp giải nén vượt kích thước khai báo.')
                        output.write(block)
                        digest.update(block)
                    output.flush()
                    os.fsync(output.fileno())
                if length != item['bytes'] or digest.hexdigest() != item['sha256']:
                    raise ValueError('Checksum không khớp; gói chưa được khôi phục.')
        document = json.loads((staging/'project.json').read_text(encoding='utf-8'))
        if not isinstance(document, dict) or document.get('format') != 'npk-project' or document.get('version') != 1:
            raise ValueError('Thiếu bản chỉnh sửa NPK hợp lệ.')
        source_file = staging/roles['source'] if 'source' in roles else _local_file(source)
        reference = manifest.get('source_reference')
        if not isinstance(reference, dict) or _hash(source_file) != reference:
            raise ValueError('Video nguồn khác checksum của bản sao; chọn đúng video gốc.')
        media = probe(source_file)
        original = document.get('media', {})
        if abs(media['duration'] - float(original.get('duration', 0))) > .2 or any(media[k] != original.get(k) for k in ('width', 'height')):
            raise ValueError('Video nguồn không khớp bản chỉnh sửa.')
        params = {'tts_timing_mode': 'legacy'} | dict(document.get('params', {}))
        for key in ('export_directory', 'voice_path', 'bgm_path'):
            params[key] = ''
        for role, key in (('voice', 'voice_path'), ('bgm', 'bgm_path')):
            if role in roles:
                params[key] = str(staging/roles[role])
        from core import restored_params
        params = restored_params(server.validate_params(params))
        rows = server.validate_segments(document.get('segments', []), media['duration'])
        regions = server.validate_regions(document.get('regions', []), media)
        reading = server.validate_regions(document.get('extraction_regions', []), media)
        os.rename(staging, destination)
        moved = True
        if 'source' in roles:
            media['path'] = str(destination/roles['source'])
        for role, key in (('voice', 'voice_path'), ('bgm', 'bgm_path')):
            if role in roles:
                params[key] = str(destination/roles[role])
        now = time.time()
        project = {'id': identifier, 'name': str(document.get('name', 'Dự án'))[:180] + ' — khôi phục gói',
                   'media': media, 'params': params, 'segments': rows, 'regions': regions,
                   'extraction_regions': reading, 'artifacts': {}, 'created': now, 'updated': now,
                   'source_revision': uuid.uuid4().hex,
                   'bundle_import': {'version': 1, 'folder': str(destination), 'final': str(destination/roles['final']) if 'final' in roles else ''}}
        save_project_version(project)
        committed = True
        return project
    except (zipfile.BadZipFile, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError('Gói dự án hỏng hoặc cấu trúc không hợp lệ.') from exc
    finally:
        if staging.exists():
            _remove_owned(staging, base)
        if moved and not committed:
            # A failing commit can be ambiguous. Preserve files if SQLite cannot
            # confirm that no project record points at them.
            try:
                exists = get('project', identifier)
            except Exception:
                exists = True
            if not exists and destination.exists():
                _remove_owned(destination, base)
