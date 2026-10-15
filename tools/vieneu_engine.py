"""Private NPK VieNeu stdio broker: SEA-G2P + owned audio.cpp, no HTTP server.

Native requests across several cues share each bounded CLI batch/session. The
broker retains the phonemizer; model sessions are reused within a native batch.
"""
from __future__ import annotations
import functools
import array
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
_BRACKETS = re.compile(r'\[([^\]\r\n]{1,80})\]')
_PAUSE = re.compile(r'(?:nghỉ|nghi|pause)(?:\s+([0-9]+(?:[.,][0-9]+)?)(?:\s*(ms|s|giây|giay))?)?', re.I)
_EMOTIONS = {'cười': 1, 'cuoi': 1, 'chuckle': 1, 'thở dài': 2, 'tho dai': 2, 'sigh': 2,
             'hắng giọng': 3, 'hang giong': 3, 'clear throat': 3}


def parse_delivery(text):
    """Return speech/explicit-silence parts; speed never changes silence parts."""
    parts, cursor = [], 0
    for match in _BRACKETS.finditer(text):
        content = match.group(1).strip()
        pause = _PAUSE.fullmatch(content)
        if not pause:
            if re.match(r'^(nghỉ|nghi|pause)\b', content, re.I):
                raise ValueError('Thẻ nghỉ không hợp lệ. Dùng [nghỉ 1s] hoặc [nghỉ 500ms].')
            continue
        if text[cursor:match.start()].strip():
            parts.append(('speech', text[cursor:match.start()].strip()))
        seconds = float((pause.group(1) or '1').replace(',', '.'))
        if (pause.group(2) or '').lower() == 'ms':
            seconds /= 1000
        if not math.isfinite(seconds) or not 0 <= seconds <= 10:
            raise ValueError('Mỗi thẻ nghỉ VieNeu cần từ 0 đến 10 giây.')
        parts.append(('pause', seconds))
        cursor = match.end()
    if text[cursor:].strip():
        parts.append(('speech', text[cursor:].strip()))
    spoken = ''.join(value for kind, value in parts if kind == 'speech')
    # Recognized nonverbal cues are real model inputs even without lexical
    # speech. Silence-only or unknown tags are still not a valid utterance.
    has_words = any(char.isalnum() for char in _BRACKETS.sub('', spoken))
    tags = [match.group(1).strip().lower() for match in _BRACKETS.finditer(spoken)]
    has_nonverbal = bool(tags) and all(tag in _EMOTIONS for tag in tags)
    if not has_words and not has_nonverbal:
        raise ValueError('Nhập lời cần đọc hoặc thẻ cười/thở dài/hắng giọng được hỗ trợ; không chỉ có thẻ nghỉ.')
    if sum(kind == 'speech' for kind, _ in parts) > 256:
        raise ValueError('Văn bản có quá nhiều đoạn; chia thành các lần tạo ngắn hơn.')
    return parts


def chunk_speech(text, max_words=36, max_chars=240):
    """Keep sentence boundaries and emotion tags intact; bound native requests."""
    chunks, pending = [], []
    for sentence in re.split(r'(?<=[.!?…])\s+|[\r\n]+', text):
        words = re.findall(r'\[[^\]\r\n]+\]|\S+', sentence)
        for word in words:
            if pending and (len(pending) >= max_words or len(' '.join(pending + [word])) > max_chars):
                chunks.append(' '.join(pending))
                pending = []
            pending.append(word)
        if pending and len(pending) >= max_words // 2:
            chunks.append(' '.join(pending))
            pending = []
    if pending:
        chunks.append(' '.join(pending))
    return chunks


def expanded_delivery(text):
    result = []
    for kind, value in parse_delivery(text):
        if kind == 'pause':
            result.append((kind, value))
        else:
            chunks = chunk_speech(value)
            for index, chunk in enumerate(chunks):
                if index:
                    result.append(('pause', .15))
                result.append(('speech', chunk))
    return result


