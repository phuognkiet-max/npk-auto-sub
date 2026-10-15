"""One short public-model phrase on each owned native backend; no CIT calls."""
import json
import os
from pathlib import Path
import subprocess
import time
import wave
from sea_g2p import SEAPipeline

root = Path(__file__).resolve().parents[1]
out = root / 'tests/vieneu-independent-results'
out.mkdir(exist_ok=True)
model = root / 'models/vieneu-v3-turbo'
text = 'Xin chào bạn. Chúc bạn một ngày tốt lành!'
phonemes = SEAPipeline(lang='vi').run(text, punc_norm=True)
print('Phonemes ready', len(phonemes), flush=True)
records = []
for backend in ('cpu', 'cuda'):
    output = out / (backend + '-probe.wav')
    args = [str(root / f'runtime/audio-cpp/{backend}/audiocpp_cli.exe'), '--task', 'tts',
            '--family', 'vieneu_v3_turbo', '--model', str(model / 'model.gguf'),
            '--backend', backend, '--threads', '4', '--text', phonemes,
            '--request-option', 'reference_codes_file=' + str(model / 'voices/minh_quan_pro/ref_codes.txt'),
            '--request-option', 'speaker_embedding_file=' + str(model / 'voices/minh_quan_pro/speaker.emb.txt'),
            '--seed', '42', '--out', str(output)]
    env = dict(os.environ)
    env['PATH'] = str(root / 'runtime/venv/Lib/site-packages/torch/lib') + os.pathsep + env.get('PATH', '')
    began = time.perf_counter()
    result = subprocess.run(args, env=env, capture_output=True, timeout=120,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    elapsed = time.perf_counter() - began
    (out / (backend + '-probe.log')).write_bytes(result.stdout + result.stderr)
    row = dict(backend=backend, exit_code=result.returncode, elapsed=round(elapsed, 3))
    if result.returncode == 0 and output.is_file():
        with wave.open(str(output), 'rb') as audio:
            row.update(sample_rate=audio.getframerate(), duration=audio.getnframes()/audio.getframerate())
    records.append(row)
    print(json.dumps(row), flush=True)
(out / 'backend-probe.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
