"""Read-only admission for provider work, shared by plans, queue and dispatch.

Content/checkpoint validity belongs to the pipeline planner. This module decides
whether otherwise valid work may execute *now*. It never reads credentials,
contacts a provider, modifies the monitor, or imports the desktop store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import time
import ipaddress
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parent
PROTECTED_PROJECT_ID = '00000000000000000000000000000001'
MAX_MONITOR_BYTES = 128 * 1024
QUOTA_SCOPES = {'unknown', 'daily', 'requests_per_minute', 'tokens_per_minute'}
TRANSIENT_STATUSES = {408, 500, 502, 503, 504}
CONTENT_KINDS = {'ocr', 'asr', 'translate', 'optimize_spoken', 'tts', 'clean', 'pipeline', 'render', 'auto_subtitles'}
BANGKOK = timezone(timedelta(hours=7))


class AdmissionError(ValueError):
    """A safe status object may be returned in a local API error response."""
    def __init__(self, report):
        self.report = report
        super().__init__(report['blocked_reason'])


def require_execution_contract(snapshot, current, kind, *, remaining_steps=None):
    """A saved connection/model change stops the old immutable job before send.

    This guard applies only to provider work, never to an explicit connection
    probe. Model changes take effect through a new revision-checked continuation.
    """
    if not provider_required(snapshot, kind, remaining_steps):
        return
    expected = snapshot.get('params') or {}
    actual = (current or {}).get('params') or {}
    expected_connection = _connection(expected.get('provider'), expected.get('router9_base_url'))
    actual_connection = _connection(actual.get('provider'), actual.get('router9_base_url'))
    director_changed = (kind in {'ocr', 'auto_subtitles', 'pipeline'} and
                        expected.get('subtitle_automation') in {'gemini', 'router9'} and
                        expected.get('subtitle_automation') != actual.get('subtitle_automation'))
    if (not current or current.get('id') != snapshot.get('id') or not expected_connection or
            not actual_connection or expected_connection != actual_connection or
            director_changed or not expected.get('translation_model') or
            expected.get('translation_model') != actual.get('translation_model')):
        raise AdmissionError({'execution_allowed': False, 'blocked_code': 'execution_model_changed',
            'blocked_reason': 'Kết nối hoặc model đã được lưu khác với tác vụ đang chạy. Đã dừng trước lượt API kế tiếp; '
                              'giữ checkpoint và tiếp tục bằng cấu hình mới.',
            'expected_connection': list(expected_connection) if expected_connection else None,
            'current_connection': list(actual_connection) if actual_connection else None,
            'expected_model': expected.get('translation_model'), 'current_model': actual.get('translation_model'),
            'next_action': 'continue_with_saved_model'})


def default_monitor_path():
    # Isolated tests must never inherit the production monitor just because the
    # source checkout is shared. No core import/store creation is needed here.
    data = Path(os.environ.get('VIETDUB_DATA_DIR', ROOT / 'data')).resolve()
    return ROOT / 'build' / 'npk-pipeline-monitor-state.json' if data == (ROOT / 'data').resolve() else data / 'npk-pipeline-monitor-state.json'


def _stamp(value):
    return value if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 253402214400 else None


def provider_required(project, kind, remaining_steps=None):
    """Conservatively identify operations which can actually send a request."""
    params = project.get('params') or {}
    director = params.get('subtitle_automation', 'manual') in ('gemini', 'router9')
    if remaining_steps is not None:
        steps = set(remaining_steps)
        return 'translate' in steps or (director and bool(steps & {'ocr', 'auto_subtitles'}))
    if kind in {'translate', 'optimize_spoken'}:
        return True
    if kind in {'ocr', 'auto_subtitles'}:
        return director
    if kind == 'pipeline':
        return params.get('pipeline_translate', True) is not False or (director and params.get('pipeline_extract', 'existing') in {'ocr', 'existing'})
    return False


def _connection(provider, endpoint=None):
    """Only public connection identity; never model, key, or credential identity.

    Keep this read-only module independent of provider clients/vault imports.
    Invalid or missing history is an unknown connection, never an exemption.
    """
    if provider not in ('gemini', 'deepseek', 'router9'):
        return None
    if provider != 'router9':
        return (provider, None)
    value = 'http://127.0.0.1:20128/v1' if endpoint in (None, '') else endpoint
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\x00-\x20\x7f\\]', value):
        return None
    try:
        parts = urlsplit(value)
        host, port = parts.hostname, parts.port
        if (parts.scheme not in ('http', 'https') or not host or parts.username is not None or
                parts.password is not None or '?' in value or '#' in value or
                port is not None and not 1 <= port <= 65535):
            return None
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == 'localhost'
        path = parts.path.rstrip('/') or '/v1'
        if (parts.scheme == 'http' and not loopback or
                not re.fullmatch(r'(?:/[a-zA-Z0-9_-][a-zA-Z0-9._-]*)+', path) or
                any(segment in ('.', '..') for segment in path.split('/'))):
            return None
        return (provider, urlunsplit((parts.scheme, parts.netloc, path, '', '')))
    except (ValueError, UnicodeError):
        return None


def _required_connections(project, kind, remaining_steps=None):
    if not provider_required(project, kind, remaining_steps):
        return set()
    params = project.get('params') or {}
    steps = set(remaining_steps) if remaining_steps is not None else None
    connections = set()
    if (steps is not None and 'translate' in steps or steps is None and
            (kind in {'translate', 'optimize_spoken'} or
             kind == 'pipeline' and params.get('pipeline_translate', True) is not False)):
        connections.add(_connection(params.get('provider'), params.get('router9_base_url')))
    director = params.get('subtitle_automation', 'manual')
    if director in ('gemini', 'router9') and (
            steps is not None and steps & {'ocr', 'auto_subtitles'} or steps is None and
            (kind in {'ocr', 'auto_subtitles'} or kind == 'pipeline' and
             params.get('pipeline_extract', 'existing') in {'ocr', 'existing'})):
        connections.add(_connection(director, params.get('router9_base_url')))
    return connections or {None}


def _public_connection(connection):
    if connection is None:
        return {'provider': 'unknown'}
    provider, endpoint = connection
    return {'provider': provider, **({'endpoint': endpoint} if endpoint else {})}


def _overlap(left, right):
    return None in left or None in right or bool(left & right)


def _read_monitor(path, project_id, protected_project_id):
    """Return only allowlisted metadata, failing closed for the protected job."""
    protects_project = project_id == protected_project_id
    try:
        if not path.exists():
            return ({}, 'monitor_missing') if project_id == protected_project_id else ({}, None)
        if path.is_symlink() or path.stat().st_size > MAX_MONITOR_BYTES:
            raise ValueError('unsafe monitor')
        with path.open('rb') as stream:
            payload = stream.read(MAX_MONITOR_BYTES + 1)
        if len(payload) > MAX_MONITOR_BYTES:
            raise ValueError('oversized monitor')
        row = json.loads(payload.decode('utf-8'))
        if not isinstance(row, dict) or not isinstance(row.get('project_id'), str) or not row['project_id']:
            raise ValueError('invalid monitor identity')
        if row['project_id'] != project_id:
            return {}, None
        protects_project = True
        if type(row.get('audit_hold')) is not bool:
            raise ValueError('invalid hold')
        scope = row.get('quota_scope', 'unknown')
        if scope not in QUOTA_SCOPES:
            raise ValueError('invalid quota scope')
        result = {'project_id': project_id, 'audit_hold': row['audit_hold'], 'quota_scope': scope}
        if 'quota_connection' in row:
            identity = row['quota_connection']
            if not isinstance(identity, dict) or set(identity) not in ({'provider'}, {'provider', 'endpoint'}):
                raise ValueError('invalid quota connection')
            connection = _connection(identity.get('provider'), identity.get('endpoint'))
            if connection is None or _public_connection(connection) != identity:
                raise ValueError('invalid quota connection')
            result['quota_connection'] = identity
        for key in ('last_attempt_utc', 'earliest_retry_utc', 'quota_recovery_confirmed_at'):
            if key in row:
                value = _stamp(row[key])
                if value is None:
                    raise ValueError('invalid monitor timestamp')
                result[key] = value
        if 'last_http_status' in row:
            if type(row['last_http_status']) is not int or not 100 <= row['last_http_status'] <= 599:
                raise ValueError('invalid http status')
            result['last_http_status'] = row['last_http_status']
        for key in ('latest_job_id', 'source_revision'):
            if key in row:
                if row[key] is not None and (not isinstance(row[key], str) or len(row[key]) > 128):
                    raise ValueError('invalid monitor revision')
                result[key] = row[key]
        # This ledger survives wakeups. Jobs supply an independent lower bound
        # and manual attempts supply cooldown, but do not consume automatic cap.
        counts = row.get('automatic_attempts_by_local_date', {})
        if (not isinstance(counts, dict) or len(counts) > 3660 or
                any(not isinstance(day, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', day) or
                    type(count) is not int or not 0 <= count <= 100000 for day, count in counts.items())):
            raise ValueError('invalid automatic counter')
        result['automatic_attempts_by_local_date'] = counts
        return result, None
    except (OSError, UnicodeError, ValueError, RecursionError, TypeError):
        # A malformed unknown project marker cannot safely exempt the protected
        # production project. Unrelated projects retain ordinary job guards.
        return ({}, 'monitor_invalid') if protects_project else ({}, None)


def _matching(project, row):
    return (isinstance(row, dict) and row.get('project_id') == project.get('id') and
            row.get('source_revision') == project.get('source_revision'))


def _attempt_time(row):
    # queued-but-never-started work is not an API attempt. Prefer completion so
    # timeout/slow manual attempts cannot shorten a cooldown to their start.
    if row.get('state') == 'queued' and not _stamp(row.get('started')):
        return 0
    return next((_stamp(row.get(key)) for key in ('ended', 'started', 'created')
                 if _stamp(row.get(key)) is not None), 0)


def _job_provider_required(row):
    return provider_required({'params': row.get('configuration') or {}}, row.get('kind'))


def _job_connections(row):
    configuration = row.get('configuration')
    params = configuration if isinstance(configuration, dict) else {}
    kind = row.get('kind')
    # Worker writes a canonical provider failure stage. A pipeline can use a
    # different connection in its director and translator, so do not assign a
    # translation failure to its director just because both were configured.
    if kind == 'pipeline' and row.get('failure_stage') in {'translate', 'optimize_spoken', 'ocr', 'auto_subtitles'}:
        kind = row['failure_stage']
    connections = _required_connections({'params': params}, kind)
    if kind in {'ocr', 'auto_subtitles', 'pipeline'} and 'subtitle_automation' not in params:
        connections.add(None)
    return connections


def _is_transient(row):
    return row.get('http_status') in TRANSIENT_STATUSES or row.get('failure_code') in {
        'provider_timeout', 'provider_connection', 'provider_network', 'provider_transport', 'provider_unavailable'}


def admission_status(project, jobs, *, kind='pipeline', remaining_steps=None, now=None,
                     automatic=False, current_job_id=None, monitor_path=None,
                     protected_project_id=PROTECTED_PROJECT_ID):
    """Decide without mutation; use the same report at GET, POST and dispatch.

    ``current_job_id`` excludes the owner's queued/running row at dispatch.
    ``automatic`` controls only the daily automatic-attempt limit. All provider
    attempts, including manual ones, participate in cooldown and quota barriers.
    ``quota_recovery_confirmed_at`` is an explicit local recovery confirmation;
    a guessed cooldown/reset time is not proof of quota recovery.
    """
    now = time.time() if now is None else now
    required = provider_required(project, kind, remaining_steps)
    required_connections = _required_connections(project, kind, remaining_steps)
    report = {'execution_allowed': True, 'provider_required': required,
              'blocked_code': None, 'blocked_reason': '', 'earliest_retry_utc': None,
              'quota_scope': None, 'latest_job_id': None, 'automatic_attempts_today': 0,
              'required_connections': sorted((_public_connection(item) for item in required_connections),
                                             key=lambda item: (item['provider'], item.get('endpoint', ''))),
              'quota_connection': None, 'quota_connection_applies': None}

    def blocked(code, message):
        report.update(execution_allowed=False, blocked_code=code, blocked_reason=message)
        return report

    if _stamp(now) is None:
        return blocked('clock_invalid', 'Không xác định được thời điểm kiểm tra an toàn; chưa chạy tác vụ.')
    jobs = list(jobs)
    current_job = next((row for row in jobs if _matching(project, row) and row.get('id') == current_job_id), None)
    matching = [row for row in jobs if _matching(project, row) and row.get('id') != current_job_id]
    if any(row.get('state') in {'queued', 'running'} and row.get('kind') in CONTENT_KINDS for row in matching):
        return blocked('project_busy', 'Dự án đang có tác vụ chờ hoặc chạy. Chờ hoàn tất trước khi tạo lượt tiếp theo.')
    if not required:
        return report
    monitor, error = _read_monitor(Path(monitor_path) if monitor_path is not None else default_monitor_path(),
                                   project.get('id'), protected_project_id)
    if error:
        return blocked(error, 'Thiếu hoặc hỏng trạng thái bảo vệ quota của dự án; chưa gọi API. Kiểm tra trạng thái theo dõi.')
    if 'source_revision' in monitor and monitor['source_revision'] != project.get('source_revision'):
        return blocked('monitor_source_changed', 'Video nguồn không khớp trạng thái theo dõi; cần kiểm tra trước khi gọi API.')
    scoped = 'quota_connection' in monitor
    all_provider_jobs = [row for row in matching if
                         (_job_connections(row) if scoped else _job_provider_required(row)) and _attempt_time(row)]
    quota_connection = None
    if scoped:
        identity = monitor['quota_connection']
        quota_connection = _connection(identity['provider'], identity.get('endpoint'))
        report['quota_connection'] = identity
        report['quota_connection_applies'] = _overlap(required_connections, {quota_connection})
    provider_jobs = [row for row in all_provider_jobs if not scoped or _overlap(
        required_connections, _job_connections(row))]
    quota_monitor = monitor if not scoped or report['quota_connection_applies'] else {}
    provider_jobs.sort(key=lambda row: (_stamp(row.get('created')) or 0, _attempt_time(row)))
    latest = provider_jobs[-1] if provider_jobs else None
    report['latest_job_id'] = latest.get('id') if latest else quota_monitor.get('latest_job_id')
    latest_time = max([_attempt_time(row) for row in provider_jobs] + [quota_monitor.get('last_attempt_utc', 0)])
    quota_jobs = [row for row in provider_jobs if row.get('http_status') == 429 or row.get('failure_code') == 'provider_quota']
    recovery = quota_monitor.get('quota_recovery_confirmed_at')

    def recovered(row):
        connections = _job_connections(row)
        return (recovery is not None and 0 < _attempt_time(row) <= recovery <= now and
                (not scoped or connections == {quota_connection}))

    pending_quota_jobs = [row for row in quota_jobs if not recovered(row)] if scoped else quota_jobs
    own_quota_time = max([_attempt_time(row) for row in quota_jobs if
        _job_connections(row) == {quota_connection}] +
        [quota_monitor.get('last_attempt_utc', 0) if quota_monitor.get('last_http_status') == 429 else 0])
    own_confirmed = recovery is not None and 0 < own_quota_time <= recovery <= now
    quota = max(pending_quota_jobs or quota_jobs, key=_attempt_time, default=None)
    quota_time = _attempt_time(quota) if quota else 0
    if quota_monitor.get('last_http_status') == 429:
        quota_time = max(quota_time, quota_monitor.get('last_attempt_utc', 0))
    confirmed = (recovery is not None and quota_time > 0 and quota_time <= recovery <= now and
                 (not scoped or not pending_quota_jobs))
    unresolved_quota = quota_time > 0 and not confirmed
    scope = quota.get('quota_scope', 'unknown') if quota else quota_monitor.get('quota_scope')
    if scope not in QUOTA_SCOPES:
        scope = 'unknown' if unresolved_quota else None
    # A later 503 must not silently replace a still unresolved 429. Monitor
    # unknown/daily is deliberately stronger than a stale rate-limit hint.
    if (unresolved_quota and quota_monitor.get('quota_scope') in {'unknown', 'daily'} and
            (not scoped or not own_confirmed)):
        scope = quota_monitor['quota_scope']
    report['quota_scope'] = scope
    delay = 7200 if unresolved_quota else 1800 if latest and _is_transient(latest) else 0
    # An old Retry-After belongs to its own response, not the newest attempt.
    # Otherwise one historical delay would restart on every later manual run.
    retry_deadline = max((_attempt_time(row) + (_stamp(row.get('retry_after')) or 0)
                          for row in provider_jobs if row.get('retry_after') is not None and
                          (_is_transient(row) or row.get('http_status') == 429)), default=0)
    earliest = max(latest_time + delay if delay else 0, retry_deadline)
    earliest = max(earliest, quota_monitor.get('earliest_retry_utc', 0))
    report['earliest_retry_utc'] = earliest or None
    local_day = datetime.fromtimestamp(now, BANGKOK).date().isoformat()
    job_count = sum(1 for row in all_provider_jobs if
        (row.get('automatic') is True or row.get('execution_origin') == 'automation') and
        datetime.fromtimestamp(_stamp(row.get('started')) or _stamp(row.get('created')) or _attempt_time(row),
                               BANGKOK).date().isoformat() == local_day)
    monitor_count = monitor.get('automatic_attempts_by_local_date', {}).get(local_day, 0)
    # CLI reserves an automatic attempt in the monitor as soon as the owning
    # server accepts the queued row. Dispatch/child sends must not charge that
    # *same* approved attempt again. Exempt only the exact current active owner;
    # another latest row, manual work or a previous day cannot claim a credit.
    if (current_job and current_job.get('state') in {'queued', 'running'} and
            (current_job.get('automatic') is True or current_job.get('execution_origin') == 'automation') and
            monitor.get('latest_job_id') == current_job_id and monitor_count > 0):
        current_start = _stamp(current_job.get('started')) or _stamp(current_job.get('created'))
        if current_start is not None and datetime.fromtimestamp(current_start, BANGKOK).date().isoformat() == local_day:
            monitor_count -= 1
    report['automatic_attempts_today'] = max(job_count, monitor_count)
    if monitor.get('audit_hold'):
        return blocked('audit_hold', 'Dự án đang có khóa kiểm toán quota API chưa hoàn tất. Khóa này áp dụng cho mọi kết nối; đổi nhà cung cấp hoặc model không gỡ khóa. Giữ mọi checkpoint đã có.')
    monitor_quota_barrier = (quota_monitor.get('quota_scope') in {'unknown', 'daily'} and
                             not (own_confirmed if scoped else confirmed))
    if (unresolved_quota and scope in {'unknown', 'daily'}) or monitor_quota_barrier:
        return blocked('quota_recovery_required', 'Chưa xác nhận quota API đã phục hồi. Kiểm tra hạn mức của dự án trước khi tiếp tục; không tự đổi key hoặc model.')
    if earliest > now:
        return blocked('provider_cooldown', 'Đang trong thời gian chờ sau lượt gọi API gần nhất; giữ checkpoint và chờ đủ thời gian.')
    # Counts stay project-wide. Changing connection cannot buy more automatic
    # attempts; an unresolved 429 elsewhere still selects the stricter cap.
    other_quota = scoped and any((row.get('http_status') == 429 or row.get('failure_code') == 'provider_quota')
                                 and not recovered(row) for row in all_provider_jobs)
    monitor_429 = scoped and monitor.get('last_http_status') == 429 and not (
        _stamp(monitor.get('quota_recovery_confirmed_at')) is not None and
        0 < monitor.get('last_attempt_utc', 0) <= monitor['quota_recovery_confirmed_at'] <= now)
    cap = 3 if unresolved_quota or other_quota or monitor_429 else 4
    if automatic and report['automatic_attempts_today'] >= cap:
        tomorrow = (datetime.fromtimestamp(now, BANGKOK) + timedelta(days=1)).date()
        midnight = datetime.combine(tomorrow, datetime.min.time(), BANGKOK).timestamp()
        report['earliest_retry_utc'] = max(earliest, midnight)
        return blocked('automatic_daily_limit', 'Đã đạt số lượt tiếp tục tự động trong ngày. Giữ checkpoint và chờ ngày tiếp theo.')
    return report


def require_admission(project, jobs, **options):
    report = admission_status(project, jobs, **options)
    if not report['execution_allowed']:
        raise AdmissionError(report)
    return report
