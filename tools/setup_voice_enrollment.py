"""Install small, pinned CAM++ enrollment assets in NPK-owned folders only."""
import json
from pathlib import Path
import time
from setup_independent_vieneu import ROOT, HF, REVISION, download, extract, sha256

ENCODER_SHA256 = 'a6ac6a63997761ae2997373e2ee1c47040854b4b759ea41ec48e4e42df0f4d73'
WHEEL_SHA256 = 'cbbeea19fe6d584c54e93fe6615a7185b10e0d78fdb6471f9e44596018437c38'
WHEEL_URL = ('https://files.pythonhosted.org/packages/cb/8d/c0b0b6280edabad85d7e15093fad612c027e175fe4e0b960ce2f36485143/'
             'kaldi_native_fbank-1.22.3-cp312-cp312-win_amd64.whl')
MODEL = ROOT / 'models/vieneu-enrollment'
RUNTIME = ROOT / 'runtime/voice-enrollment'


def main():
    encoder = download(HF + '/resolve/' + REVISION + '/speaker_encoder.onnx', MODEL / 'speaker_encoder.onnx', ENCODER_SHA256)
    wheel = download(WHEEL_URL, RUNTIME / 'downloads/kaldi-native-fbank-1.22.3-win64.whl', WHEEL_SHA256)
    extract(wheel, RUNTIME / 'python')
    files = {str(path.relative_to(ROOT)).replace('\\', '/'): sha256(path)
             for path in (RUNTIME / 'python').rglob('*') if path.is_file()}
    record = dict(schema=1, verified=True, model_repo='pnnbao-ump/VieNeu-TTS-v3-Turbo', model_revision=REVISION,
                  speaker_encoder_sha256=sha256(encoder), kaldi_version='1.22.3', kaldi_wheel_sha256=sha256(wheel),
                  frontend='kaldi-native-fbank 80 bins 16kHz, povey, mean-normalized', files=files, installed_at=time.time())
    temporary = MODEL / 'installation.json.part'
    temporary.write_text(json.dumps(record, indent=2), encoding='utf-8')
    temporary.replace(MODEL / 'installation.json')
    print('Enrollment assets verified. No SDK, torch, torchaudio or main-runtime packages installed.', flush=True)


if __name__ == '__main__':
    main()
