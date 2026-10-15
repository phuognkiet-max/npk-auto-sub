"""Conservative, reversible cleanup of NPK-owned generated data only.

Call mutation functions under server.state_lock. Plans never accept filesystem
targets from the client. A SQLite journal survives interruptions between moves.
Sources, published finals, other projects and their histories are protected.
"""
from contextlib import closing
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time
import uuid

from core import ROOT, DATA, connect, get, put, all_items

ID = re.compile(r'^[0-9a-f]{32}$')
TERMINAL = {'done', 'review', 'error', 'cancelled'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def is_link(path):
    s = path.lstat()
    return stat.S_ISLNK(s.st_mode) or bool(getattr(s, 'st_file_attributes', 0) & 0x400)


def owned(relative):
    """Reject traversal and every existing junction/symlink ancestor."""
    relative = Path(relative)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts:
        raise ValueError('Đường dẫn dọn dữ liệu không hợp lệ.')
    path = DATA
    if is_link(path):
        raise ValueError('Không dọn qua thư mục dữ liệu liên kết.')
    for component in relative.parts:
        path = path / component
        if os.path.lexists(path) and is_link(path):
            raise ValueError('Không dọn qua liên kết thư mục hoặc tệp.')
    resolved = path.resolve()
    if resolved == DATA or DATA not in resolved.parents:
        raise ValueError('Đường dẫn nằm ngoài dữ liệu ứng dụng.')
    return path


def inventory(path):
    """Strict snapshot. Abort on access errors/links, never follow them."""
    if not os.path.lexists(path):
        return []
    rows = []
    def visit(p):
        if is_link(p):
            raise ValueError('Có liên kết trong dữ liệu; giữ nguyên để bảo vệ tệp ngoài.')
        s = p.stat()
        rows.append([str(p.relative_to(path)), p.is_dir(), s.st_size if p.is_file() else 0,
                     s.st_mtime_ns, s.st_ino])
        if p.is_dir():
            for child in sorted(p.iterdir()):
                visit(child)
        elif not p.is_file():
            raise ValueError('Có thành phần đặc biệt không thể dọn an toàn.')
    visit(path)
    return rows


def measured(path):
    try:
        rows = inventory(path)
        return {'bytes': sum(r[2] for r in rows), 'files': sum(not r[1] for r in rows), 'error': None}
    except (ValueError, OSError):
        return {'bytes': None, 'files': None, 'error': 'Chưa đo được đầy đủ; không tự dọn.'}


def _documents():
    documents = all_items('project') + all_items('project_revision') + all_items('project_backup')
    for entry in all_items('storage_trash'):
        if entry.get('state') not in ('restored', 'purged'):
            documents += [r[2] for r in entry.get('records', [])]
    return documents


def job_refs(value):
    result = set()
    if isinstance(value, dict):
        if isinstance(value.get('job_id'), str):
            result.add(value['job_id'])
        for child in value.values():
            result.update(job_refs(child))
    elif isinstance(value, list):
        for child in value:
            result.update(job_refs(child))
    return result


def input_paths():
    paths = set()
    for p in _documents() + [{'params': get('settings', 'main') or {}}]:
        for value in [p.get('media', {}).get('path'), p.get('params', {}).get('voice_path'),
                      p.get('params', {}).get('bgm_path')]:
            if value:
                paths.add(Path(value).resolve())
    for job in all_items('job'):
        published = job.get('result', {}).get('published', {}).get('path')
        if published:
            paths.add(Path(published).resolve())
    return paths


def contains_input(path, paths):
    return any(path == p or path in p.parents for p in paths)


def _ensure_idle():
    import server
    if server.active_id or any(j['state'] not in TERMINAL for j in all_items('job')):
        raise ValueError('Đợi hàng đợi và bộ xử lý dừng hẳn trước khi dọn hoặc xóa dự án.')
    # Cancellation metadata can be terminal before an orphan worker has exited.
    import psutil
    script = os.path.normcase(str(ROOT/'worker.py'))
    for process in psutil.process_iter(['name', 'cmdline']):
        try:
            if (process.info['name'] or '').lower() not in ('python.exe', 'pythonw.exe'):
                continue
            args = process.info['cmdline'] or []
            if not any(os.path.normcase(os.path.abspath(a)) == script for a in args[1:] if a and not a.startswith('-')):
                continue
            for a in args[1:]:
                if a and Path(a).name == 'input.json' and Path(a).resolve().parent.parent == DATA/'jobs':
                    raise ValueError('Bộ xử lý cũ chưa thoát hẳn. Đợi dừng trước khi dọn dữ liệu.')
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue


def _ensure_no_pending():
    if any(e.get('state') in ('moving', 'attention', 'restoring') for e in all_items('storage_trash')):
        raise ValueError('Có lượt dọn bị gián đoạn. Mở Thùng rác và khôi phục lượt đó trước.')


def _records(identifier):
    with closing(connect()) as db:
        rows = db.execute("SELECT kind,id,value FROM objects WHERE kind IN ('project','project_revision','project_backup')").fetchall()
    return [[k, i, json.loads(v)] for k, i, v in rows if json.loads(v).get('id') == identifier]


def preview(action, identifier=None):
    if action == 'speech':
        return speech_preview(identifier)
    if action not in ('cleanup', 'delete', 'cache'):
        raise ValueError('Thao tác dọn không được hỗ trợ.')
    project = get('project', identifier) if isinstance(identifier, str) and ID.fullmatch(identifier) else None
    if action != 'cache' and not project:
        raise ValueError('Không tìm thấy dự án để dọn.')
    documents = _documents()
    refs = set()
    own_refs = set()
    existing_ids = {p['id'] for p in all_items('project')}
    for doc in documents:
        if doc.get('id') == identifier:
            own_refs.update(job_refs(doc.get('artifacts', {})))
        if action != 'delete' or doc.get('id') != identifier:
            refs.update(job_refs(doc.get('artifacts', {})))
    protected = input_paths()
    targets, retained, jobs = [], [], []
    candidates = []
    if action == 'cache':
        cache = owned('speech-cache')
        if cache.is_dir():
            candidates = [(p.relative_to(DATA).as_posix(), None) for p in sorted(cache.iterdir()) if p.suffix == '.wav']
        for j in all_items('job'):
            if j.get('kind') == 'voice_preview' and j['state'] in TERMINAL and ID.fullmatch(j['id']):
                p = owned('jobs/' + j['id'])
                if p.is_dir() and j['id'] not in refs and not (p/'final.mp4').exists():
                    candidates.append(('jobs/' + j['id'], j))
    else:
        for j in all_items('job'):
            borrowed_orphan = j['id'] in own_refs and j.get('project_id') not in existing_ids
            if (j.get('project_id') != identifier and not borrowed_orphan) or not ID.fullmatch(j['id']):
                continue
            relative = 'jobs/' + j['id']
            try:
                p = owned(relative)
                if not p.exists():
                    continue
                size = measured(p)
                reason = None
                if j['state'] not in TERMINAL:
                    reason = 'Tác vụ chưa dừng'
                elif j['id'] in refs:
                    reason = 'Dự án hoặc lịch sử phiên bản còn dùng'
                elif (p / 'final.mp4').exists() or j.get('result', {}).get('primary_file') == 'final.mp4':
                    reason = 'Giữ video thành phẩm và tệp đi kèm'
                elif contains_input(p, protected):
                    reason = 'Có video nguồn, âm thanh nhập hoặc bản xuất cần giữ'
                elif size['error']:
                    reason = size['error']
                if reason:
                    retained.append({'path': relative, 'bytes': size['bytes'], 'reason': reason})
                    continue
                candidates.append((relative, j))
            except (OSError, ValueError):
                retained.append({'path': relative, 'bytes': None, 'reason': 'Không truy cập an toàn; giữ nguyên'})
        frames = owned('frames')
        if frames.is_dir():
            candidates += [(p.relative_to(DATA).as_posix(), None) for p in frames.iterdir()
                           if p.is_file() and p.name.startswith(identifier + '-')]
    for relative, job in candidates:
        p = owned(relative)
        if contains_input(p, protected):
            retained.append({'path': relative, 'bytes': None, 'reason': 'Tệp đang được dùng làm đầu vào'})
            continue
        rows = inventory(p)
        targets.append({'path': relative, 'bytes': sum(r[2] for r in rows), 'snapshot': digest(rows)})
        if job:
            jobs.append(job)
    records = _records(identifier) if action == 'delete' else []
    blocked = None
    try:
        _ensure_idle()
        _ensure_no_pending()
    except ValueError as e:
        blocked = str(e)
    result = {'action': action, 'project_id': identifier, 'name': project['name'] if project else 'Bộ nhớ đệm giọng',
              'bytes': sum(t['bytes'] for t in targets), 'targets': targets, 'retained': retained,
              'records': records, 'jobs': jobs, 'revision': project['updated'] if project else None, 'blocked': blocked}
    result['managed_bytes'] = result['bytes'] + sum(t['bytes'] or 0 for t in retained)
    result['measurement_partial'] = any(t['bytes'] is None for t in retained)
    result['signature'] = digest(result)
    return result


def speech_preview(identifier):
    """A standalone recording is not a project, but has the same trash journal."""
    job = get('job', identifier) if isinstance(identifier, str) and ID.fullmatch(identifier) else None
    if not job or job.get('kind') != 'text_to_speech' or job.get('storage_removed'):
        raise ValueError('Không tìm thấy bản đọc để dọn.')
    relative = 'jobs/' + identifier
    folder = owned(relative)
    references = set().union(*(job_refs(d.get('artifacts', {})) for d in _documents()))
    reason = None
    if job.get('state') not in TERMINAL:
        reason = 'Bản đọc chưa dừng'
    elif identifier in references:
        reason = 'Dự án hoặc lịch sử phiên bản còn dùng'
    elif contains_input(folder, input_paths()):
        reason = 'Có âm thanh đang được dùng làm đầu vào hoặc bản xuất cần giữ'
    rows = inventory(folder) if folder.exists() else []
    size = sum(r[2] for r in rows)
    targets = [{'path': relative, 'bytes': size, 'snapshot': digest(rows)}] if rows and not reason else []
    blocked = reason
    try:
        _ensure_idle()
        _ensure_no_pending()
    except ValueError as exc:
        blocked = str(exc)
    plan = {'action': 'speech', 'project_id': identifier, 'name': job.get('speech_name') or 'Bản đọc ' + identifier[:8],
            'bytes': size if targets else 0, 'targets': targets,
            'retained': [{'path': relative, 'bytes': size, 'reason': reason}] if reason else [],
            'records': [], 'jobs': [job] if targets else [], 'revision': job.get('created'), 'blocked': blocked,
            'managed_bytes': size, 'measurement_partial': False}
    plan['signature'] = digest(plan)
    return plan


def public_plan(plan):
    return {k: v for k, v in plan.items() if k not in ('records', 'jobs')}


def _db_write(entry, deleting=False, restoring=False):
    with closing(connect()) as db:
        with db:
            for kind, identifier, value in entry['records']:
                if deleting:
                    db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, identifier))
                elif restoring:
                    value = copy.deepcopy(value)
                    if kind == 'project':
                        value['updated'] = time.time()
                    db.execute('INSERT INTO objects VALUES (?,?,?)', (kind, identifier, json.dumps(value, ensure_ascii=False)))
            for job in entry['jobs']:
                value = copy.deepcopy(job)
                if not restoring:
                    value.update(storage_removed=True, storage_trash_id=entry['id'])
                db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)', ('job', job['id'], json.dumps(value, ensure_ascii=False)))
            db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)', ('storage_trash', entry['id'], json.dumps(entry, ensure_ascii=False)))


