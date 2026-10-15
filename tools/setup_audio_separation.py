"""Install one pinned 56.34 MiB ONNX model; never pip-install or replace runtimes."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from audio_separation import (ATTRIBUTION, MODEL_BYTES, MODEL_DIR, MODEL_METADATA_URL,
    MODEL_NAME, MODEL_SHA256, MODEL_URL, MODEL_UVR_HASH, model_path, verify_model)


def install(destination=None, opener=urllib.request.urlopen):
    destination = Path(destination) if destination is not None else MODEL_DIR
    destination.mkdir(parents=True, exist_ok=True)
    target = destination/MODEL_NAME
    if target.is_file():
        try:
            verify_model(target)
            _manifest(destination)
            return target
        except ValueError:
            pass  # preserve previous file until replacement has fully verified.
    descriptor, name = tempfile.mkstemp(prefix=MODEL_NAME+'.', suffix='.download', dir=destination)
    temporary = Path(name)
    try:
        request = urllib.request.Request(MODEL_URL, headers={'User-Agent':'NPK-Audio-Separation/1.0'})
        digest, size = hashlib.sha256(), 0
        with os.fdopen(descriptor, 'wb') as output, opener(request, timeout=60) as response:
            for block in iter(lambda: response.read(1024*1024), b''):
                size += len(block)
                if size > MODEL_BYTES:
                    raise ValueError('Tải model vượt kích thước đã chốt; dừng tải.')
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if size != MODEL_BYTES or digest.hexdigest() != MODEL_SHA256:
            raise ValueError('Model tải về thiếu hoặc SHA256 không khớp; giữ nguyên bản cũ.')
        os.replace(temporary, target)
        _manifest(destination)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def _manifest(destination):
    from translation_checkpoint import write_document
    write_document(destination/'manifest.json', {
        'model':MODEL_NAME, 'bytes':MODEL_BYTES, 'sha256':MODEL_SHA256,
        'uvr_last_10000k_md5':MODEL_UVR_HASH, 'source':MODEL_URL,
        'metadata_source':MODEL_METADATA_URL, 'provider':'CPUExecutionProvider',
        'license':'UVR models MIT with attribution; see linked upstream statement',
        'attribution':ATTRIBUTION})
    (destination/'ATTRIBUTION.txt').write_text(ATTRIBUTION, encoding='utf-8')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    path = install(args.destination)
    print(json.dumps({'installed':True, 'path':str(path), 'bytes':MODEL_BYTES,
                      'sha256':MODEL_SHA256, 'device':'cpu'}, ensure_ascii=False))
