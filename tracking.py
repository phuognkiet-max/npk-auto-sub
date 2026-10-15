"""Sampled OCR masks, separate from speech/subtitle timing and original projects."""
import bisect
import hashlib
import json
import math
import re
from pathlib import Path
from core import DATA, safe_owned

def tracking_signature(project):
    source = Path(project['media']['path'])
    payload = ['ocr-mask-v1', str(source.resolve()), source.stat().st_size,
               source.stat().st_mtime_ns, project['regions'],
               project['params'].get('ocr_fps', 2)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

class MaskTimeline:
    def __init__(self, samples):
        self.samples = samples
        self.starts = [s['start'] for s in samples]
        last_end = -1
        for sample in samples:
            start, end = sample['start'], sample['end']
            if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end and start >= last_end):
                raise ValueError('Mốc thời gian mặt nạ OCR không hợp lệ.')
            last_end = end

    def at(self, timestamp):
        index = bisect.bisect_right(self.starts, timestamp) - 1
        if index < 0 or timestamp >= self.samples[index]['end']:
            return []
        return self.samples[index]['regions']

def cleanup_regions(project):
    if project['params'].get('mask_source', 'manual') != 'tracked':
        return lambda timestamp: project['regions']
    artifact = project.get('artifacts', {}).get('scan', {})
    if artifact.get('tracking_signature') != tracking_signature(project):
        raise ValueError('Chưa quét mặt nạ OCR, hoặc video/vùng/mật độ đọc đã thay đổi. Bấm Quét chữ theo thời gian trước.')
    identifier = artifact.get('job_id', '')
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('Kết quả quét OCR không hợp lệ.')
    path = safe_owned(DATA / 'jobs' / identifier / 'masks.json', DATA / 'jobs')
    document = json.loads(path.read_text(encoding='utf-8'))
    if document['tracking_signature'] != artifact['tracking_signature']:
        raise ValueError('Mặt nạ không khớp video hiện tại. Quét lại trước khi xử lý.')
    return MaskTimeline(document['samples']).at
