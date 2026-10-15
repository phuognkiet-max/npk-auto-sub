"""Paginated metadata for standalone speech; generated files stay NPK-owned."""
from pathlib import Path
from core import DATA, all_items, get, put
from storage_manager import ID, owned, measured


def item(job):
    result = job.get('result', {})
    folder = owned('jobs/' + job['id'])
    names = [name for name in ('speech.wav', 'speech.mp3') if name in result.get('files', [])]
    files = [name for name in names if (folder / name).is_file() and not (folder / name).is_symlink()]
    return {'id': job['id'], 'name': job.get('speech_name') or 'Bản đọc ' + job['id'][:8],
            'created': job.get('created'), 'ended': job.get('ended'), 'state': job.get('state'),
            'status': 'trash' if job.get('storage_removed') else job.get('state'),
            'bytes': sum((folder / name).stat().st_size for name in files), 'files': files,
            'duration': result.get('duration', 0), 'voice_id': result.get('voice_id') or job.get('configuration', {}).get('voice_id', ''),
            'voice': result.get('voice_name') or result.get('name') or result.get('voice_id', ''),
            'message': job.get('message', '')}


def listing(query='', offset=0, limit=30):
    if len(query) > 200 or offset < 0 or offset > 1000000 or not 1 <= limit <= 100:
        raise ValueError('Phạm vi thư viện bản đọc không hợp lệ.')
    query = query.strip().casefold()
    rows = [j for j in all_items('job') if j.get('kind') == 'text_to_speech' and ID.fullmatch(j['id']) and not j.get('storage_removed')]
    if query:
        rows = [j for j in rows if query in (j.get('speech_name', '') + ' ' + j.get('result', {}).get('voice_id', '') + ' ' + j['id']).casefold()]
    return {'items': [item(j) for j in rows[offset:offset + limit]], 'total': len(rows), 'offset': offset, 'limit': limit}


def rename(identifier, name):
    job = get('job', identifier) if ID.fullmatch(identifier) else None
    if not job or job.get('kind') != 'text_to_speech' or job.get('storage_removed'):
        raise ValueError('Không tìm thấy bản đọc để đổi tên.')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160 or any(ord(c) < 32 for c in name):
        raise ValueError('Tên bản đọc cần 1–160 ký tự, không có ký tự điều khiển.')
    job['speech_name'] = name.strip()
    put('job', identifier, job)
    return item(job)
