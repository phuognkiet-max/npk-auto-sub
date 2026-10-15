"""Opt-in real local-GPU acceptance test for NPK-owned VieNeu.

Run explicitly with runtime/venv/Scripts/python.exe. This file is intentionally
outside the normal test_*.py discovery pattern: it performs real inference.
The checks assess PCM format, timing and signal, not subjective voice quality.
"""
from __future__ import annotations
from array import array
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'tests/vieneu-independent-results'
REPORT = RESULTS / 'presets-report.json'
SHORT_TEXT = 'Xin chào, đây là giọng đọc thử.'
LONG_TEXT = (
    'Chào bạn, đây là bản đọc thử dài của NPK Auto Sub. Công cụ giúp chuyển lời thoại trong video '
    'thành phụ đề và giọng đọc tiếng Việt trên máy tính của bạn. Bạn có thể chọn giọng nam hoặc nữ, '
    'điều chỉnh tốc độ và kiểm tra từng đoạn trước khi xuất video. [nghỉ 1s] '
    'Ở phần tiếp theo, chúng ta sẽ nghe một đoạn hướng dẫn có nhiều câu liên tục. '
    'Hãy giữ lời đọc ngắn gọn, rõ ý và phù hợp với nội dung đang xuất hiện trên màn hình. '
    'Sau khi xem lại kết quả, bạn có thể sửa kịch bản, thêm khoảng nghỉ và tạo lại phần cần thiết. '
    'Mọi âm thanh trong bài kiểm tra này được tổng hợp bằng engine và model đã cài riêng trong NPK.'
)
EMOTION_TEXTS = [
    ('cuoi', '[cười]', 'Xin chào [cười] thật vui khi được gặp bạn.', True),
    ('tho-dai', '[thở dài]', 'Ôi [thở dài] hôm nay chúng ta cần nghỉ một chút.', True),
    ('hang-giong', '[hắng giọng]', 'Xin phép [hắng giọng] tôi sẽ bắt đầu phần hướng dẫn.', False),
]


def inspect_wave(path):
    peak, square_sum, nonzero, longest_zero, zero_run, count = 0, 0, 0, 0, 0, 0
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    with wave.open(str(path), 'rb') as audio:
        channels, width, rate, frames = audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()
        if (channels, width, rate) != (1, 2, 48000) or not 0 < frames <= rate * 300:
            raise ValueError('Expected nonempty PCM16 mono 48 kHz, bounded to 5 minutes.')
        while raw := audio.readframes(48000):
            samples = array('h')
            samples.frombytes(raw)
            if sys.byteorder != 'little':
                samples.byteswap()
            for sample in samples:
                count += 1
                peak = max(peak, abs(sample))
                square_sum += sample * sample
                if sample:
                    nonzero += 1
                    longest_zero = max(longest_zero, zero_run)
                    zero_run = 0
                else:
                    zero_run += 1
        longest_zero = max(longest_zero, zero_run)
        if count != frames:
            raise ValueError('Truncated WAV payload.')
    rms = math.sqrt(square_sum / count)
    if peak < 50 or nonzero < 100 or rms < 1:
        raise ValueError('Audio signal is silent or too small for this acceptance check.')
    return {'channels': channels, 'sample_width_bytes': width, 'sample_rate': rate, 'frames': frames,
        'duration_seconds': frames / rate, 'peak_pcm16': peak, 'rms_pcm16': round(rms, 3),
        'nonzero_samples': nonzero, 'longest_exact_zero_run_frames': longest_zero,
        'longest_exact_zero_run_seconds': longest_zero / rate, 'sha256': digest.hexdigest()}


