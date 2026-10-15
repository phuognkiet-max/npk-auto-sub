"""Chunked local narration with reusable paragraph audio and bounded memory."""
from pathlib import Path
import json
import os
import shutil
import uuid
import wave
from core import DATA, binary, run
from performance import ffmpeg_limits


def valid_wave(path):
    try:
        from audio_verification import verify_pcm_speech
        return verify_pcm_speech(path)['channels'] == 1
    except (OSError, wave.Error, EOFError, ValueError):
        return False


def paragraph_audio(segments, params, folder, progress=None):
    from voices import resolve_voice, audio_identity, synthesize_text, synthesize_vieneu_batch
    from subtitle_quality import digest
    cache = DATA / 'speech-cache'
    cache.mkdir(exist_ok=True)
    paths, metadata, pending = [], [], []
    cache_hits = 0
    for index, segment in enumerate(segments):
        entry = resolve_voice(segment.get('voice_id') or params.get('voice_id', 'piper:vais1000'), require_ready=True)
        rate = float(segment.get('rate', params.get('voice_speed', 1)))
        key = digest(['npk-narration-paragraph-v1', *audio_identity(entry, params), segment['text'], rate])
        cached = cache / (key + '.wav')
        paths.append(cached)
        metadata.append({'voice_id': entry['id'], 'voice_name': entry['name'], 'text': segment['text'],
                         'rate': rate, 'pause_after_ms': segment.get('pause_after_ms', 0), 'cache_hit': valid_wave(cached),
                         'lexical_verification': 'unverified'})
        if metadata[-1]['cache_hit']:
            cache_hits += 1
            metadata[-1]['audio_verification'] = 'pcm-complete'
            metadata[-1]['engine_metadata_complete'] = False
            try:
                proof = json.loads(cached.with_suffix('.metadata.json').read_text(encoding='utf-8'))
                metadata[-1]['engine_metadata_complete'] = proof.get('engine_metadata_complete') is True
            except (OSError, ValueError, AttributeError):
                pass
        elif not any(item['key'] == key for item in pending):
            pending.append({'entry': entry, 'text': segment['text'], 'rate': rate, 'key': key,
                            'identity': audio_identity(entry, params),
                            'path': folder / (key + '.' + uuid.uuid4().hex + '.part.wav'), 'target': cached})
    def promote(item, details=None):
        current = resolve_voice(item['entry']['id'], require_ready=True)
        if audio_identity(current, params) != item['identity']:
            raise ValueError('Giọng/model đã thay đổi; không lưu đoạn vào cache của cấu hình cũ.')
        if not valid_wave(item['path']):
            raise ValueError('Bản đọc đoạn chưa có WAV hợp lệ; chưa ghép âm thanh.')
        os.replace(item['path'], item['target'])
        proof = {'audio_verification': 'pcm-complete', 'lexical_verification': 'unverified',
                 'engine_metadata_complete': bool((details or {}).get('engine_metadata_complete', False))}
        target = item['target'].with_suffix('.metadata.json')
        temporary = target.with_name(target.name + '.tmp')
        try:
            temporary.write_text(json.dumps(proof), encoding='utf-8')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        for index, path in enumerate(paths):
            if path == item['target']:
                metadata[index].update(proof)
    try:
        vie = [item for item in pending if item['entry'].get('provider') == 'vieneu']
        for offset in range(0, len(vie), 128):
            batch = vie[offset:offset + 128]
            try:
                results = synthesize_vieneu_batch(batch, params=params, progress=progress)
            except Exception as error:
                partial = getattr(error, 'partial_results', {})
                for index, item in enumerate(batch):
                    if not item['path'].is_file():
                        continue
                    try:
                        promote(item, partial.get(index, {}))
                    except (ValueError, OSError):
                        pass
                raise
            for index, item in enumerate(batch):
                promote(item, results[index] if isinstance(results, list) else {})
        for index, item in enumerate(pending):
            if item['entry'].get('provider') == 'vieneu':
                continue
            synthesize_text(item['entry']['id'], item['text'], item['path'], params | {'voice_speed': item['rate']})
            promote(item)
            if progress:
                progress({'progress': (index + 1) / max(1, len(pending)), 'message': f'Tạo đoạn {index + 1}/{len(pending)}'})
    finally:
        for item in pending:
            item['path'].unlink(missing_ok=True)
    return paths, metadata, cache_hits


def create_narration(segments, params, folder, destination, progress=None):
    paths, metadata, hits = paragraph_audio(segments, params, folder, progress)
    rate = 48000
    cursor, chunks = 0, []
    aligned = folder / 'narration-aligned.tmp.wav'
    try:
        with wave.open(str(destination), 'wb') as output:
            output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
            for path, info in zip(paths, metadata):
                run([binary('ffmpeg'), '-v', 'error', '-y', *ffmpeg_limits(params), '-i', path,
                     '-ac', '1', '-ar', str(rate), '-c:a', 'pcm_s16le', aligned], timeout=180)
                began = cursor / rate
                with wave.open(str(aligned), 'rb') as audio:
                    while block := audio.readframes(rate * 10):
                        output.writeframes(block)
                        cursor += len(block) // 2
                chunks.append(info | {'start': began, 'end': cursor / rate})
                remaining = round(float(info['pause_after_ms']) / 1000 * rate)
                while remaining:
                    count = min(remaining, rate * 10)
                    output.writeframes(b'\0\0' * count)
                    remaining -= count
                    cursor += count
        return {'chunks': chunks, 'cache_hits': hits, 'generated': len(segments) - hits,
                'voice_name': ' / '.join(dict.fromkeys(x['voice_name'] for x in metadata)),
                'voice_id': metadata[0]['voice_id'] if len({x['voice_id'] for x in metadata}) == 1 else 'multiple',
                'duration': cursor / rate, 'sample_rate': rate}
    finally:
        aligned.unlink(missing_ok=True)
