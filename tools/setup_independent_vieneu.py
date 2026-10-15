"""Install pinned public VieNeu/audio.cpp assets under NPK, never another app."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import time
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / 'models/vieneu-v3-turbo'
DOWNLOADS = ROOT / 'runtime/audio-cpp/downloads'
REVISION = '61b85e3d937fbbacb387714180e8182823512523'
SDK_REVISION = '85344322b7258b4e25479b692e8e3396baf9db34'
AUDIO_CPP_REVISION = 'cf124a67cc55d8f65a9a15eec69edbff0fb212c8'
MODEL_SHA256 = 'a60569e5d7dd6f24cbb7bb2e7c472bb988ad19563a4e78f0cc67cc53fd189bb4'
CATALOG_SHA256 = 'e1f13cd2c2e0d29fdab5e15bfd7a30f3d7b768e6132bb43dd2b853f8efefdce9'
SDK_LICENSE_SHA256 = 'c71d239df91726fc519c6eb72d318ec65820627232b2f796219e87dcf35d0ab4'
AUDIO_CPP_LICENSE_SHA256 = '8f0b762d062e1ec79a3e73189b29474051c0e957973f43146aefeb03f2e11581'
ARCHIVE_SHA256 = {
    'cpu': '2fccc73dc97af84da1e81c3af545fa619348a46d023ea7d927ba80bc8680d2a0',
    'cuda': 'f32a40f8fb14ac4772c9c25525f97715db228979178e51654da73aa65005c0ef',
}
HF = 'https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo'


def sha256(path):
    """Hash checkpoints without loading hundreds of megabytes into memory."""
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def download(url, target, expected=None):
    target = Path(target)
    if target.is_file() and target.stat().st_size:
        if not expected or sha256(target) == expected:
            return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + '.part')
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': 'NPK-Auto-Sub-Installer/1.0'})
            digest, count = hashlib.sha256(), 0
            with urllib.request.urlopen(request, timeout=90) as response, temporary.open('wb') as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
                    count += len(block)
            if not count or expected and digest.hexdigest() != expected:
                raise ValueError('Download checksum mismatch: ' + target.name)
            temporary.replace(target)
            print('Downloaded', target.relative_to(ROOT), count, digest.hexdigest(), flush=True)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            if attempt == 2:
                raise
            time.sleep(2)


def extract(archive, destination):
    destination.mkdir(parents=True, exist_ok=True)
    base = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            target = (base / member.filename).resolve()
            if target != base and base not in target.parents:
                raise ValueError('Archive path escapes NPK runtime')
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Symlink in runtime archive')
        source.extractall(base)
    print('Extracted', destination.relative_to(ROOT), flush=True)


def base_files():
    sdk = 'https://raw.githubusercontent.com/pnnbao97/VieNeu-TTS/' + SDK_REVISION
    audio_cpp = 'https://raw.githubusercontent.com/0xShug0/audio.cpp/' + AUDIO_CPP_REVISION
    return [
        (HF + '/resolve/' + REVISION + '/gguf/vieneu-v3-turbo-q8_0.gguf', MODEL / 'model.gguf', MODEL_SHA256),
        (HF + '/resolve/' + REVISION + '/README.md', MODEL / 'MODEL-README.md', None),
        (sdk + '/src/vieneu/assets/voices_v3_turbo.json', MODEL / 'voices_v3_turbo.json', CATALOG_SHA256),
        (sdk + '/LICENSE', MODEL / 'LICENSE-VieNeu-TTS', SDK_LICENSE_SHA256),
        (audio_cpp + '/LICENSE', ROOT / 'runtime/audio-cpp/LICENSE', AUDIO_CPP_LICENSE_SHA256),
        ('https://github.com/0xShug0/audio.cpp/releases/download/v0.9.1/audio-v0.9.1-bin-windows-x64-cpu-portable.zip',
            DOWNLOADS / 'cpu-v0.9.1.zip', ARCHIVE_SHA256['cpu']),
        ('https://github.com/0xShug0/audio.cpp/releases/download/v0.9.1/audio-v0.9.1-bin-windows-x64-cuda12.4.zip',
            DOWNLOADS / 'cuda-v0.9.1.zip', ARCHIVE_SHA256['cuda']),
    ]


def create_manifest(previous):
    """Read existing owned assets only; preserve the backend already accepted."""
    expected = [(MODEL / 'model.gguf', MODEL_SHA256), (MODEL / 'voices_v3_turbo.json', CATALOG_SHA256),
        (MODEL / 'LICENSE-VieNeu-TTS', SDK_LICENSE_SHA256),
        (ROOT / 'runtime/audio-cpp/LICENSE', AUDIO_CPP_LICENSE_SHA256)]
    hashes = {}
    for path, digest in expected:
        actual = sha256(path)
        if actual != digest:
            raise ValueError('Installed asset checksum mismatch: ' + str(path.relative_to(ROOT)))
        hashes[str(path.relative_to(ROOT)).replace('\\', '/')] = actual
    other_assets = [MODEL / 'MODEL-README.md', MODEL / 'voices/manifest.json']
    other_assets += sorted((MODEL / 'voices').rglob('*.txt'))
    for backend in ('cpu', 'cuda'):
        other_assets += sorted((ROOT / 'runtime/audio-cpp' / backend).rglob('audiocpp_cli.exe'))
    for path in other_assets:
        hashes[str(path.relative_to(ROOT)).replace('\\', '/')] = sha256(path)
    backend = previous.get('backend', 'cpu')
    if backend not in ('cpu', 'cuda'):
        backend = 'cpu'
    default_backend = previous.get('default_backend', backend)
    if default_backend not in ('cpu', 'cuda'):
        default_backend = backend
    return previous | {
        'schema': 1, 'verified': True, 'backend': backend, 'threads': previous.get('threads', 4),
        'model_repo': 'pnnbao-ump/VieNeu-TTS-v3-Turbo', 'model_revision': REVISION,
        'model_sha256': MODEL_SHA256, 'format': 'GGUF-Q8_0', 'audio_cpp_version': '0.9.1', 'sample_rate': 48000,
        'audio_cpp_sha256': ARCHIVE_SHA256[backend], 'runtime_archives_sha256': ARCHIVE_SHA256,
        'runtime': 'independent NPK-owned files', 'default_backend': default_backend,
        'voices_source': 'pnnbao97/VieNeu-TTS/src/vieneu/assets/voices_v3_turbo.json',
        'voices_source_revision': SDK_REVISION,
        'source_revisions': {'model': REVISION, 'vieneu_sdk': SDK_REVISION, 'audio_cpp': AUDIO_CPP_REVISION},
        'assets_sha256': dict(sorted(hashes.items())), 'installed_at': previous.get('installed_at', time.time()),
    }


def write_manifest(manifest):
    target = MODEL / 'installation.json'
    contents = json.dumps(manifest, ensure_ascii=False, indent=2)
    if target.is_file() and target.read_text(encoding='utf-8') == contents:
        return
    temporary = target.with_suffix('.json.part')
    try:
        temporary.write_text(contents, encoding='utf-8')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def main():
    MODEL.mkdir(parents=True, exist_ok=True)
    DOWNLOADS.mkdir(parents=True, exist_ok=True)
    try:
        previous = json.loads((MODEL / 'installation.json').read_text(encoding='utf-8'))
        if not isinstance(previous, dict):
            previous = {}
    except (OSError, ValueError):
        previous = {}
    files = base_files()
    with urllib.request.urlopen('https://huggingface.co/api/models/pnnbao-ump/VieNeu-TTS-v3-Turbo/tree/' +
            REVISION + '/gguf/voices?recursive=true', timeout=30) as response:
        tree = json.load(response)
    for row in tree:
        path = row.get('path', '')
        if row.get('type') == 'file' and path.startswith('gguf/voices/'):
            relative = path.removeprefix('gguf/')
            target = (MODEL / relative).resolve()
            if MODEL.resolve() not in target.parents:
                raise ValueError('Invalid upstream asset path')
            files.append((HF + '/resolve/' + REVISION + '/' + path, target, None))
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(download, *args) for args in files]
        for future in futures:
            future.result()
    for backend in ('cpu', 'cuda'):
        destination = ROOT / 'runtime/audio-cpp' / backend
        # Rerunning setup must not replace an installed native executable.
        if not any(destination.rglob('audiocpp_cli.exe')):
            extract(DOWNLOADS / (backend + '-v0.9.1.zip'), destination)
    write_manifest(create_manifest(previous))
    print('DONE independent model/runtime assets', flush=True)


if __name__ == '__main__':
    main()
