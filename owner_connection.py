"""Private discovery for the one desktop process that owns NPK's queue.

This module has no application imports or storage initialization. The local
session token is written only after the temporary file has a private ACL, is
never part of a public identity, and must never be logged by callers.
"""
from __future__ import annotations

import ctypes
import json
import math
import os
from pathlib import Path
import secrets
import time
import uuid

import psutil


NAME = 'owner-connection.json'
MAX_BYTES = 16384
MAX_HEARTBEAT_AGE = 60


class OwnerUnavailable(ValueError):
    """A safe, non-secret reason why an owner cannot receive a request."""


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _current_user_sid():
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    token = wintypes.HANDLE()
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    advapi.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        raise OSError(ctypes.get_last_error(), 'Không xác định được quyền người dùng của phiên NPK.')
    try:
        needed = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not advapi.GetTokenInformation(token, 1, buffer, needed, ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), 'Không xác định được quyền người dùng của phiên NPK.')
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise OSError(ctypes.get_last_error(), 'Không xác định được quyền người dùng của phiên NPK.')
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def _secure_file(path):
    if os.name != 'nt':
        os.chmod(path, 0o600)
        return
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    advapi.SetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    descriptor = ctypes.c_void_p()
    # Protected DACL: only this Windows user and Local System can read the token.
    sddl = 'D:P(A;;FA;;;' + _current_user_sid() + ')(A;;FA;;;SY)'
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise OSError(ctypes.get_last_error(), 'Không bảo vệ được phiên truy cập NPK.')
    try:
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise OSError(ctypes.get_last_error(), 'Không bảo vệ được phiên truy cập NPK.')
    finally:
        kernel.LocalFree(descriptor)


def _private_file(path):
    """Reject an inherited/broad ACL before reading a local session token."""
    if os.name != 'nt':
        stat = path.stat()
        return stat.st_uid == os.getuid() and not stat.st_mode & 0o077
    from ctypes import wintypes
    advapi = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    advapi.GetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD))
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.LPWSTR), ctypes.c_void_p)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    needed = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(needed))
    if not needed.value:
        return False
    buffer = ctypes.create_string_buffer(needed.value)
    if not advapi.GetFileSecurityW(str(path), 4, buffer, needed, ctypes.byref(needed)):
        return False
    result = wintypes.LPWSTR()
    if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(buffer, 1, 4, ctypes.byref(result), None):
        return False
    try:
        actual = result.value
        # No inherited ACEs and no other principal than our exact SID/System.
        import re
        aces = re.findall(r'\(([^()]*)\)', actual)
        expected = {_current_user_sid(), 'SY'}
        return (actual.startswith('D:P') and len(aces) == 2 and
                all(ace.split(';')[:5] == ['A', '', 'FA', '', ''] for ace in aces) and
                {ace.split(';')[-1] for ace in aces} == expected)
    finally:
        kernel.LocalFree(result)


def _document(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise OwnerUnavailable('owner_missing')
    try:
        if not _private_file(path):
            raise OwnerUnavailable('owner_permissions')
        with path.open('rb') as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise OwnerUnavailable('owner_invalid')
        value = json.loads(raw.decode('utf-8'))
    except OwnerUnavailable:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError):
        raise OwnerUnavailable('owner_invalid') from None
    if not isinstance(value, dict):
        raise OwnerUnavailable('owner_invalid')
    return value


def _validate(record, *, now=None, check_process=True):
    now = time.time() if now is None else now
    if (record.get('schema_version') != 1 or record.get('host') != '127.0.0.1' or
            type(record.get('pid')) is not int or record['pid'] <= 0 or
            not _number(record.get('process_started')) or record['process_started'] <= 0 or
            type(record.get('port')) is not int or not 1 <= record['port'] <= 65535 or
            not isinstance(record.get('instance_id'), str) or len(record['instance_id']) != 32 or
            any(c not in '0123456789abcdef' for c in record['instance_id']) or
            not isinstance(record.get('token'), str) or not 16 <= len(record['token']) <= 512 or
            any(ord(c) < 33 or ord(c) > 126 for c in record['token']) or
            not _number(record.get('heartbeat_at')) or
            type(record.get('queue_owned')) is not bool):
        raise OwnerUnavailable('owner_invalid')
    if not -5 <= now - record['heartbeat_at'] <= MAX_HEARTBEAT_AGE:
        raise OwnerUnavailable('owner_stale')
    if not record['queue_owned']:
        raise OwnerUnavailable('owner_queue_missing')
    if check_process:
        try:
            process = psutil.Process(record['pid'])
            if not process.is_running() or abs(process.create_time() - record['process_started']) > .001:
                raise OwnerUnavailable('owner_process_changed')
        except (psutil.Error, OSError):
            raise OwnerUnavailable('owner_process_missing') from None
    return record


def load(data_dir, *, now=None):
    """Read discovery without creating any directory or changing any file."""
    return _validate(_document(Path(data_dir) / NAME), now=now)


def _write(data_dir, record):
    directory = Path(data_dir)
    if not directory.is_dir() or directory.is_symlink():
        raise OwnerUnavailable('owner_data_missing')
    target = directory / NAME
    temporary = directory / ('.owner-' + secrets.token_hex(16) + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _secure_file(temporary)
        with os.fdopen(fd, 'wb') as stream:
            fd = None
            stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False).encode('utf-8'))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if fd is not None:
            os.close(fd)
        if temporary.exists():
            temporary.unlink()


def publish(data_dir, port, token, *, instance_id=None, queue_owned=True):
    record = {'schema_version': 1, 'host': '127.0.0.1', 'port': port,
              'pid': os.getpid(), 'process_started': psutil.Process().create_time(),
              'instance_id': instance_id or uuid.uuid4().hex, 'token': token,
              'heartbeat_at': time.time(), 'queue_owned': queue_owned}
    _validate(record)
    _write(data_dir, record)
    return record


def refresh(data_dir, record, *, queue_owned=True):
    current = _document(Path(data_dir) / NAME)
    if current.get('instance_id') != record.get('instance_id'):
        raise OwnerUnavailable('owner_replaced')
    record = dict(record, heartbeat_at=time.time(), queue_owned=queue_owned)
    _write(data_dir, record)
    return record


def remove(data_dir, record):
    path = Path(data_dir) / NAME
    try:
        current = _document(path)
        if current.get('instance_id') == record.get('instance_id'):
            path.unlink()
    except (OwnerUnavailable, OSError):
        pass


def public_identity(record):
    return {key: record[key] for key in ('schema_version', 'pid', 'process_started',
            'instance_id', 'port', 'heartbeat_at', 'queue_owned')}


def validate_live_owner(record, payload, *, now=None):
    """The HTTP listener must prove it is the same healthy queue owner."""
    _validate(record, now=now)
    if (not isinstance(payload, dict) or
            any(payload.get(key) != record[key] for key in ('schema_version', 'pid', 'process_started', 'instance_id', 'port')) or
            payload.get('queue_owned') is not True or payload.get('healthy') is not True or
            payload.get('thread_alive') is not True or
            not _number(payload.get('heartbeat_at')) or
            not -5 <= (time.time() if now is None else now) - payload['heartbeat_at'] <= MAX_HEARTBEAT_AGE):
        raise OwnerUnavailable('owner_unhealthy')
    return public_identity(record)