def execute(action, identifier, body):
    _ensure_idle()
    _ensure_no_pending()
    plan = preview(action, identifier)
    # A project-delete click is the request itself; no typed name or second
    # confirmation. Keep the fresh snapshot and all ownership/busy checks.
    if body.get('signature') != plan['signature']:
        raise ValueError('Dữ liệu đã đổi. Chọn lại thao tác để dùng danh sách mới.')
    if action != 'delete' and body.get('confirmed') is not True:
        raise ValueError('Dữ liệu đã đổi hoặc chưa xác nhận. Xem lại danh sách trước khi dọn.')
    if action != 'delete' and not plan['targets']:
        raise ValueError('Không có dữ liệu có thể dọn an toàn trong nhóm này.')
    entry = {k: copy.deepcopy(v) for k, v in plan.items() if k not in ('signature', 'blocked', 'retained')}
    entry.update(id=uuid.uuid4().hex, created=time.time(), state='moving')
    base = owned('trash/' + entry['id'])
    base.mkdir(parents=True, exist_ok=False)
    put('storage_trash', entry['id'], entry)  # Durable journal before first move.
    try:
        for target in entry['targets']:
            src = owned(target['path'])
            dst = owned('trash/' + entry['id'] + '/' + target['path'])
            if digest(inventory(src)) != target['snapshot']:
                raise ValueError('Tệp đã thay đổi khi dọn. Không tiếp tục.')
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)  # Same-volume move; no copy and no overwrite.
        entry['state'] = 'ready'
        _db_write(entry, deleting=action == 'delete')
    except Exception:
        entry['state'] = 'attention'
        put('storage_trash', entry['id'], entry)
        # Preserve the journal and every moved file. Explicit restore repairs it.
        raise ValueError('Lượt dọn bị gián đoạn; dữ liệu được giữ lại. Mở Thùng rác để khôi phục.')
    return {'trash_id': entry['id'], 'moved_bytes': entry['bytes'], 'freed_bytes': 0,
            'message': 'Đã chuyển vào thùng rác NPK. Chưa giải phóng dung lượng; có thể khôi phục.'}


