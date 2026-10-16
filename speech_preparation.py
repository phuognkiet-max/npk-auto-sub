"""Measured speech admission shared by the worker and read-only preflight.

Cache keys remain byte-compatible with the original raw speech cache. Every
unknown duration is distinguished from a measured constraint, not guessed.
"""
from functools import lru_cache
import json
from pathlib import Path
import os
import uuid

from speech_delivery import delivery_window, effective_speech_rows, fit_rate


@lru_cache(maxsize=4096)
def _verified_duration(path, size, modified):
    from audio_verification import verify_pcm_speech
    result = verify_pcm_speech(path)
    if result['channels'] != 1:
        raise ValueError('Cache giọng cần PCM mono.')
    return result['duration']


def measured_duration(path):
    path = Path(path)
    try:
        stat = path.stat()
        if path.is_symlink() or not path.is_file():
            return None
        return _verified_duration(str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    except (OSError, ValueError):
        return None


def cache_items(project, *, entries=None, plans=None, cache_root=None):
    from voices import resolve_voice, audio_identity
    from subtitle_quality import digest
    from core import DATA
    p = project['params']
    rows = effective_speech_rows(project['segments'], p)
    plans = plans if plans is not None else [
        delivery_window(row, p, i + 1, project['media']['duration']) for i, row in enumerate(rows)]
    if len(plans) != len(rows):
        raise ValueError('Kế hoạch giọng không khớp số câu phụ đề; chưa tạo cache.')
    # Supplied worker plans must be derived from exactly the same cleaned text.
    # A mismatch must fail before model resolution or any cache key is created.
    expected = [delivery_window(row, p, i + 1, project['media']['duration']) for i, row in enumerate(rows)]
    contract_fields = ('mode', 'text', 'start', 'end', 'available', 'rate',
                       'pause_before_ms', 'pause_after_ms')
    if any(any(plan.get(key) != wanted[key] for key in contract_fields)
           for plan, wanted in zip(plans, expected)):
        raise ValueError('Kế hoạch giọng khác nội dung hoặc mốc đã làm sạch; cần lập lại trước khi tạo cache.')
    if entries is None:
        resolved = {}
        entries = []
        for row, plan in zip(rows, plans):
            if not plan['text']:
                entries.append({'id':'caption-only', 'provider':'none'})
                continue
            identifier = row.get('voice_id') or p.get('voice_id', 'piper:vais1000')
            if identifier not in resolved:
                resolved[identifier] = resolve_voice(identifier, require_ready=True)
            entries.append(resolved[identifier])
    if len(entries) != len(rows):
        raise ValueError('Danh sách giọng không khớp số câu phụ đề; chưa tạo cache.')
    root = Path(cache_root) if cache_root is not None else DATA / 'speech-cache'
    identities = {}
    items = []
    for index, (plan, entry) in enumerate(zip(plans, entries)):
        if not plan['text']:
            items.append({'key':'', 'path':root/('caption-only-'+str(index)+'.wav'),
                          'duration':None, 'cache_hit':False, 'plan':plan, 'entry':entry})
            continue
        rate = p.get('voice_speed', 1)
        if float(rate) != plan['rate']:
            rate = plan['rate']
        namespace = 'npk-vieneu-local-speech-v1' if entry.get('provider') == 'vieneu' else 'npk-speech-v1'
        if entry['id'] not in identities:
            identities[entry['id']] = audio_identity(entry, p)
        key = digest([namespace, *identities[entry['id']], plan['text'], rate])
        path = root / (key + '.wav')
        duration = measured_duration(path) if plan['text'] else None
        items.append({'key': key, 'path': path, 'duration': duration, 'cache_hit': duration is not None,
                      'plan': plan, 'entry': entry})
    return items


def delivery_report(project, items, *, refresh=False):
    if project['params'].get('tts_auto_schedule', False):
        from speech_delivery import schedule_speech
        durations = [measured_duration(i['path']) if refresh else i['duration'] for i in items]
        spoken = sum(bool(i['plan']['text']) for i in items)
        measured = sum(bool(i['plan']['text']) and d is not None for i, d in zip(items, durations))
        report = {'schema_version': 2, 'text_source': project['params'].get('tts_text_source'),
                  'cue_count': spoken, 'measured_count': measured, 'unknown_count': spoken-measured,
                  'constraint_count': 0, 'issues': [], 'auto_schedule': True,
                  'status': 'needs_measurement', 'next_action': 'continue'}
        if measured == spoken:
            try:
                plans = schedule_speech([i['plan'] for i in items], durations, project['media']['duration'])
                report.update(status='measured_ready', placements=plans)
            except ValueError as error:
                report.update(status='edit_required', next_action='edit_speech_timing', constraint_count=1,
                              issues=[{'code': 'speech_timing_constraint', 'message': str(error)}])
        return report
    issues, occupied = [], []
    measured = 0
    for index, item in enumerate(items):
        plan = item['plan']
        if not plan['text']:
            continue
        duration = measured_duration(item['path']) if refresh else item['duration']
        if duration is None:
            continue
        measured += 1
        try:
            speed = fit_rate(duration, plan['available'], plan['mode'], index + 1, preserve_text=True)
            end = plan['start'] + duration / speed
            if plan['mode'] == 'natural':
                for start_before, end_before, cue_before in occupied:
                    if plan['start'] < end_before - 1e-9 and start_before < end - 1e-9:
                        raise ValueError(f'Câu {index+1}: lời đọc chồng với câu {cue_before}. '
                                         'Điều chỉnh mốc câu; nội dung và giọng gốc được giữ nguyên.')
                occupied.append((plan['start'], end, index + 1))
        except ValueError as error:
            issues.append({'code': 'speech_timing_constraint', 'index': index, 'cue': index + 1,
                           'message': str(error), 'raw_duration': duration, 'available': plan['available'],
                           'required_tempo': max(1., duration / plan['available']),
                           'start': plan['start'], 'end': plan['end'], 'cache_key': item['key']})
    spoken = sum(bool(item['plan']['text']) for item in items)
    return {'schema_version': 1, 'text_source': project['params'].get('tts_text_source'),
            'cue_count': spoken, 'measured_count': measured, 'unknown_count': spoken - measured,
            'constraint_count': len(issues), 'issues': issues,
            'status': 'edit_required' if issues else 'measured_ready' if measured == spoken else 'needs_measurement',
            'next_action': 'edit_speech_timing' if issues else 'continue'}


class SpeechPreparationError(ValueError):
    def __init__(self, report):
        self.report = report
        first = report['issues'][0]['message']
        extra = (f" Có {report['constraint_count']} câu cần sửa; mở Phụ đề → Kiểm tra để xem toàn bộ."
                 if report['constraint_count'] > 1 else '')
        unknown = report.get('unknown_count', 0)
        measured = (f" Mới đo {report.get('measured_count', 0)}/{report.get('cue_count', 0)} câu; "
                    f"còn {unknown} câu chưa đo, chưa xác nhận toàn bộ lời đọc vừa thời gian."
                    if unknown else '')
        super().__init__(first + extra + measured)


def require_delivery(report, folder=None):
    if folder is not None:
        path = Path(folder) / 'speech-delivery-plan.json'
        pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with pending.open('w', encoding='utf-8') as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    if report['issues']:
        raise SpeechPreparationError(report)


def inspect_cached_delivery(project):
    return delivery_report(project, cache_items(project))