def save_report(report):
    temporary = REPORT.with_suffix('.json.part')
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(REPORT)


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.path.insert(0, str(ROOT))
    RESULTS.mkdir(parents=True, exist_ok=True)
    result_base = RESULTS.resolve()
    if ROOT.resolve() not in result_base.parents:
        raise RuntimeError('Result path must stay inside the intended NPK workspace.')
    assert 450 <= len(LONG_TEXT) <= 700
    report = {'started_at_utc': datetime.now(timezone.utc).isoformat(),
        'purpose': 'Real sequential local-GPU smoke checks for all 25 independent VieNeu presets.',
        'scope': 'PCM16 mono 48 kHz, non-silent signal, successful inference and explicit pause timing.',
        'limitations': [
            'This is not a subjective listening assessment or an intelligibility/emotion-quality evaluation.',
            'Emotion cases prove successful generation with supported tokens; perceived emotion is not graded.',
            'A zero run of at least 48000 samples verifies an inserted one-second pause; adjacent native silence may extend that run.',
            'No claim is made that every output is ready for production without listening review.',
        ], 'preset_results': [], 'extended_results': [], 'archived_samples': []}
    failures = []
    previous_data = os.environ.get('VIETDUB_DATA_DIR')
    began = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='.smoke-data-', dir=result_base) as temporary:
        temp = Path(temporary).resolve()
        if result_base not in temp.parents:
            raise RuntimeError('Temporary cleanup target escaped the acceptance-test directory.')
        os.environ['VIETDUB_DATA_DIR'] = str(temp / 'data')
        import vieneu_voice
        try:
            status = vieneu_voice.provider_status(force=True)
            if not status['available'] or status['device'] != 'cuda':
                raise RuntimeError('This acceptance test requires the verified NPK-owned CUDA installation.')
            rows = vieneu_voice.entries(force=True)
            if len(rows) != 25:
                raise RuntimeError('Expected exactly 25 installed canonical presets.')
            installation = json.loads((vieneu_voice.MODEL_ROOT / 'installation.json').read_text(encoding='utf-8'))
            report.update(provider_status=status, model_sha256=installation['model_sha256'],
                source_revisions=installation.get('source_revisions'), voice_count=len(rows),
                data_isolation='Temporary VIETDUB_DATA_DIR, removed after completion.',
                long_text_characters=len(LONG_TEXT))

            def render(entry, text, filename, speed, group, keep=False, pause_required=False, emotion_tag=None):
                record = {'voice_id': entry['id'], 'voice_name': entry['name'], 'speed': speed,
                    'text_characters': len(text), 'emotion_tag': emotion_tag, 'status': 'running'}
                group.append(record)
                path = temp / filename
                started = time.perf_counter()
                try:
                    metadata = vieneu_voice.synthesize_vieneu(entry, text, path, speed)
                    audio = inspect_wave(path)
                    if pause_required and audio['longest_exact_zero_run_frames'] < 48000:
                        raise ValueError('Explicit one-second pause was not found at speed 1.3.')
                    record.update(status='passed', synthesis=metadata, audio=audio,
                        explicit_one_second_pause_preserved=pause_required if pause_required else None)
                    if keep:
                        archived = RESULTS / filename
                        shutil.copyfile(path, archived)
                        record['archived_sample'] = str(archived)
                        report['archived_samples'].append(str(archived))
                except Exception as error:
                    record.update(status='failed', error=str(error))
                    failures.append(entry['id'] + '/' + filename + ': ' + str(error))
                finally:
                    record['wall_seconds'] = round(time.perf_counter() - started, 3)
                    if record.get('audio'):
                        record['wall_to_audio_ratio'] = round(record['wall_seconds'] / record['audio']['duration_seconds'], 3)
                    path.unlink(missing_ok=True)
                    save_report(report)
                    print(f"{record['status'].upper()}: {entry['name']} | {filename} | {record['wall_seconds']:.3f}s", flush=True)

            for index, entry in enumerate(rows):
                render(entry, SHORT_TEXT, f'preset-{index:02d}.wav', 1, report['preset_results'])
            primary = next(row for row in rows if row['id'] == 'vieneu:Hải Đăng')
            render(primary, LONG_TEXT, 'long-speed-1_3.wav', 1.3, report['extended_results'], keep=True, pause_required=True)
            for slug, tag, text, keep in EMOTION_TEXTS:
                render(primary, text, 'emotion-' + slug + '.wav', 1, report['extended_results'], keep=keep, emotion_tag=tag)
        finally:
            vieneu_voice._close()
            if previous_data is None:
                os.environ.pop('VIETDUB_DATA_DIR', None)
            else:
                os.environ['VIETDUB_DATA_DIR'] = previous_data
    report.update(completed_at_utc=datetime.now(timezone.utc).isoformat(),
        elapsed_seconds=round(time.perf_counter() - began, 3),
        passed=sum(row['status'] == 'passed' for row in report['preset_results'] + report['extended_results']),
        failed=len(failures), failures=failures, temporary_data_removed=True,
        status='passed' if not failures else 'failed')
    save_report(report)
    print(json.dumps({'status': report['status'], 'passed': report['passed'], 'failed': report['failed'],
        'elapsed_seconds': report['elapsed_seconds'], 'report': str(REPORT),
        'archived_samples': report['archived_samples']}, ensure_ascii=False), flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
