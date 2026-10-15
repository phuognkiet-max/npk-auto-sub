"""Exact job-scoped cancellation across Windows venv redirector processes."""
import os
from pathlib import Path
import re
import sqlite3
import threading
import psutil
from core import ROOT, DATA, get

def normalized(path):
    return os.path.normcase(os.path.abspath(path))

def matches_job(args, identifier):
    if not re.fullmatch(r'[0-9a-f]{32}', identifier):
        return False
    script = normalized(ROOT / 'worker.py')
    spec = normalized(DATA / 'jobs' / identifier / 'input.json')
    normalized_args = [normalized(arg) for arg in args[1:] if arg and not arg.startswith('-')]
    return script in normalized_args and spec in normalized_args

def find_job_workers(identifier):
    found = []
    for process in psutil.process_iter(['cmdline', 'name']):
        try:
            if process.info['name'].lower() in {'python.exe', 'pythonw.exe'} and matches_job(process.info['cmdline'] or [], identifier):
                found.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, AttributeError):
            pass
    return found

def terminate_job_workers(identifier):
    job = get('job', identifier)
    if not job or job['state'] != 'cancelled':
        return []
    workers = find_job_workers(identifier)
    victims = {}
    for worker in workers:
        try:
            for child in worker.children(recursive=True):
                victims[child.pid] = child
            victims[worker.pid] = worker
        except psutil.NoSuchProcess:
            pass
    for process in reversed(list(victims.values())):
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(list(victims.values()), timeout=3)
    return [p.pid for p in alive if p.is_running()]

def start_worker_guard(identifier):
    """Stop even if cancellation arrives in another UI or the owner disappears."""
    event = threading.Event()
    owner_id = int(os.environ.get('VIETDUB_OWNER_PID', '0'))
    owner_created = float(os.environ.get('VIETDUB_OWNER_CREATED', '0'))
    def watch():
        while not event.wait(1):
            try:
                job = get('job', identifier)
                cancelled = bool(job and job['state'] == 'cancelled')
                owner_gone = False
                if owner_id:
                    try:
                        owner_gone = abs(psutil.Process(owner_id).create_time() - owner_created) > .1
                    except psutil.NoSuchProcess:
                        owner_gone = True
                if cancelled or owner_gone:
                    for child in psutil.Process().children(recursive=True):
                        try:
                            child.kill()
                        except psutil.NoSuchProcess:
                            pass
                    os._exit(2)
            except (OSError, sqlite3.Error, psutil.AccessDenied):
                continue
    threading.Thread(target=watch, name='StudioJobGuard', daemon=True).start()
    return event
