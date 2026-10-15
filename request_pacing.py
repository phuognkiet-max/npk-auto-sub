"""Local API ceiling: serialize sends and space starts by at least 12 seconds.

This is a conservative local policy, not a statement of upstream quota. The
bucket deliberately excludes model and credential so changing either cannot
create a fresh allowance. No prompt, token or credential is stored here.
"""
from contextlib import contextmanager, ExitStack, closing
import hashlib
import math
import os
from pathlib import Path
import sqlite3
import threading
import time

MIN_START_INTERVAL = 12.0  # At most five starts per minute, without a burst.
QUOTA_PROBE_COOLDOWN = 2 * 60 * 60
_locks = {}
_locks_guard = threading.Lock()


class RequestPacer:
    def __init__(self, root, *, clock=time.time, sleep=time.sleep):
        self.root = Path(root)
        self.clock, self.sleep = clock, sleep

    def _note_quota(self, bucket, scope, retry_after):
        scope = scope if scope in ('requests_per_minute', 'tokens_per_minute', 'daily', 'unknown') else 'unknown'
        # Unknown/daily is never inferred recovered from time or catalog success.
        now = self.clock()
        until = (now + max(MIN_START_INTERVAL, retry_after)
                 if scope in ('requests_per_minute', 'tokens_per_minute') and
                 isinstance(retry_after, (int, float)) and math.isfinite(retry_after) and retry_after >= 0
                 else None)
        with closing(sqlite3.connect(self.root / 'starts.sqlite3', timeout=5)) as db:
            existing = db.execute('SELECT scope, until_at, probe_after FROM quota WHERE bucket=?', (bucket,)).fetchone()
            if existing and existing[1] is None:
                scope, until = existing[0], None
            elif existing and until is not None:
                until = max(existing[1], until)
            delay = retry_after if type(retry_after) in (int, float) and math.isfinite(retry_after) and retry_after >= 0 else 0.
            probe_after = max(now + max(QUOTA_PROBE_COOLDOWN, delay), existing[2] if existing else 0.)
            db.execute('INSERT OR REPLACE INTO quota (bucket,scope,until_at,probe_after) VALUES (?,?,?,?)',
                       (bucket, scope, until, probe_after))
            db.commit()

    @contextmanager
    def slot(self, provider, endpoint=None, *, check_cancel=lambda: None, on_wait=None,
             allow_quota_probe=False):
        if provider not in ('gemini', 'deepseek', 'router9'):
            raise ValueError('Nhà cung cấp không hợp lệ cho lịch API.')
        if provider == 'router9':
            from providers import router_base_url
            endpoint = router_base_url(endpoint)
        else:
            endpoint = ''
        bucket = hashlib.sha256((provider + '\n' + endpoint).encode()).hexdigest()
        self.root.mkdir(parents=True, exist_ok=True)
        identity = (str(self.root.resolve()), bucket)
        with _locks_guard:
            lock = _locks.setdefault(identity, threading.Lock())
        notified = False

        def wait(delay):
            nonlocal notified
            check_cancel()
            if not notified and on_wait is not None:
                on_wait({'event': 'rate_wait', 'interval_seconds': MIN_START_INTERVAL,
                         'message': 'Đang chờ lịch API chung: tối đa 5 lượt/phút, không gọi song song.'})
                notified = True
            self.sleep(min(.2, delay))
            check_cancel()

        while not lock.acquire(blocking=False):
            wait(.2)
        try:
            # An OS lock lasts exactly as long as the HTTP send. It is released
            # automatically on process death; no expiring lease can overlap a
            # slow request. SQLite commits retain start spacing after restart.
            with (self.root / (bucket + '.lock')).open('a+b') as handle:
                handle.seek(0, 2)
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                while True:
                    check_cancel()
                    handle.seek(0)
                    try:
                        if os.name == 'nt':
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except (BlockingIOError, PermissionError):
                        wait(.2)
                try:
                    with closing(sqlite3.connect(self.root / 'starts.sqlite3', timeout=5)) as db:
                        db.execute('CREATE TABLE IF NOT EXISTS starts (bucket TEXT PRIMARY KEY, at REAL NOT NULL)')
                        db.execute('CREATE TABLE IF NOT EXISTS quota (bucket TEXT PRIMARY KEY, scope TEXT NOT NULL, until_at REAL)')
                        if 'probe_after' not in {r[1] for r in db.execute('PRAGMA table_info(quota)')}:
                            db.execute('ALTER TABLE quota ADD COLUMN probe_after REAL')
                            # Old evidence without a timestamp cannot prove that
                            # its cooldown has elapsed. Preserve it conservatively.
                            db.execute('UPDATE quota SET probe_after=? WHERE probe_after IS NULL',
                                       (self.clock() + QUOTA_PROBE_COOLDOWN,))
                            db.commit()
                        quota = db.execute('SELECT scope, until_at, probe_after FROM quota WHERE bucket=?', (bucket,)).fetchone()
                        if quota and (quota[0] not in ('requests_per_minute', 'tokens_per_minute', 'daily', 'unknown') or
                                type(quota[2]) not in (int, float) or not math.isfinite(quota[2]) or quota[2] < 0 or
                                quota[1] is not None and (type(quota[1]) not in (int, float) or
                                not math.isfinite(quota[1]) or quota[1] < 0)):
                            raise ValueError('Bằng chứng quota trong lịch API không hợp lệ.')
                        recovery_probe = quota and quota[1] is None and allow_quota_probe is True and self.clock() >= quota[2]
                        if quota and not recovery_probe and (quota[1] is None or self.clock() < quota[1]):
                            from providers import ProviderError
                            raise ProviderError('quota', 'Kết nối API đã trả HTTP 429. Đang giữ khóa quota của kết nối; '
                                'đổi model/key hoặc tải danh mục không gỡ khóa. Sau thời gian chờ, dùng Thử dịch ngắn '
                                'để kiểm tra phục hồi; không tự gửi lại quy trình.',
                                http_status=429, attempts=0, quota_scope=quota[0],
                                retry_after=max(0., (quota[1] if quota[1] is not None else quota[2]) - self.clock()))
                        row = db.execute('SELECT at FROM starts WHERE bucket=?', (bucket,)).fetchone()
                        last = row[0] if row else None
                        now = self.clock()
                        if (not math.isfinite(now) or (last is not None and (
                                not isinstance(last, (int, float)) or not math.isfinite(last) or
                                last > now + MIN_START_INTERVAL))):
                            raise ValueError('Đồng hồ hoặc lịch API không hợp lệ; kiểm tra giờ hệ thống trước khi gọi tiếp.')
                        while last is not None and now < last + MIN_START_INTERVAL:
                            wait(last + MIN_START_INTERVAL - now)
                            now = self.clock()
                        check_cancel()
                        db.execute('INSERT OR REPLACE INTO starts VALUES (?,?)', (bucket, now))
                        db.commit()
                    # Caller rechecks live admission and reserves its budget
                    # here, after the wait and immediately before HTTP.
                    def record_result(scope=None, delay=None, *, probe_succeeded=False):
                        if probe_succeeded:
                            if allow_quota_probe is not True:
                                raise ValueError('Chỉ phép thử sinh nội dung chủ động mới xác nhận phục hồi quota.')
                            # Output validation runs after releasing the slot.
                            # Compare the original quota receipt atomically, so
                            # a newer 429 from another process is never erased.
                            if quota is not None:
                                with closing(sqlite3.connect(self.root / 'starts.sqlite3', timeout=5)) as db:
                                    db.execute('DELETE FROM quota WHERE bucket=? AND scope=? AND until_at IS ? AND probe_after=?',
                                               (bucket, *quota))
                                    db.commit()
                        else:
                            self._note_quota(bucket, scope, delay)
                    yield record_result
                finally:
                    handle.seek(0)
                    if os.name == 'nt':
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            lock.release()


@contextmanager
def request_slot(provider, endpoint=None, *, check_cancel=lambda: None, on_wait=None,
                 allow_quota_probe=False):
    # Read at call time so private test stores and app profiles never leak.
    from core import DATA
    with ExitStack() as stack:
        try:
            recorder = stack.enter_context(RequestPacer(DATA / 'api-pacing').slot(provider, endpoint,
                                check_cancel=check_cancel, on_wait=on_wait, allow_quota_probe=allow_quota_probe))
        except (OSError, sqlite3.Error, ValueError):
            from providers import ProviderError
            raise ProviderError('scheduler', 'Không xác minh được lịch gọi API chung; dừng trước khi gửi tiếp.') from None
        yield recorder