def _entry(identifier):
    e = get('storage_trash', identifier) if ID.fullmatch(identifier) else None
    if not e or e['state'] in ('restored', 'purged'):
        raise ValueError('Mục thùng rác không còn khả dụng.')
    return e


def restore(identifier):
    _ensure_idle()
    e = _entry(identifier)
    if e['state'] == 'purging':
        raise ValueError('Mục này đã bắt đầu xóa vĩnh viễn, không thể khôi phục đầy đủ.')
    # A ready delete removed records; an interrupted move did not commit them.
    restore_records = e['action'] == 'delete' and not get('project', e['project_id'])
    if e['state'] == 'ready' and e['action'] == 'delete' and not restore_records:
        raise ValueError('Mã dự án đã tồn tại; không ghi đè khi khôi phục.')
    for t in e['targets']:
        src = owned('trash/' + identifier + '/' + t['path'])
        dst = owned(t['path'])
        if src.exists() and dst.exists():
            raise ValueError('Vị trí gốc đã có dữ liệu mới. Giữ cả hai, không ghi đè.')
        if not src.exists() and not dst.exists():
            raise ValueError('Thiếu tệp cần khôi phục; giữ thùng rác để kiểm tra.')
        if digest(inventory(src if src.exists() else dst)) != t['snapshot']:
            raise ValueError('Tệp đã thay đổi bên ngoài; không tự khôi phục đè dữ liệu.')
    e['state'] = 'restoring'
    put('storage_trash', identifier, e)
    for t in e['targets']:
        src = owned('trash/' + identifier + '/' + t['path'])
        dst = owned(t['path'])
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
    e['state'] = 'restored'
    # Interrupted delete never removed records: do not insert duplicates.
    if not restore_records:
        e['records'] = []
    _db_write(e, restoring=True)
    e.update(records=[], jobs=[], targets=[])
    put('storage_trash', identifier, e)
    # Restored entries no longer protect or retain duplicate metadata.
    return {'restored': True, 'project_id': e['project_id'], 'message': 'Đã khôi phục dữ liệu; không ghi đè tệp mới.'}


