"""Durable counters and admission for translation HTTP attempts, never secrets.

Request attempts include sends that may fail before reaching the provider.
Reported token usage is distinct from billed cost and unknown/failed usage.
"""
import json
import hashlib
import math
import os
from pathlib import Path
import re
import sqlite3
from contextlib import closing, contextmanager
import time
import uuid

NAME = 'api-usage.json'
USAGE_KEYS = ('input_tokens', 'output_tokens', 'total_tokens', 'thinking_tokens', 'cached_input_tokens')
LINEAGE_NAME = 'api-usage-lineage.sqlite3'
_IDENTITY = ('schema_version', 'root_id', 'project_id', 'source_identity', 'source_revision',
             'provider', 'target_language', 'endpoint')
_RECORD_KEYS = {'ordinal', 'at', 'cue_count', 'first_cue', 'last_cue', 'prompt_bytes', 'attempt',
                'http_status', 'elapsed_seconds', 'quota_scope', 'retry_after', 'usage', 'lineage_ordinal',
                'configured_model', 'response_model', 'response_model_status',
                'response_model_matches_configured'}


def _model_id(value):
    from providers import safe_model
    try:
        return isinstance(value, str) and safe_model(value) == value
    except ValueError:
        return False


def _valid_id(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{32}', value) is not None


def _validate_records(records):
    if not isinstance(records, list) or len(records) > 50000:
        raise ValueError('Nhật ký lượt gửi API không hợp lệ.')
    for i, record in enumerate(records):
        if (not isinstance(record, dict) or set(record) - _RECORD_KEYS or record.get('ordinal') != i + 1 or
                type(record.get('attempt')) is not int or not 1 <= record['attempt'] <= 5 or
                any(type(record.get(key)) not in (int, float) or not math.isfinite(record[key]) or record[key] < 0
                    for key in ('at', 'cue_count', 'first_cue', 'last_cue', 'prompt_bytes')) or
                ('usage' in record and (not isinstance(record['usage'], dict) or
                 set(record['usage']) - set(USAGE_KEYS) or any(type(n) is not int or n < 0 for n in record['usage'].values()))) or
                ('quota_scope' in record and record['quota_scope'] not in ('daily','requests_per_minute','tokens_per_minute','unknown')) or
                any(type(record.get(key)) not in (int, float) or not math.isfinite(record[key]) or record[key] < 0
                    for key in ('http_status','elapsed_seconds','retry_after') if key in record) or
                ('lineage_ordinal' in record and (type(record['lineage_ordinal']) is not int or record['lineage_ordinal'] < 1))):
            raise ValueError('Nhật ký lượt gửi API bị lỗi; giữ checkpoint và chưa gọi dịch tiếp.')
        if (any(not _model_id(record[key]) for key in ('configured_model', 'response_model') if key in record) or
                ('response_model_status' in record and record['response_model_status'] not in ('missing', 'invalid', 'reported')) or
                ('response_model' in record and record.get('response_model_status') != 'reported') or
                (record.get('response_model_status') == 'reported' and 'response_model' not in record) or
                ('response_model_matches_configured' in record and
                 (type(record['response_model_matches_configured']) is not bool or
                  'configured_model' not in record or 'response_model' not in record or
                  record['response_model_matches_configured'] != (record['configured_model'] == record['response_model'])))):
            raise ValueError('Nhật ký danh tính model không hợp lệ; giữ checkpoint và chưa gọi dịch tiếp.')
    return records


def _ledger(path, provider, model, endpoint=None):
    """Validate the complete safe ledger before using any historical counts."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024**2:
        raise ValueError('Nhật ký lượt gửi API không hợp lệ; giữ checkpoint và chưa gọi dịch tiếp.')
    saved = json.loads(path.read_text(encoding='utf-8'))
    records, summary = saved.get('requests'), saved.get('summary')
    if (saved.get('schema_version') != 1 or saved.get('provider') != provider or saved.get('model') != model or
            not isinstance(records, list) or not isinstance(summary, dict) or len(records) > 50000 or
            type(summary.get('request_limit')) is not int or not 1 <= summary['request_limit'] <= 50000 or
            type(summary.get('baseline_requests')) is not int or summary['baseline_requests'] < 1 or
            summary.get('request_attempts') != len(records)):
        raise ValueError('Nhật ký lượt gửi API không khớp tác vụ; chưa gọi dịch tiếp.')
    if provider == 'router9':
        from providers import router_base_url
        if saved.get('endpoint') != router_base_url(endpoint):
            raise ValueError('Endpoint 9Router đã đổi; không dùng lại budget hoặc nhật ký của kết nối khác.')
    _validate_records(records)
    if any(record.get('configured_model', model) != model for record in records):
        raise ValueError('Model đã cấu hình không khớp nhật ký lượt gửi; không tự đổi model trong tác vụ.')
    return saved


def _model_summary(records, configured_model):
    """Bounded public provenance; complete per-attempt history stays in requests."""
    reported = sorted({record['response_model'] for record in records if 'response_model' in record})
    fields = ('ordinal', 'attempt', 'http_status', 'configured_model', 'response_model',
              'response_model_status', 'response_model_matches_configured')
    result = {'configured_model': configured_model, 'response_models': reported[:100],
        'response_model_count': len(reported),
        'response_model_reported_requests': sum('response_model' in record for record in records),
        'response_model_missing_requests': sum('response_model' not in record for record in records),
        'model_mismatch_requests': sum(record.get('response_model_matches_configured') is False for record in records),
        'model_provenance': [{key: record[key] for key in fields if key in record} for record in records[-100:]],
        'model_provenance_total': len(records), 'model_provenance_truncated': len(records) > 100}
    if records and 'response_model' in records[-1]:
        result['response_model'] = records[-1]['response_model']
    return result


def _jobs_root(value):
    if value is None:
        from core import DATA
        value = DATA / 'jobs'
    root = Path(value)
    if root.is_symlink():
        raise ValueError('Thư mục nhật ký API không hợp lệ.')
    return root.resolve()


def _state_path(descriptor, jobs_root):
    if not isinstance(descriptor, dict) or not _valid_id(descriptor.get('root_id')):
        raise ValueError('Danh tính quy trình API không hợp lệ.')
    root = _jobs_root(jobs_root)
    folder = root / descriptor['root_id']
    path = folder / LINEAGE_NAME
    if folder.is_symlink() or path.is_symlink() or folder.resolve().parent != root:
        raise ValueError('Nhật ký API không thuộc thư mục quy trình đã xác minh.')
    return path


def _validate_descriptor(value):
    if (not isinstance(value, dict) or value.get('schema_version') != 1 or
            not _valid_id(value.get('root_id')) or not isinstance(value.get('project_id'), str) or
            not isinstance(value.get('source_identity'), str) or len(value['source_identity']) != 64 or
            value.get('history_status') not in ('known', 'unknown') or
            any(not isinstance(value.get(k), str) or not value[k] for k in ('provider', 'model', 'target_language')) or
            not isinstance(value.get('historical_job_ids'), list) or not isinstance(value.get('unknown_job_ids'), list) or
            any(not _valid_id(v) for v in value['historical_job_ids'] + value['unknown_job_ids'])):
        raise ValueError('Danh tính/bằng chứng budget API không hợp lệ.')
    models = value.get('historical_models', {})
    if (not isinstance(models, dict) or any(not _valid_id(jid) or jid not in value['historical_job_ids'] or
            not isinstance(model, str) or not model for jid, model in models.items())):
        raise ValueError('Danh tính model trong lịch sử API không hợp lệ.')
    if value.get('provider') == 'router9':
        from providers import router_base_url
        if not value.get('endpoint') or value['endpoint'] != router_base_url(value['endpoint']):
            raise ValueError('Nhật ký 9Router thiếu endpoint đã xác minh.')
    baseline, limit = value.get('baseline_requests'), value.get('request_limit')
    if (baseline is None) != (limit is None) or (baseline is not None and
            (type(baseline) is not int or type(limit) is not int or not 1 <= baseline <= limit <= 50000)):
        raise ValueError('Giới hạn API của quy trình không hợp lệ.')
    return value


def _read_state(path):
    """Inspection uses a real read-only SQLite connection; never creates files."""
    if not path.is_file():
        return None
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=20)) as db:
            db.execute('BEGIN')
            rows = db.execute('SELECT value FROM metadata').fetchall()
            if len(rows) != 1:
                raise ValueError('Nhật ký budget API thiếu danh tính quy trình.')
            descriptor = _validate_descriptor(json.loads(rows[0][0]))
            requests = db.execute('SELECT ordinal, job_id, local_ordinal, record FROM requests ORDER BY ordinal').fetchall()
            by_job = {}
            for index, (ordinal, jid, local, encoded) in enumerate(requests):
                record = json.loads(encoded)
                if (not isinstance(record, dict) or not _valid_id(jid) or ordinal != index+1 or
                        record.get('lineage_ordinal') != ordinal or record.get('ordinal') != local):
                    raise ValueError('Danh tính lượt gửi API trong bộ đếm chung bị lỗi.')
                by_job.setdefault(jid, []).append(record)
            for records in by_job.values(): _validate_records(records)
            if type(descriptor.get('reserved_attempts')) is not int or descriptor['reserved_attempts'] != len(requests):
                raise ValueError('Bộ đếm tổng API không khớp các lượt đã dành; không tự reset budget.')
            return descriptor, len(requests), set(by_job)
    except sqlite3.Error as error:
        raise ValueError('Không đọc được bộ đếm API chung; giữ checkpoint và chưa gọi tiếp.') from error


def _could_translate(row):
    if (row.get('state') in ('queued', 'cancelled') and row.get('stage') == 'queued' and
            not row.get('started')):
        return False
    if row.get('kind') == 'translate':
        return True
    if row.get('kind') != 'pipeline' or row.get('configuration', {}).get('pipeline_translate') is False:
        return False
    stages = [str(row.get('stage', ''))] + [str(e.get('stage', '')) for e in row.get('events', []) if isinstance(e, dict)]
    if any('translate' in stage for stage in stages) or 'translate' in (row.get('completed_steps') or []):
        return True
    result = row.get('result') or {}
    completed = result.get('completed_steps', result.get('steps', []))
    if 'translate' in completed:
        return True
    if row.get('state') == 'review' and result.get('needs_review') and any(k in completed for k in ('ocr', 'asr')):
        return False
    if row.get('failure_stage') == 'admission' and (row.get('execution_admission') or {}).get('execution_allowed') is False:
        return False
    stage = str(row.get('stage', ''))
    # Recognition/director failures before translation have no translation sends.
    return not any(step in stage for step in ('queued', 'ocr', 'asr', 'auto_subtitles'))


def _lineage_jobs(root_id, prior_jobs):
    """Follow bounded retry/resume edges as well as pipeline continuations."""
    from collections import deque
    if len(prior_jobs) > 50000:
        raise ValueError('Lịch sử API vượt giới hạn kiểm tra an toàn.')
    children, by_id = {}, {}
    for row in prior_jobs:
        if not isinstance(row, dict):
            raise ValueError('Lịch sử tác vụ API không hợp lệ.')
        jid = row.get('id')
        roots = [row.get(k) for k in ('continuation_from', 'retry_from', 'resume_from')]
        attached = row.get('api_usage_lineage')
        if isinstance(attached, dict): roots.append(attached.get('root_id'))
        if _valid_id(jid):
            if jid in by_id:
                raise ValueError('Danh tính tác vụ bị trùng trong lịch sử API.')
            by_id[jid] = row
        for parent in roots:
            if _valid_id(parent): children.setdefault(parent, []).append(row)
    selected, pending = {}, deque([root_id])
    if root_id in by_id: selected[root_id] = by_id[root_id]
    visited = set()
    while pending:
        parent = pending.popleft()
        if parent in visited: continue
        visited.add(parent)
        for row in children.get(parent, []):
            jid = row.get('id')
            if not _valid_id(jid):
                raise ValueError('Danh tính successor API không hợp lệ.')
            if jid not in selected:
                selected[jid] = row
                pending.append(jid)
    return sorted(selected.values(), key=lambda row: (row.get('created', 0), row['id']))


def prepare_lineage(project, kind, root_id, prior_jobs=(), *, jobs_root=None, segment_indices=None):
    """Build a read-only descriptor. Creation/enqueue never renews its allowance.

    An empty OCR/ASR snapshot defers the initial allowance until translation
    knows its pending rows. Missing historical translation ledgers stay unknown.
    """
    if kind not in ('pipeline', 'translate') or not _valid_id(root_id):
        raise ValueError('Quy trình dịch API không hợp lệ.')
    from subtitle_quality import source_signature
    p = project['params']
    descriptor = {'schema_version': 1, 'root_id': root_id, 'project_id': project['id'],
        'source_identity': source_signature(project), 'source_revision': project.get('source_revision'),
        'provider': p['provider'], 'model': p['translation_model'], 'target_language': p['target_language'],
        'baseline_requests': None, 'request_limit': None, 'history_status': 'known',
        'historical_job_ids': [], 'unknown_job_ids': [], 'historical_models': {}}
    if p['provider'] == 'router9':
        from providers import router_base_url
        descriptor['endpoint'] = router_base_url(p.get('router9_base_url'))
    path = _state_path(descriptor, jobs_root)
    state = _read_state(path)
    if state:
        saved = state[0]
        if any(saved.get(k) != descriptor.get(k) for k in _IDENTITY):
            raise ValueError('Nguồn/cấu hình dịch đã đổi; không tự cấp budget mới cho continuation.')
        descriptor.update(baseline_requests=saved['baseline_requests'], request_limit=saved['request_limit'])
    history = _lineage_jobs(root_id, list(prior_jobs))
    for row in history:
        jid = row.get('id')
        if (not _valid_id(jid) or row.get('project_id') != project['id'] or
                row.get('source_revision') != project.get('source_revision')):
            raise ValueError('Lịch sử API không thuộc đúng quy trình/nguồn.')
        ledger = _jobs_root(jobs_root) / jid / NAME
        if not _could_translate(row) and not ledger.is_file():
            continue
        if not ledger.is_file():
            if state and jid in state[2]:
                # The shared reservation was committed before a JSON write
                # failed. This is measured history, not an unobserved old job.
                continue
            descriptor['unknown_job_ids'].append(jid)
            continue
        # The allowance belongs to the connection, but each old ledger must
        # still match the model captured by its immutable job configuration.
        config = row.get('configuration') or {}
        historical_model = config.get('translation_model')
        if not historical_model:
            historical_model = saved['model'] if state else p['translation_model']
        if config.get('provider', p['provider']) != p['provider']:
            raise ValueError('Nhật ký API thuộc nhà cung cấp khác.')
        if p['provider'] == 'router9' and 'router9_base_url' in config:
            from providers import router_base_url
            if router_base_url(config['router9_base_url']) != descriptor['endpoint']:
                raise ValueError('Nhật ký API thuộc endpoint khác.')
        measured = _ledger(ledger, p['provider'], historical_model, descriptor.get('endpoint'))
        descriptor['historical_job_ids'].append(jid)
        descriptor['historical_models'][jid] = historical_model
        if descriptor['baseline_requests'] is None:
            descriptor.update(baseline_requests=measured['summary']['baseline_requests'],
                              request_limit=measured['summary']['request_limit'])
    if descriptor['unknown_job_ids']:
        descriptor['history_status'] = 'unknown'
    if descriptor['baseline_requests'] is None:
        selected = None if segment_indices is None else set(segment_indices)
        pending = sum((selected is None or i in selected) and
                      (p.get('translation_scope') != 'missing' or not row.get('translated', '').strip())
                      for i, row in enumerate(project.get('segments', [])))
        if pending:
            baseline = math.ceil(pending / int(p.get('translation_batch', 20)))
            descriptor.update(baseline_requests=baseline, request_limit=baseline + max(4, math.ceil(baseline * .25)))
    return _validate_descriptor(descriptor)


def prepare_continuation_lineage(project, kind, root_id, prior_jobs=(), *, jobs_root=None, segment_indices=None):
    """Partition an explicitly changed connection without renewing its allowance.

    Content provenance remains the original continuation family. The budget root
    is stable for each public connection and excludes key/model, so switching
    away and back (or changing a model) cannot reset reservations. Read-only.
    """
    from execution_admission import _connection
    from subtitle_quality import source_signature
    rows = list(prior_jobs)
    family = _lineage_jobs(root_id, rows)
    origin = next((row for row in family if row.get('id') == root_id), None)
    original = (origin or {}).get('configuration') or {}
    original_connection = _connection(original.get('provider'), original.get('router9_base_url'))
    params = project['params']
    wanted = _connection(params.get('provider'), params.get('router9_base_url'))
    if not origin or original_connection is None or wanted is None:
        # Legacy/unknown identity must retain the conservative old history.
        return prepare_lineage(project, kind, root_id, rows, jobs_root=jobs_root, segment_indices=segment_indices)
    partition = root_id
    if wanted != original_connection:
        identity = ['connection-budget-v1', root_id, project['id'], project.get('source_revision'),
                    source_signature(project), list(wanted)]
        partition = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()[:32]
    selected = []
    for row in family:
        if (row.get('project_id') != project['id'] or
                row.get('source_revision') != project.get('source_revision')):
            raise ValueError('Lịch sử API không thuộc đúng quy trình/nguồn.')
        config = row.get('configuration') or {}
        connection = _connection(config.get('provider'), config.get('router9_base_url'))
        if connection is not None and connection != wanted:
            continue
        # Preserve unknown same-family history; never treat it as zero sends.
        item = dict(row)
        item['api_usage_lineage'] = dict(item.get('api_usage_lineage') or {}, root_id=partition)
        selected.append(item)
    result = prepare_lineage(project, kind, partition, selected, jobs_root=jobs_root, segment_indices=segment_indices)
    if partition != root_id:
        result['connection_partition_of'] = root_id
    return result


def ensure_continuation_lineage_folder(descriptor, *, jobs_root=None):
    """Enqueue-only setup after admission; plans never create budget storage."""
    descriptor = _validate_descriptor(descriptor)
    origin = descriptor.get('connection_partition_of')
    if origin is not None:
        if not _valid_id(origin) or origin == descriptor['root_id']:
            raise ValueError('Danh tính phân vùng kết nối API không hợp lệ.')
        path = _state_path(descriptor, jobs_root)
        path.parent.mkdir(exist_ok=True)


def lineage_status(descriptor, *, jobs_root=None):
    """Safe numeric admission summary. Unknown historical requests are not zero."""
    descriptor = _validate_descriptor(descriptor)
    state = _read_state(_state_path(descriptor, jobs_root))
    if state and (any(state[0].get(k) != descriptor.get(k) for k in _IDENTITY) or
                  descriptor['request_limit'] is not None and state[0]['request_limit'] != descriptor['request_limit']):
        raise ValueError('Bộ đếm API không khớp danh tính/giới hạn continuation.')
    count = state[1] if state else sum(len(_ledger(_jobs_root(jobs_root)/jid/NAME,
                    descriptor['provider'], descriptor.get('historical_models', {}).get(jid, descriptor['model']),
                    descriptor.get('endpoint'))['requests']) for jid in descriptor['historical_job_ids'])
    limit = state[0]['request_limit'] if state else descriptor['request_limit']
    unknown = descriptor['history_status'] == 'unknown' or bool(descriptor['unknown_job_ids'])
    remaining = None if limit is None else max(0, limit-count)
    allowed = not unknown and (remaining is None or remaining > 0)
    code = 'history_unknown' if unknown else 'request_budget_exhausted' if remaining == 0 else None
    reason = ('Lịch sử lượt gọi API chưa đầy đủ; không tự coi các lượt cũ là 0.' if unknown else
              'Đã hết budget lượt gọi API của quy trình; continuation không cấp lại lượt.' if remaining == 0 else '')
    return {'history_status': 'unknown' if unknown else 'known', 'history_unknown_jobs': len(descriptor['unknown_job_ids']),
            'lineage_request_attempts': count, 'lineage_request_limit': limit, 'lineage_remaining_requests': remaining,
            'can_send': allowed, 'execution_allowed': allowed, 'blocked_code': code,
            'request_limit': limit, 'remaining_requests': remaining, 'history_unknown': unknown,
            'reason': reason, 'blocked_reason': reason,
            'next_action': 'review_request_history' if unknown else 'review_request_budget' if remaining == 0 else 'continue_pipeline'}


def validate_lineage(descriptor, project):
    from subtitle_quality import source_signature
    descriptor = _validate_descriptor(descriptor)
    wanted = {'project_id': project['id'], 'source_revision': project.get('source_revision'),
              'source_identity': source_signature(project), 'provider': project['params']['provider'],
              'model': project['params']['translation_model'], 'target_language': project['params']['target_language']}
    if project['params']['provider'] == 'router9':
        from providers import router_base_url
        wanted['endpoint'] = router_base_url(project['params'].get('router9_base_url'))
    if any(descriptor.get(k) != value for k, value in wanted.items()):
        raise ValueError('Danh tính budget API không khớp nguồn/cấu hình tác vụ; chưa gửi yêu cầu.')
    return descriptor


class _LineageStore:
    """One committed reservation per HTTP send, shared by every successor."""
    def __init__(self, descriptor, jobs_root, pending_count, batch_size, job_id):
        self.descriptor = _validate_descriptor(dict(descriptor))
        if self.descriptor['history_status'] != 'known' or self.descriptor['unknown_job_ids']:
            from providers import ProviderError
            raise ProviderError('budget', 'Lịch sử lượt gọi API của quy trình chưa được đo đầy đủ. '
                                'Giữ checkpoint và kiểm tra quota/lịch sử trước khi tiếp tục; không tự coi các lượt cũ là 0.')
        self.root = _jobs_root(jobs_root)
        self.path = _state_path(self.descriptor, self.root)
        if not self.path.parent.is_dir():
            raise ValueError('Thiếu thư mục quy trình gốc; không tạo một budget API mới.')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS requests (ordinal INTEGER PRIMARY KEY, job_id TEXT NOT NULL, '
                       'local_ordinal INTEGER NOT NULL, record TEXT NOT NULL, UNIQUE(job_id, local_ordinal))')
            db.execute('CREATE TABLE IF NOT EXISTS job_models (job_id TEXT PRIMARY KEY, model TEXT NOT NULL)')
            rows = db.execute('SELECT value FROM metadata').fetchall()
            if not rows:
                value = dict(self.descriptor)
                value['reserved_attempts'] = 0
                if value['baseline_requests'] is None:
                    baseline = math.ceil(pending_count / batch_size)
                    if baseline < 1:
                        raise ValueError('Chưa có số câu để khởi tạo budget dịch.')
                    value.update(baseline_requests=baseline, request_limit=baseline + max(4, math.ceil(baseline * .25)))
                db.execute('INSERT INTO metadata(value) VALUES (?)', (json.dumps(value, allow_nan=False),))
            saved = self._metadata(db)
            def bind_model(jid, model):
                previous = db.execute('SELECT model FROM job_models WHERE job_id=?', (jid,)).fetchone()
                if previous and previous[0] != model:
                    raise ValueError('Model của tác vụ đã đổi; không dùng lại lượt gửi dưới model khác.')
                db.execute('INSERT OR IGNORE INTO job_models VALUES (?,?)', (jid, model))
            # Migrate old stores without renewing counters or changing the
            # original model binding of an already reserved job.
            for (jid,) in db.execute('SELECT DISTINCT r.job_id FROM requests r LEFT JOIN job_models m '
                                    'ON r.job_id=m.job_id WHERE m.job_id IS NULL').fetchall():
                bind_model(jid, self.descriptor.get('historical_models', {}).get(jid, saved['model']))
            bind_model(job_id, self.descriptor['model'])
            for jid in self.descriptor['historical_job_ids']:
                historical_model = self.descriptor.get('historical_models', {}).get(jid, self.descriptor['model'])
                bind_model(jid, historical_model)
                ledger = _ledger(self.root/jid/NAME, saved['provider'], historical_model, saved.get('endpoint'))
                for record in ledger['requests']:
                    existing = db.execute('SELECT ordinal, record FROM requests WHERE job_id=? AND local_ordinal=?',
                                          (jid, record['ordinal'])).fetchone()
                    if existing:
                        old = json.loads(existing[1])
                        # The DB commits first; a crash can leave the JSON less complete,
                        # but any contradictory field is corrupted historical evidence.
                        if any(old.get(k) != v for k, v in record.items() if k != 'lineage_ordinal'):
                            raise ValueError('Nhật ký API của continuation không khớp bộ đếm chung.')
                        continue
                    ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM requests').fetchone()[0]
                    imported = dict(record, lineage_ordinal=ordinal)
                    db.execute('INSERT INTO requests VALUES (?,?,?,?)',
                               (ordinal, jid, record['ordinal'], json.dumps(imported, allow_nan=False)))
            saved['reserved_attempts'] = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            db.execute('UPDATE metadata SET value=?', (json.dumps(saved, allow_nan=False),))
            self.limit, self.baseline = saved['request_limit'], saved['baseline_requests']
            if db.execute('SELECT COUNT(*) FROM requests').fetchone()[0] >= self.limit:
                from providers import ProviderError
                raise ProviderError('budget', 'Đã hết budget API của quy trình; giữ checkpoint và không đọc key/gửi tiếp.')

    @contextmanager
    def _connect(self):
        if self.path.is_symlink() or self.path.exists() and self.path.stat().st_size > 32 * 1024**2:
            raise ValueError('Nhật ký budget API không hợp lệ; chưa gửi yêu cầu.')
        db = sqlite3.connect(self.path, timeout=20)
        try:
            with db:
                yield db
        finally:
            db.close()

    def _metadata(self, db):
        rows = db.execute('SELECT value FROM metadata').fetchall()
        if len(rows) != 1:
            raise ValueError('Nhật ký budget API thiếu danh tính quy trình.')
        saved = _validate_descriptor(json.loads(rows[0][0]))
        if (any(saved.get(k) != self.descriptor.get(k) for k in _IDENTITY) or
                (self.descriptor['baseline_requests'] is not None and any(saved.get(k) != self.descriptor.get(k)
                    for k in ('baseline_requests', 'request_limit'))) or saved['history_status'] != 'known'):
            raise ValueError('Budget API đã đổi danh tính/giới hạn; không tự tiếp tục.')
        if (type(saved.get('reserved_attempts')) is not int or
                saved['reserved_attempts'] != db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]):
            raise ValueError('Bộ đếm tổng API bị lỗi; không tự cấp lại lượt đã mất.')
        return saved

    def records(self, job_id):
        with self._connect() as db:
            db.execute('BEGIN')
            self._metadata(db)
            return _validate_records([json.loads(row[0]) for row in db.execute(
                'SELECT record FROM requests WHERE job_id=? ORDER BY local_ordinal', (job_id,))])

    def summary(self):
        with self._connect() as db:
            db.execute('BEGIN')
            saved = self._metadata(db)
            count = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            return {'lineage_request_attempts': count, 'lineage_request_limit': saved['request_limit'],
                    'lineage_remaining_requests': max(0, saved['request_limit']-count),
                    'history_unknown_jobs': 0}

    def reserve(self, job_id, record):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            saved = self._metadata(db)
            count = db.execute('SELECT COUNT(*) FROM requests').fetchone()[0]
            if count >= saved['request_limit']:
                from providers import ProviderError
                raise ProviderError('budget', f'Quy trình đã dùng {count}/{saved["request_limit"]} lượt gửi API. '
                    'Continuation giữ nguyên budget và checkpoint; cần kiểm tra trước khi cấp thêm lượt.')
            local = db.execute('SELECT COALESCE(MAX(local_ordinal),0)+1 FROM requests WHERE job_id=?', (job_id,)).fetchone()[0]
            if local != record['ordinal']:
                raise ValueError('Hai bộ xử lý đang dùng cùng tác vụ; chưa gửi yêu cầu API trùng.')
            ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0)+1 FROM requests').fetchone()[0]
            record = dict(record, lineage_ordinal=ordinal)
            db.execute('INSERT INTO requests VALUES (?,?,?,?)', (ordinal, job_id, local, json.dumps(record, allow_nan=False)))
            saved['reserved_attempts'] = count + 1
            db.execute('UPDATE metadata SET value=?', (json.dumps(saved, allow_nan=False),))
            return record

    def update(self, job_id, record):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._metadata(db)
            updated = db.execute('UPDATE requests SET record=? WHERE job_id=? AND local_ordinal=? AND ordinal=?',
                                 (json.dumps(record, allow_nan=False), job_id, record['ordinal'], record['lineage_ordinal']))
            if updated.rowcount != 1:
                raise ValueError('Thiếu lượt gửi đã được ghi bền vững; giữ checkpoint và dừng.')


def read_summary(folder):
    path = Path(folder) / NAME
    try:
        if path.is_symlink() or path.stat().st_size > 8 * 1024**2:
            return None
        document = json.loads(path.read_text(encoding='utf-8'))
        data = document['summary']
        counts = ('request_attempts', 'request_limit', 'baseline_requests', 'responses_received',
                  'usage_reported_requests', 'usage_unknown_requests', 'completed_cues',
                  'lineage_request_attempts', 'lineage_request_limit', 'lineage_remaining_requests', 'history_unknown_jobs',
                  'reused_cues', 'newly_completed_cues', 'remaining_cues')
        result = {key: data[key] for key in counts if type(data.get(key)) is int and data[key] >= 0}
        tokens = data.get('reported_tokens') or {}
        result['reported_tokens'] = {key: tokens[key] for key in USAGE_KEYS
                                    if type(tokens.get(key)) is int and tokens[key] >= 0}
        result['cost_verification'] = 'unverified'
        if _model_id(document.get('model')) and isinstance(document.get('requests'), list):
            result.update(_model_summary(_validate_records(document['requests']), document['model']))
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


class TranslationUsage:
    def __init__(self, folder, pending_count, batch_size, provider, model, *, lineage=None, job_id=None, jobs_root=None, endpoint=None):
        self.folder = Path(folder) if folder is not None else None
        baseline = math.ceil(pending_count / batch_size)
        # Shared overhead, rather than a separate allowance at every retry layer.
        self.limit = baseline + max(4, math.ceil(baseline * .25))
        self.records = []
        self.provider, self.model = provider, model
        self._configured_model = model
        self.endpoint = None
        if provider == 'router9':
            from providers import router_base_url
            self.endpoint = router_base_url(endpoint if endpoint is not None else (lineage or {}).get('endpoint'))
        self.baseline, self.batch = baseline, {}
        self.completed = 0
        self.reused_cues, self.eligible_cues, self.inherited_completed = 0, pending_count, 0
        self.lineage, self.job_id = None, job_id
        path = self.folder / NAME if self.folder is not None else None
        if path is not None and path.exists():
            saved = _ledger(path, provider, model, self.endpoint)
            records, summary = saved['requests'], saved['summary']
            self.records, self.limit, self.baseline = records, summary['request_limit'], summary['baseline_requests']
        if lineage is not None:
            if (self.folder is None or not _valid_id(job_id) or self.folder.name != job_id or
                    lineage.get('provider') != provider or lineage.get('model') != model or
                    lineage.get('endpoint') != self.endpoint):
                raise ValueError('Tác vụ không khớp nhật ký API của quy trình.')
            self.lineage = _LineageStore(lineage, jobs_root if jobs_root is not None else self.folder.parent,
                                         pending_count, batch_size, job_id)
            global_records = self.lineage.records(job_id)
            if self.records and (len(self.records) > len(global_records) or any(
                    any(global_records[i].get(k) != v for k, v in record.items()) for i, record in enumerate(self.records))):
                raise ValueError('Nhật ký tác vụ không khớp các lượt gửi đã dành trong budget chung.')
            self.records, self.limit, self.baseline = global_records, self.lineage.limit, self.lineage.baseline
        self.persist()

    def set_batch(self, indices, prompt):
        self.batch = {'cue_count': len(indices), 'first_cue': min(indices) + 1,
                      'last_cue': max(indices) + 1, 'prompt_bytes': len(prompt.encode('utf-8'))}

    def summary(self):
        usage = {key: sum(r.get('usage', {}).get(key, 0) for r in self.records) for key in USAGE_KEYS}
        result = {'request_attempts': len(self.records), 'request_limit': self.limit,
                'baseline_requests': self.baseline, 'responses_received': sum('http_status' in r for r in self.records),
                'usage_reported_requests': sum(bool(r.get('usage')) for r in self.records),
                'usage_unknown_requests': sum(not r.get('usage') for r in self.records),
                'reported_tokens': usage, 'completed_cues': self.completed,
                'reused_cues': self.reused_cues, 'newly_completed_cues': max(0, self.completed-self.inherited_completed),
                'remaining_cues': max(0, self.eligible_cues-self.completed),
                'cost_verification': 'unverified'}
        if self.lineage is not None:
            result.update(self.lineage.summary())
        result.update(_model_summary(self.records, self._configured_model))
        return result

    def persist(self):
        if self.folder is None:
            return
        path = self.folder / NAME
        document = {'schema_version': 1, 'provider': self.provider, 'model': self.model,
                    'summary': self.summary(), 'requests': self.records, 'updated_at': time.time()}
        if self.endpoint is not None:
            document['endpoint'] = self.endpoint
        pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with pending.open('x', encoding='utf-8') as out:
                json.dump(document, out, ensure_ascii=False, indent=2, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)

    def observe(self, event):
        phase = event.get('event')
        if (self.model != self._configured_model or
                event.get('configured_model', self._configured_model) != self._configured_model):
            from providers import ProviderError
            raise ProviderError('model_changed', 'Model của yêu cầu đã đổi giữa tác vụ. Dừng trước khi gửi tiếp; giữ checkpoint và chọn lại đúng model.',
                                output_reason='configured_model_changed')
        if phase == 'before_send':
            if self.lineage is None and len(self.records) >= self.limit:
                from providers import ProviderError
                raise ProviderError('budget', f'Đã dùng {len(self.records)}/{self.limit} lượt gửi API trong tác vụ này; '
                    f'kế hoạch ban đầu là {self.baseline} lượt. Dừng để bảo vệ quota, giữ {self.completed} câu đã dịch. '
                    'Kiểm tra phản hồi, kích thước nhóm và hạn mức trước khi tiếp tục.')
            record = {'ordinal': len(self.records) + 1, 'at': time.time(), **self.batch, 'attempt': event['attempt'],
                      'configured_model': self._configured_model, 'response_model_status': 'missing'}
            if self.lineage is not None:
                record = self.lineage.reserve(self.job_id, record)
            self.records.append(record)
        elif phase == 'response' and self.records:
            record = self.records[-1]
            record['http_status'] = event['http_status']
            elapsed = event.get('elapsed_seconds')
            if isinstance(elapsed, (int, float)) and math.isfinite(elapsed) and elapsed >= 0:
                record['elapsed_seconds'] = round(elapsed, 3)
            if event.get('quota_scope') in ('daily', 'requests_per_minute', 'tokens_per_minute', 'unknown'):
                record['quota_scope'] = event['quota_scope']
            delay = event.get('retry_after')
            if isinstance(delay, (int, float)) and math.isfinite(delay) and delay >= 0:
                record['retry_after'] = delay
        elif phase == 'usage' and self.records:
            values = event.get('usage') or {}
            self.records[-1]['usage'] = {key: values[key] for key in USAGE_KEYS
                                        if type(values.get(key)) is int and values[key] >= 0}
        elif phase == 'model_provenance' and self.records:
            record = self.records[-1]
            if event.get('attempt') != record['attempt']:
                raise ValueError('Phản hồi model không thuộc lượt gửi đang theo dõi; chưa áp dụng kết quả.')
            status = event.get('response_model_status', 'missing')
            if status not in ('missing', 'invalid', 'reported'):
                raise ValueError('Trạng thái model phản hồi không hợp lệ.')
            if status == 'reported':
                value = event.get('response_model')
                if not _model_id(value):
                    raise ValueError('Model phản hồi không có danh tính hợp lệ.')
                record.update(response_model=value, response_model_status=status,
                              response_model_matches_configured=value == self._configured_model)
            else:
                record['response_model_status'] = status
        else:
            return
        if self.lineage is not None and phase != 'before_send':
            self.lineage.update(self.job_id, self.records[-1])
        self.persist()

    def checkpoint(self, completed):
        self.completed = completed
        self.persist()

    def set_cue_counts(self, reused, eligible, inherited_completed=0):
        self.reused_cues, self.eligible_cues = reused, eligible
        self.inherited_completed = inherited_completed
