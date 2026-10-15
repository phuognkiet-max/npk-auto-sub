"""Inspect a stopped pipeline read-only; enqueue only through its live owner.

No server/core import, database initialization, credential access, or direct
route invocation is permitted here. --enqueue talks to the authenticated
desktop owner and retains the same revision/checkpoint admission as the UI.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
MAX_DOCUMENT_BYTES = 32 * 1024**2


def _identifier(value):
    if not isinstance(value, str) or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('Mã tác vụ không hợp lệ.')
    return value


def _read_json(path):
    path = Path(path)
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_DOCUMENT_BYTES:
            return None
        with path.open('rb') as stream:
            raw = stream.read(MAX_DOCUMENT_BYTES + 1)
        return json.loads(raw.decode('utf-8')) if len(raw) <= MAX_DOCUMENT_BYTES else None
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None


def read_store(data_dir, identifier):
    """One read-only SQLite snapshot; missing DATA is never created."""
    path = (Path(data_dir) / 'studio.sqlite3').resolve()
    if not path.is_file() or path.is_symlink():
        raise ValueError('Không có kho dữ liệu NPK để kiểm tra.')
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            stored = db.execute('SELECT value FROM objects WHERE kind=? AND id=?', ('job', identifier)).fetchone()
            row = json.loads(stored[0]) if stored else None
            if not isinstance(row, dict):
                raise ValueError('Không tìm thấy tác vụ.')
            _identifier(row.get('project_id'))
            stored = db.execute('SELECT value FROM objects WHERE kind=? AND id=?', ('project', row['project_id'])).fetchone()
            project = json.loads(stored[0]) if stored else None
            if not isinstance(project, dict):
                raise ValueError('Không tìm thấy dự án của tác vụ.')
            jobs = [json.loads(value) for (value,) in db.execute('SELECT value FROM objects WHERE kind=?', ('job',))]
    except sqlite3.Error:
        raise ValueError('Không đọc được kho dữ liệu NPK.') from None
    return row, project, jobs


def _observed_counts(folder):
    checkpoint = _read_json(Path(folder) / 'translation-checkpoint.json')
    rows = checkpoint.get('rows') if isinstance(checkpoint, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        return {}
    return {'observed_checkpoint_cues': len(rows), 'observed_translated_cues':
            sum(isinstance(row.get('translated'), str) and bool(row['translated'].strip()) for row in rows)}


def _admission(project, jobs, row, monitor_path, remaining_steps=None, now=None):
    # This module is deliberately pure and does not initialize the application.
    from execution_admission import admission_status
    return admission_status(project, jobs, kind='pipeline', remaining_steps=remaining_steps,
                            now=now, automatic=True, monitor_path=monitor_path)


def _request(client, record, method, path, body=None):
    from owner_connection import OwnerUnavailable
    try:
        response = client.request(method, 'http://127.0.0.1:' + str(record['port']) + path,
                                  headers={'x-studio-token': record['token']}, json=body,
                                  follow_redirects=False)
        if response.status_code != 200:
            raise OwnerUnavailable('owner_http_' + str(response.status_code))
        if len(response.content) > 2 * 1024**2:
            raise OwnerUnavailable('owner_response_invalid')
        payload = response.json()
        if not isinstance(payload, dict):
            raise OwnerUnavailable('owner_response_invalid')
        return payload
    except OwnerUnavailable:
        raise
    except Exception:
        # Transport errors can include authentication headers; never echo them.
        raise OwnerUnavailable('owner_unreachable') from None


def _apply_admission(report, admission):
    for key in ('execution_allowed', 'provider_required', 'blocked_code', 'blocked_reason',
                'earliest_retry_utc', 'quota_scope', 'latest_job_id'):
        if key in admission:
            report[key] = admission[key]


def inspect_or_continue(identifier, *, enqueue=False, data_dir=None, monitor_path=None, client=None, now=None):
    identifier = _identifier(identifier)
    data_dir = Path(data_dir if data_dir is not None else os.environ.get('VIETDUB_DATA_DIR', ROOT / 'data')).resolve()
    monitor_path = (Path(monitor_path) if monitor_path is not None else
                    ROOT / 'build/npk-pipeline-monitor-state.json' if data_dir == (ROOT / 'data').resolve() else
                    data_dir / 'npk-pipeline-monitor-state.json')
    row, project, jobs = read_store(data_dir, identifier)
    if row.get('kind') != 'pipeline' or row.get('state') not in ('error', 'review', 'cancelled'):
        raise ValueError('Chỉ tiếp tục tác vụ quy trình đã dừng.')
    matching = [job for job in jobs if job.get('project_id') == project['id'] and
                job.get('source_revision') == project.get('source_revision')]
    latest = max(matching, key=lambda job: (job.get('created', 0), job.get('updated', 0)), default=row)
    report = {'previous_job': identifier, 'project_id': project['id'], 'state': row['state'],
              'expected_revision': project.get('updated'), 'can_continue': False,
              'content_ready': None, 'owner_available': False, 'enqueued': False,
              'reason': 'Chưa xác nhận được ứng dụng đang sở hữu hàng đợi.'}
    report.update(_observed_counts(data_dir / 'jobs' / _identifier(latest['id'])))
    from provider_usage import read_summary
    usage = read_summary(data_dir / 'jobs' / latest['id'])
    if usage is not None:
        report['api_usage'] = usage
    admission = _admission(project, jobs, row, monitor_path, now=now)
    _apply_admission(report, admission)
    from owner_connection import OwnerUnavailable, load, validate_live_owner
    try:
        record = load(data_dir, now=now)
        own_client = client is None
        if own_client:
            import httpx
            client = httpx.Client(timeout=5, trust_env=False, follow_redirects=False)
        try:
            identity = _request(client, record, 'GET', '/api/studio/owner')
            validate_live_owner(record, identity, now=now)
            plan = _request(client, record, 'GET', '/api/studio/jobs/' + identifier + '/continuation')
            if plan.get('project_id') != project['id'] or plan.get('expected_revision') != project.get('updated'):
                raise OwnerUnavailable('owner_revision_changed')
            report.update(owner_available=True, owner_instance_id=record['instance_id'])
            for key in ('can_continue', 'content_ready', 'reason', 'requires_review', 'errors',
                        'next_action', 'remaining_steps', 'completed_steps', 'expected_revision'):
                if key in plan:
                    report[key] = plan[key]
            # Local guards remain authoritative for automation even if an old
            # owner has not yet loaded the new execution-admission contract.
            admission = _admission(project, jobs, row, monitor_path,
                                    remaining_steps=plan.get('remaining_steps'), now=now)
            _apply_admission(report, admission)
            if admission.get('execution_allowed') is not True:
                report['can_continue'] = False
                report['reason'] = admission.get('blocked_reason') or report['reason']
            if not enqueue or report.get('can_continue') is not True:
                return report
            # Verify ownership again immediately before mutation. The POST also
            # carries the identity for the owner's atomic admission check.
            fresh = load(data_dir, now=now)
            if fresh['instance_id'] != record['instance_id']:
                raise OwnerUnavailable('owner_replaced')
            validate_live_owner(fresh, _request(client, fresh, 'GET', '/api/studio/owner'), now=now)
            current_row, current_project, current_jobs = read_store(data_dir, identifier)
            if current_project.get('updated') != report['expected_revision']:
                raise OwnerUnavailable('owner_revision_changed')
            current_admission = _admission(current_project, current_jobs, current_row, monitor_path,
                                         remaining_steps=plan.get('remaining_steps'), now=now)
            if current_admission.get('execution_allowed') is not True:
                _apply_admission(report, current_admission)
                report['can_continue'] = False
                report['reason'] = current_admission.get('blocked_reason') or 'Quy trình chưa được phép tiếp tục.'
                return report
            result = _request(client, fresh, 'POST', '/api/studio/jobs/' + identifier + '/continue-pipeline',
                              {'expected_revision': report['expected_revision'], 'request_id': uuid.uuid4().hex,
                               'owner_instance_id': fresh['instance_id'], 'automatic': True})
            _identifier(result.get('id'))
            if result.get('project_id') != project['id'] or result.get('state') not in ('queued', 'running'):
                raise OwnerUnavailable('owner_response_invalid')
            report.update(enqueued=True, job_id=result['id'], state=result['state'])
            for key in ('recovery_from', 'continuation_from'):
                if key in result:
                    report[key] = result[key]
            return report
        finally:
            if own_client:
                client.close()
    except OwnerUnavailable as exc:
        report.update(can_continue=False, owner_available=False, owner_error=str(exc))
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--enqueue', action='store_true')
    args = parser.parse_args(argv)
    try:
        report = inspect_or_continue(args.job_id, enqueue=args.enqueue)
    except ValueError as exc:
        report = {'enqueued': False, 'can_continue': False, 'reason': str(exc)}
    print(json.dumps(report, ensure_ascii=False))
    return 0 if not args.enqueue or report.get('enqueued') else 2


if __name__ == '__main__':
    raise SystemExit(main())