def purge_preview(identifier):
    e = _entry(identifier)
    if e['state'] not in ('ready', 'purging'):
        raise ValueError('Khôi phục lượt bị gián đoạn trước; không xóa vĩnh viễn dữ liệu chưa đối soát.')
    base = owned('trash/' + identifier)
    if contains_input(base, input_paths()):
        raise ValueError('Có dự án đang dùng tệp trong thùng rác. Không xóa.')
    # Each remaining top-level payload must still match its original inventory.
    # This refuses foreign files added manually instead of deleting them silently.
    for t in e['targets']:
        path = owned('trash/' + identifier + '/' + t['path'])
        if path.exists() and e['state'] != 'purging' and digest(inventory(path)) != t['snapshot']:
            raise ValueError('Nội dung thùng rác đã thay đổi. Giữ nguyên để kiểm tra.')
    rows = inventory(base)
    if e['state'] == 'purging':
        original = {r[0]: r for r in e.get('purge_inventory', [])}
        for r in rows:
            old = original.get(r[0])
            if old is None or (not r[1] and r != old):
                raise ValueError('Có dữ liệu mới trong lượt xóa dở; không tiếp tục xóa.')
    allowed = [base / t['path'] for t in e['targets']]
    for relative, directory, *_ in rows:
        p = base / relative
        if p != base and not any(p == a or a in p.parents or (directory and p in a.parents) for a in allowed):
            raise ValueError('Có tệp ngoài danh sách trong thùng rác; không tự xóa.')
    return {'id': identifier, 'name': e['name'], 'bytes': sum(r[2] for r in rows),
            'signature': digest([e, rows]), 'state': e['state']}


