"""Short durable project leases around queue admission, across processes.

The lease is committed before validation starts and released only after the
queued job has been committed (or validation failed). Other connections can
therefore read the store without deadlocking on a long SQLite transaction.
An abandoned lease is reclaimed only when its exact process has stopped.
"""
from contextlib import closing, contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import uuid

import psutil


def _alive(record):
    if (type(record.get('pid')) is not int or record['pid'] <= 0 or
            type(record.get('process_started')) not in (int, float) or
            not math.isfinite(record['process_started']) or record['process_started'] <= 0):
        return True
    try:
        return abs(psutil.Process(record['pid']).create_time() - record['process_started']) < .01
    except psutil.NoSuchProcess:
        return False
    except (psutil.AccessDenied, KeyError, TypeError, ValueError):
        # Ambiguous ownership cannot authorize a second admission.
        return True


@contextmanager
def project_lease(data_dir, project_id):
    if not isinstance(project_id, str) or not project_id or len(project_id) > 256:
        raise ValueError('Mã dự án không hợp lệ để xếp hàng.')
    database = Path(data_dir) / 'studio.sqlite3'
    identifier = hashlib.sha256(project_id.encode('utf-8')).hexdigest()
    record = {'pid': os.getpid(), 'process_started': psutil.Process().create_time(),
              'nonce': uuid.uuid4().hex}
    with closing(sqlite3.connect(database, timeout=20)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute("SELECT value FROM objects WHERE kind='admission_lease' AND id=?",
                              (identifier,)).fetchone()
        if previous:
            try:
                owner = json.loads(previous[0])
            except (ValueError, TypeError):
                raise ValueError('Khóa xếp hàng không hợp lệ; kiểm tra chẩn đoán trước khi chạy.') from None
            if not isinstance(owner, dict) or _alive(owner):
                raise ValueError('Dự án đang được xếp hàng bởi một yêu cầu khác. Chờ yêu cầu đó hoàn tất.')
        db.execute('INSERT OR REPLACE INTO objects VALUES (?,?,?)',
                   ('admission_lease', identifier, json.dumps(record)))
    try:
        yield
    finally:
        with closing(sqlite3.connect(database, timeout=20)) as db, db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("DELETE FROM objects WHERE kind='admission_lease' AND id=? AND value=?",
                       (identifier, json.dumps(record)))
