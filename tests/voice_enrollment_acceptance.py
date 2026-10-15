"""Real CPU reference enrollment using only NPK-generated fixtures and isolated data."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / 'tests/voice-enrollment-results'
OUT.mkdir(exist_ok=True)


def main():
    with tempfile.TemporaryDirectory(prefix='npk-enrollment-acceptance-', dir=ROOT / 'tests') as isolated:
        os.environ['VIETDUB_DATA_DIR'] = isolated
        import numpy as np
        import voice_enrollment as enrollment
        from voices import resolve_voice
        from vieneu_voice import synthesize_vieneu
        source = ROOT / 'tests/vieneu-independent-results/NPK-text-to-speech.wav'
        if not source.is_file():
            raise ValueError('Missing own generated reference fixture')
        events, began = [], time.perf_counter()
        params = dict(tts_device='cpu', workload_mode='quiet', reference_duration=6)
        result = enrollment.enroll_voice('NPK — kiểm thử giọng riêng', source, OUT, params, events.append)
        identifier = enrollment.register_voice(OUT)
        row = resolve_voice(identifier, require_ready=True)
        spoken = 'Đây là bản kiểm tra tạo giọng riêng, xử lý hoàn toàn trong NPK.'
        generated = synthesize_vieneu(row, spoken, OUT / 'enrolled-voice.wav', 1, params, events.append)
        embedding, codes = enrollment._validate_voice_assets(OUT)
        report = dict(ok=True, source=str(source), backend='cpu', registered_in_isolated_data=True,
                      elapsed_seconds=time.perf_counter() - began, enrollment=result,
                      embedding_shape=list(embedding.shape), embedding_norm=float(np.linalg.norm(embedding)),
                      codes_shape=list(codes.shape), synthesized=generated, progress_events=len(events))
        (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: report[key] for key in ('ok', 'backend', 'elapsed_seconds', 'embedding_shape', 'embedding_norm', 'codes_shape', 'progress_events')}, ensure_ascii=False))


if __name__ == '__main__':
    main()