def purge(identifier, body):
    _ensure_idle()
    plan = purge_preview(identifier)
    if body.get('signature') != plan['signature']:
        raise ValueError('Dữ liệu thùng rác đã đổi. Bấm lại thao tác để dùng danh sách mới.')
    e = _entry(identifier)
    if e['state'] != 'purging':
        e['purge_inventory'] = inventory(owned('trash/' + identifier))
    e['state'] = 'purging'
    put('storage_trash', identifier, e)
    base = owned('trash/' + identifier)
    # Exact validated leaf subtree only. Never DATA, app root, models or runtime.
    if base.exists():
        inventory(base)  # Recheck all descendants for reparse points.
        shutil.rmtree(base)
    e.update(state='purged', records=[], jobs=[], targets=[], purge_inventory=[], purged_at=time.time())
    put('storage_trash', identifier, e)
    return {'removed_bytes': plan['bytes'], 'message': 'Đã xóa vĩnh viễn mục đã chọn; không thể khôi phục trong NPK.'}


def overview():
    categories = [('jobs', 'Các lượt xử lý video', DATA/'jobs', 'Có thể dọn theo từng dự án'),
                  ('frames', 'Khung xem và tệp phụ đề tạm', DATA/'frames', 'Có thể tạo lại'),
                  ('speech-cache', 'Bộ nhớ đệm giọng', DATA/'speech-cache', 'Dùng chung; có thể tạo lại'),
                  ('exports', 'Video thành phẩm mặc định', DATA/'exports', 'Không tự xóa'),
                  ('trash', 'Thùng rác NPK', DATA/'trash', 'Chưa giải phóng dung lượng'),
                  ('webview', 'Dữ liệu giao diện', DATA/'webview', 'Ứng dụng quản lý; không dọn ở đây'),
                  ('runtime', 'Bộ chạy và thư viện', ROOT/'runtime', 'Thành phần hệ thống; không xóa'),
                  ('models', 'Model và kho giọng', ROOT/'models', 'Thành phần chức năng; không xóa'),
                  ('tests', 'Dữ liệu kiểm thử phát triển', ROOT/'tests', 'Không tăng theo dự án; không dọn từ ứng dụng'),
                  ('build', 'Bản build và bản sao EXE', ROOT/'build', 'Không tăng theo dự án; không dọn từ ứng dụng')]
    rows = [dict(id=i, name=n, path=str(p), note=note, **measured(p)) for i, n, p, note in categories]
    entries = [dict(id=e['id'], name=e['name'], action=e['action'], state=e['state'], created=e['created'],
                    bytes=e['bytes']) for e in all_items('storage_trash') if e['state'] not in ('restored', 'purged')]
    blocked = None
    try:
        _ensure_idle()
    except ValueError as e:
        blocked = str(e)
    return {'root': str(DATA), 'free_bytes': shutil.disk_usage(DATA).free, 'categories': rows, 'trash': entries,
            'blocked': blocked, 'database': str(DATA/'studio.sqlite3'),
            'note': 'Nhập video chỉ lưu đường dẫn, không chép video nguồn. Nơi xuất tùy chỉnh nằm ngoài bảng này.'}