@functools.lru_cache(maxsize=1)
def _pipeline():
    from sea_g2p import SEAPipeline
    return SEAPipeline(lang='vi')


def phonemize(text):
    """Preserve the three emotion tokens supported by the official checkpoint."""
    from sea_g2p import punc_norm
    pieces, cursor = [], 0
    for match in _BRACKETS.finditer(text):
        emotion = _EMOTIONS.get(match.group(1).strip().lower())
        if emotion is None:
            continue
        if text[cursor:match.start()].strip():
            pieces.append(_pipeline().run(text[cursor:match.start()], punc_norm=False))
        pieces.append(f'<|emotion_{emotion}|>')
        cursor = match.end()
    if not pieces:
        return _pipeline().run(text)
    if text[cursor:].strip():
        pieces.append(_pipeline().run(text[cursor:], punc_norm=False))
    result = ' '.join(piece.strip() for piece in pieces if piece.strip())
    result = re.sub(r'\s+([.,!?;:…])', r'\1', result)
    return punc_norm(result)


def _run(command, env, timeout=850):
    result = subprocess.run([str(value) for value in command], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding='utf-8', errors='replace', timeout=timeout, env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        detail = (result.stderr or result.stdout or '')[-2500:]
        raise ValueError(f'VieNeu/audio.cpp lỗi {result.returncode}: {detail or "Kiểm tra engine NPK và thư viện CUDA."}')
    return result


def _assemble_item(item, writing, sources, backend):
    """Publish one complete utterance; another utterance's failure cannot undo it."""
    chunks, output_frames, cursor, peak = [], 0, 0, 0
    with wave.open(str(writing), 'wb') as destination:
        destination.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
        for kind, value in item['parts']:
            start = output_frames / 48000
            if kind == 'pause':
                remaining = round(value * 48000)
                output_frames += remaining
                while remaining:
                    count = min(remaining, 48000)
                    destination.writeframes(b'\0' * count * 2)
                    remaining -= count
            else:
                with wave.open(str(sources[cursor]), 'rb') as source:
                    if (source.getnchannels(), source.getsampwidth(), source.getframerate()) != (1, 2, 48000) or source.getnframes() <= 0:
                        raise ValueError('VieNeu trả âm thanh không hợp lệ.')
                    remaining = source.getnframes()
                    output_frames += remaining
                    while remaining:
                        block = source.readframes(min(remaining, 48000))
                        if not block or len(block) % 2 or len(block) // 2 > remaining:
                            raise ValueError('Âm thanh VieNeu bị ngắt giữa chừng.')
                        samples = array.array('h', block)
                        if sys.byteorder != 'little':
                            samples.byteswap()
                        peak = max(peak, max(map(abs, samples), default=0))
                        destination.writeframes(block)
                        remaining -= len(block) // 2
                cursor += 1
            chunks.append(dict(kind=kind, start=round(start, 6), end=round(output_frames / 48000, 6),
                               **({'text': value} if kind == 'speech' else {})))
    if peak < 3:
        raise ValueError('Giọng VieNeu hoàn toàn im lặng; chưa lưu bản đọc.')
    os.replace(writing, item['output'])
    return dict(sample_rate=48000, duration=output_frames / 48000, backend=backend,
                speech_parts=cursor, chunks=chunks, audio_verification='pcm-complete',
                lexical_verification='unverified')


def synthesize_batch(request, progress=None):
    """One voice per native session; return independent cue files and chunk timing."""
    items = request.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 256:
        raise ValueError('Mỗi batch VieNeu cần 1–256 đoạn lời đọc.')
    normalized, targets = [], set()
    for item in items:
        text, speed = str(item.get('text', '')).strip(), float(item.get('rate', 1))
        if not text or len(text) > 20000 or not math.isfinite(speed) or not .5 <= speed <= 2:
            raise ValueError('Lời đọc cần 1–20.000 ký tự sau từ điển, tốc độ 0,5–2.')
        output = Path(item['output']).resolve()
        if output in targets:
            raise ValueError('Các đoạn VieNeu cần đường dẫn đầu ra riêng.')
        targets.add(output)
        normalized.append(dict(text=text, speed=speed, output=output, parts=expanded_delivery(text)))
    backend = request.get('backend', 'cpu')
    if backend not in ('cpu', 'cuda'):
        raise ValueError('Thiết bị VieNeu không hợp lệ.')
    threads = int(request.get('threads', 4))
    if not 1 <= threads <= 12:
        raise ValueError('Số luồng VieNeu cần từ 1 đến 12.')
    began = time.perf_counter()
    notify = progress or (lambda event: None)
    duty = float(request.get('duty', 1))
    batch_size = int(request.get('batch_size', 64))
    if not .25 <= duty <= 1 or not 1 <= batch_size <= 256:
        raise ValueError('Giới hạn tải batch VieNeu không hợp lệ.')
    env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1', HF_HUB_DISABLE_TELEMETRY='1')
    torch_dll = ROOT / 'runtime/venv/Lib/site-packages/torch/lib'
    if backend == 'cuda' and torch_dll.is_dir():
        env['PATH'] = str(torch_dll) + os.pathsep + env.get('PATH', '')
    writing = [item['output'].with_name(item['output'].name + '.writing') for item in normalized]
    try:
        with tempfile.TemporaryDirectory(prefix='npk-vieneu-', dir=normalized[0]['output'].parent) as temporary:
            folder = Path(temporary)
            records = [(item_index, value) for item_index, item in enumerate(normalized)
                       for kind, value in item['parts'] if kind == 'speech']
            spoken_count = len(records)
            if spoken_count > 2048:
                raise ValueError('Batch có quá nhiều đoạn nhỏ; chia thành các tác vụ ngắn hơn.')
            phonemes = []
            for index, (_, value) in enumerate(records):
                phonemes.append(phonemize(value))
                notify(dict(progress=.05 + .1 * (index + 1) / spoken_count, completed=index + 1,
                            total=spoken_count, stage='phonemize', message=f'Chuẩn bị phát âm {index + 1}/{spoken_count} đoạn'))
            aligned = [None] * spoken_count
            results = [None] * len(normalized)
            item_records = [[index for index, record in enumerate(records) if record[0] == item_index]
                            for item_index in range(len(normalized))]
            attempted = set()
            def publish_ready():
                first_error = None
                for item_index, indices in enumerate(item_records):
                    if item_index in attempted or not all(aligned[index] for index in indices):
                        continue
                    attempted.add(item_index)
                    try:
                        results[item_index] = _assemble_item(normalized[item_index], writing[item_index],
                            [aligned[index] for index in indices], backend)
                    except (ValueError, OSError, wave.Error, EOFError) as error:
                        if first_error is None:
                            first_error = error
                return first_error
            for block_start in range(0, spoken_count, batch_size):
                block = phonemes[block_start:block_start + batch_size]
                block_folder = folder if spoken_count <= batch_size else folder / f'batch{block_start:05d}'
                block_folder.mkdir(exist_ok=True)
                inputs, native = block_folder / 'inputs', block_folder / 'native'
                inputs.mkdir()
                native.mkdir()
                for index, phones in enumerate(block):
                    (inputs / f'part{index:04d}.json').write_text(json.dumps({'text': phones}, ensure_ascii=False), encoding='utf-8')
                command = [request['cli'], '--task', 'tts', '--family', 'vieneu_v3_turbo', '--model', request['model'],
                    '--backend', backend, '--threads', str(threads), '--seed', '42',
                    '--request-option', 'reference_codes_file=' + request['voice_codes'],
                    '--request-option', 'speaker_embedding_file=' + request['speaker_embedding'],
                    '--batch-text-dir', str(inputs), '--out-dir', str(native)]
                stopped = threading.Event()
                def monitor():
                    last = -1
                    while not stopped.wait(.4):
                        count = len(list(native.rglob('*.wav')))
                        if count != last:
                            last = count
                            done = block_start + min(count, len(block))
                            notify(dict(progress=.15 + .65 * done / spoken_count, completed=done,
                                        total=spoken_count, stage='synthesize', message=f'Tạo giọng {done}/{spoken_count} đoạn'))
                watcher = threading.Thread(target=monitor, daemon=True)
                watcher.start()
                block_began = time.perf_counter()
                native_error = None
                try:
                    _run(command, env)
                except Exception as error:
                    native_error = error
                finally:
                    stopped.set()
                    watcher.join(timeout=2)
                block_elapsed = time.perf_counter() - block_began
                produced = sorted(native.rglob('*.wav'))
                if len(produced) != len(block) and native_error is None:
                    native_error = ValueError(f'VieNeu chưa trả đủ đoạn âm thanh ({len(produced)}/{len(block)}). Giữ các câu đã tạo đủ để tiếp tục sau.')
                named = {}
                for path in produced:
                    named.setdefault(path.stem, []).append(path)
                by_name = {name: matches[0] for name, matches in named.items() if len(matches) == 1}
                # A partial set must retain the exact request filenames. Never
                # shift remaining audio onto another item merely by sorting.
                complete = native_error is None and len(produced) == len(block)
                for local_index in range(len(block)):
                    source = by_name.get(f'part{local_index:04d}')
                    if source is None and complete:
                        source = produced[local_index]
                    if source is None:
                        continue
                    index = block_start + local_index
                    target = folder / f'aligned{index:04d}.wav'
                    command = [request['ffmpeg'], '-v', 'error', '-y', '-i', str(source)]
                    speed = normalized[records[index][0]]['speed']
                    if speed != 1:
                        command += ['-af', f'atempo={speed:.8f}']
                    try:
                        _run(command + ['-threads', str(threads), '-filter_threads', str(threads),
                            '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(target)], env, timeout=180)
                        aligned[index] = target
                    except Exception as error:
                        if native_error is None:
                            native_error = error
                    assembly_error = publish_ready()
                    if native_error is None and assembly_error is not None:
                        native_error = assembly_error
                    notify(dict(progress=.15 + .75 * (index + 1) / spoken_count, completed=index + 1,
                                total=spoken_count, stage='align', message=f'Ghép nhịp đọc {index + 1}/{spoken_count} đoạn'))
                if native_error is not None:
                    raise native_error
                done = block_start + len(block)
                notify(dict(progress=.15 + .65 * done / spoken_count, completed=done, total=spoken_count,
                            stage='synthesize', message=f'Tạo giọng {done}/{spoken_count} đoạn'))
                if done < spoken_count and duty < 1:
                    delay = min(10., block_elapsed * (1 / duty - 1))
                    notify(dict(progress=.15 + .65 * done / spoken_count, completed=done, total=spoken_count,
                                stage='rest', message='Nghỉ ngắn giữa các batch để giảm tải máy'))
                    time.sleep(delay)
    finally:
        for temporary_output in writing:
            temporary_output.unlink(missing_ok=True)
    return dict(items=results, elapsed_seconds=time.perf_counter() - began, backend=backend,
                speech_parts=spoken_count, native_batches=math.ceil(spoken_count / batch_size))


def synthesize(request, progress=None):
    text = str(request.get('text', '')).strip()
    if not text or len(text) > 10000:
        raise ValueError('Lời đọc cần 1–10000 ký tự.')
    result = synthesize_batch(request | {'items': [dict(text=text, rate=request.get('rate', 1), output=request['output'])]}, progress)
    return result['items'][0] | {'elapsed_seconds': result['elapsed_seconds'], 'native_batches': result['native_batches']}


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stdin.reconfigure(encoding='utf-8')
    for line in sys.stdin:
        try:
            request = json.loads(line)
            def progress(event):
                print(json.dumps({'event': 'progress', **event}, ensure_ascii=False), flush=True)
            result = synthesize_batch(request, progress) if 'items' in request else synthesize(request, progress)
        except Exception as error:
            result = {'error': str(error)}
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
