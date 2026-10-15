"""Fail-closed host boundaries for isolated tests (not an OS security sandbox).

MockTransport and monkeypatched model constructors remain usable. Real Windows
secrets, production DATA, external sockets, shell commands and inference are
never admitted by the unit/media profiles. The media profile explicitly permits
the configured local FFmpeg, ffprobe and Node binaries for fixture verification.
"""
import importlib.abc
import importlib.machinery
import json
import os
from pathlib import Path
import shutil
import socket
import sys
import threading


class BoundaryViolation(RuntimeError):
    pass


def install(root, private, profile='unit'):
    root, private = Path(root).resolve(), Path(private).resolve()
    base = (root / 'build/test-runs').resolve()
    if private.parent != base or private == (root/'data').resolve() or not private.is_dir():
        raise BoundaryViolation('Test DATA must be a newly allocated store owned by build/test-runs.')
    if profile not in ('unit', 'media', 'native'):
        raise BoundaryViolation('Unknown test boundary profile.')
    report = {'profile': profile, 'data': str(private), 'production_data_used': False,
              'credentials': 'memory-only', 'external_network': 'denied-in-python',
              'inference': 'offline-opt-in' if profile == 'native' else 'denied',
              'processes': [], 'blocked': []}
    context = threading.local()

    def deny(kind):
        # Never write request bodies, API keys, prompts or command arguments.
        report['blocked'].append(kind)
        raise BoundaryViolation('Test boundary denied ' + kind + '. Mock this boundary or select the documented profile.')

    # A private credential backend can store fixture secrets. It has no route
    # to the machine's Windows vault, even for an unmocked credentials caller.
    import keyring
    from keyring.backend import KeyringBackend

    class FixtureKeyring(KeyringBackend):
        priority = 1000
        def __init__(self):
            self.values = {}
        def get_password(self, service, username):
            return self.values.get((service, username))
        def set_password(self, service, username, password):
            self.values[(service, username)] = password
        def delete_password(self, service, username):
            if (service, username) not in self.values:
                raise keyring.errors.PasswordDeleteError('Fixture secret absent.')
            del self.values[(service, username)]

    backend = FixtureKeyring()
    keyring.set_keyring(backend)
    # Bind the public entrypoints to the fixture object: selecting another
    # backend in code cannot redirect credentials helpers to the Windows vault.
    keyring.get_password = backend.get_password
    keyring.set_password = backend.set_password
    keyring.delete_password = backend.delete_password
    keyring.get_credential = lambda service, username: None
    original_set_keyring = keyring.set_keyring
    def fixture_set_keyring(value):
        if value is not backend:
            deny('native-credential-backend')
        original_set_keyring(value)
    keyring.set_keyring = fixture_set_keyring

    allowed = set()
    for name in ('ffmpeg', 'ffprobe', 'node'):
        path = shutil.which(name)
        if path:
            allowed.add(str(Path(path).resolve()).casefold())
    for path in (root/'runtime/ffmpeg/ffmpeg.exe', root/'runtime/ffmpeg/ffprobe.exe',
                 root/'runtime/bin/ffmpeg.exe', root/'runtime/bin/ffprobe.exe',
                 root/'runtime/ffmpeg.exe', root/'runtime/ffprobe.exe',
                 Path('C:/Users/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe')):
        if path.is_file():
            allowed.add(str(path.resolve()).casefold())

    def within(path, base):
        try:
            Path(path).resolve().relative_to(base)
            return True
        except (ValueError, TypeError, OSError):
            return False

    def audit(event, args):
        if event in ('socket.connect', 'socket.sendto'):
            address = args[-1]
            if getattr(context, 'socketpair', False):
                return
            # Native profile is still offline, but permits a local test server.
            if profile == 'native' and isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
                return
            deny('network-connect')
        elif event == 'socket.getaddrinfo':
            if getattr(context, 'socketpair', False):
                return
            if profile == 'native' and args[0] in ('127.0.0.1', '::1', 'localhost'):
                return
            deny('network-dns')
        elif event in ('os.system', 'os.exec', 'os.spawn'):
            deny('shell-process')
        elif event == 'subprocess.Popen':
            executable, command = args[:2]
            if not executable:
                # Windows Popen audits a list2cmdline string with executable=None.
                if isinstance(command, (list, tuple)):
                    executable = command[0]
                else:
                    # Only inspect argv[0]. Parsing the entire Windows
                    # list2cmdline string as POSIX shell syntax breaks valid
                    # multiline Node fixtures with escaped JavaScript quotes.
                    first = command.lstrip()
                    if first.startswith('"'):
                        closing = first.find('"', 1)
                        if closing < 0:
                            deny('invalid-process-command')
                        executable = first[1:closing]
                    else:
                        executable = first.split(None, 1)[0]
            path = shutil.which(str(executable)) or str(executable)
            identity = str(Path(path).resolve()).casefold()
            accepted = profile in ('media', 'native') and identity in allowed
            if profile == 'media' and identity == str(Path(sys.executable).resolve()).casefold():
                if isinstance(command, (list, tuple)):
                    argv = [str(value) for value in command]
                    accepted = argv[1:] == ['-X', 'utf8', str(root/'tools/media_fixture_child.py')]
                else:
                    # Match only the exact owned wrapper tail, not arbitrary
                    # Python -c/-m/native scripts or a user-selected command.
                    import subprocess
                    accepted = command == subprocess.list2cmdline(
                        [str(executable), '-X', 'utf8', str(root/'tools/media_fixture_child.py')])
            if profile == 'native' and within(path, root/'runtime') and Path(path).name.lower() in ('python.exe', 'pythonw.exe'):
                accepted = True
            if not accepted:
                deny('native-process')
            report['processes'].append(Path(path).name)
        elif event in ('open', 'sqlite3.connect'):
            path = args[0]
            if isinstance(path, (str, bytes, os.PathLike)):
                # Even read-only tests must not silently use desktop projects.
                value = os.fsdecode(path)
                if value.startswith('file:'):
                    from urllib.parse import unquote, urlsplit
                    value = unquote(urlsplit(value).path).lstrip('/') if os.name == 'nt' else unquote(urlsplit(value).path)
                if within(value, root/'data'):
                    deny('production-data-access')

    # Windows asyncio implements socketpair through local socket connect. Only
    # that bounded operation is permitted; loopback provider URLs stay blocked.
    original_socketpair = socket.socketpair
    def safe_socketpair(*args, **kwargs):
        previous = getattr(context, 'socketpair', False)
        context.socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            context.socketpair = previous
    socket.socketpair = safe_socketpair
    sys.addaudithook(audit)

    def blocked_inference(*args, **kwargs):
        deny('inference-constructor')

    targets = {
        'faster_whisper': ('WhisperModel',),
        'faster_whisper.transcribe': ('WhisperModel',),
        'onnxruntime': ('InferenceSession',),
        'onnxruntime.capi.onnxruntime_inference_collection': ('InferenceSession',),
        'ctranslate2.models': ('Whisper',),
        'torch': ('load',),
        'torch.jit': ('load',),
    }

    class GuardLoader(importlib.abc.Loader):
        def __init__(self, wrapped, name):
            self.wrapped, self.name = wrapped, name
        def create_module(self, spec):
            return self.wrapped.create_module(spec) if hasattr(self.wrapped, 'create_module') else None
        def exec_module(self, module):
            self.wrapped.exec_module(module)
            for name in targets[self.name]:
                if hasattr(module, name):
                    setattr(module, name, blocked_inference)

    class GuardFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname not in targets:
                return None
            spec = importlib.machinery.PathFinder.find_spec(fullname, path)
            if spec is not None and spec.loader is not None:
                spec.loader = GuardLoader(spec.loader, fullname)
            return spec

    if profile != 'native':
        sys.meta_path.insert(0, GuardFinder())
        for fullname, names in targets.items():
            if fullname in sys.modules:
                for name in names:
                    if hasattr(sys.modules[fullname], name):
                        setattr(sys.modules[fullname], name, blocked_inference)
    # Fixture transports have no upstream quota. Explicit test-runner injection
    # avoids real-time waits while production has no environment-based bypass.
    # Scheduler tests instantiate RequestPacer with a virtual clock directly.
    from contextlib import contextmanager
    import request_pacing
    @contextmanager
    def fixture_request_slot(provider, endpoint=None, *, check_cancel=lambda: None, on_wait=None,
                             allow_quota_probe=False):
        check_cancel()
        yield
    request_pacing.request_slot = fixture_request_slot
    report['installed_before_app_imports'] = not any(name in sys.modules for name in ('core', 'server', 'worker'))
    os.environ['NPK_TEST_BOUNDARY_ACTIVE'] = profile
    return report


def write_report(report):
    path = Path(report['data']) / 'test-boundary-report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
