"""Optional acoustic speaker grouping suggestions; never applies voice choices.

This is cue-level CAM++ similarity clustering, not full diarization. Short,
overlapping, emotional or music-heavy speech needs manual review.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import subprocess
import tempfile
import time
import wave


def cluster_embeddings(embeddings, threshold=.72, max_groups=12):
    """Deterministic bounded cosine clustering, with an explicit unassigned case."""
    import numpy as np
    if not .3 <= float(threshold) <= .95 or not 1 <= int(max_groups) <= 12:
        raise ValueError('Ngưỡng nhóm giọng cần 0,3–0,95 và số nhóm từ 1 đến 12.')
    if len(embeddings) > 200:
        raise ValueError('Mỗi lượt chỉ gợi ý tối đa 200 câu.')
    centers, sums, assignments = [], [], []
    normalized = []
    for embedding in embeddings:
        value = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(value))
        if value.shape != (192,) or not np.isfinite(value).all() or norm < 1e-6:
            raise ValueError('Đặc trưng người nói không hợp lệ.')
        value = value / norm
        normalized.append(value)
        similarities = [float(np.dot(value, center)) for center in centers]
        best = int(np.argmax(similarities)) if similarities else None
        if best is not None and similarities[best] >= threshold:
            sums[best] += value
            centers[best] = sums[best] / np.linalg.norm(sums[best])
            assignments.append(best)
        elif len(centers) < max_groups:
            assignments.append(len(centers))
            centers.append(value.copy())
            sums.append(value.copy())
        else:
            assignments.append(None)
    return [dict(group_index=group, similarity=round(float(np.dot(value, centers[group])), 6) if group is not None else None,
                 needs_review=True, confidence_type='cosine_similarity_not_probability')
            for value, group in zip(normalized, assignments)]


def speaker_groups_signature(project, segment_indices=None, params=None):
    """Cheap source/cue identity for stale-result checks; performs no inference."""
    from voice_enrollment import ENCODER_SHA256
    rows = project.get('segments', [])
    indices = list(range(len(rows))) if segment_indices is None else list(segment_indices)
    if not indices or len(indices) > 200 or len(set(indices)) != len(indices) or \
            any(type(index) is not int or index < 0 or index >= len(rows) for index in indices):
        raise ValueError('Chọn từ 1 đến 200 câu có thật để gợi ý nhóm người nói.')
    options = params if params is not None else project.get('params', {})
    threshold = float(options.get('speaker_similarity_threshold', .72))
    max_groups = int(options.get('speaker_max_groups', 12))
    cluster_embeddings([], threshold, max_groups)
    source = Path(project['media']['path']).resolve(strict=True)
    stat = source.stat()
    identity = [str(source), stat.st_size, stat.st_mtime_ns,
                [[index, rows[index]['start'], rows[index]['end']] for index in indices],
                ENCODER_SHA256, threshold, max_groups]
    return hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode('utf-8')).hexdigest()


def analyze_speakers(project, folder, params=None, progress=None, segment_indices=None):
    import numpy as np
    from core import binary, NO_WINDOW
    from performance import profile_for, FramePacer
    from voice_enrollment import enrollment_status, extract_embedding, ENCODER_SHA256
    options = params if params is not None else project.get('params', {})
    if not enrollment_status()['available']:
        raise ValueError(enrollment_status()['message'])
    rows = project.get('segments', [])
    if not rows:
        raise ValueError('Cần có các câu phụ đề trước khi gợi ý nhóm người nói.')
    indices = list(range(len(rows))) if segment_indices is None else list(segment_indices)
    if not indices or len(indices) > 200 or len(set(indices)) != len(indices) or \
            any(isinstance(index, bool) or not isinstance(index, int) or index < 0 or index >= len(rows) for index in indices):
        raise ValueError('Chọn từ 1 đến 200 câu có thật để gợi ý nhóm người nói.')
    threshold = float(options.get('speaker_similarity_threshold', .72))
    max_groups = int(options.get('speaker_max_groups', 12))
    # Validate options before decoding any audio.
    cluster_embeddings([], threshold, max_groups)
    source = Path(project['media']['path']).resolve()
    if not source.is_file():
        raise ValueError('Không thấy video/âm thanh nguồn để phân nhóm người nói.')
    signature = speaker_groups_signature(project, indices, options)
    output = Path(folder).resolve()
    output.mkdir(parents=True, exist_ok=True)
    profile, pacer = profile_for(options), FramePacer(options)
    notify = progress or (lambda event: None)
    warnings = ['Đây là gợi ý theo độ giống âm sắc, không xác nhận danh tính. Nghe lại và gán giọng thủ công.',
                'Nhạc nền, tiếng chồng nhau, cảm xúc và câu ngắn có thể làm nhóm sai.']
    embeddings, valid, skipped = [], [], []
    with tempfile.TemporaryDirectory(prefix='npk-speaker-groups-', dir=output) as temporary:
        sample = Path(temporary) / 'cue.wav'
        for done, index in enumerate(indices):
            began = time.perf_counter()
            row = rows[index]
            start, end = float(row['start']), float(row['end'])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
                skipped.append(dict(segment_index=index, reason='Mốc thời gian không hợp lệ'))
            elif end - start < 1.5:
                skipped.append(dict(segment_index=index, reason='Câu ngắn hơn 1,5 giây'))
            else:
                command = [binary('ffmpeg'), '-v', 'error', '-xerror', '-y', '-ss', str(start), '-i', source,
                    '-t', str(min(8., end - start)), '-threads', str(profile['threads']), '-vn', '-ac', '1',
                    '-ar', '16000', '-c:a', 'pcm_s16le', sample]
                result = subprocess.run([str(value) for value in command], capture_output=True, text=True,
                    encoding='utf-8', errors='replace', timeout=60, creationflags=NO_WINDOW)
                if result.returncode:
                    raise ValueError('Không đọc được âm thanh nguồn: ' + (result.stderr or result.stdout)[-1200:])
                with wave.open(str(sample), 'rb') as audio:
                    signal = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768
                if signal.size < 16000 * 1.4 or float(np.sqrt(np.mean(signal ** 2))) < .001:
                    skipped.append(dict(segment_index=index, reason='Đoạn quá ngắn sau giải mã hoặc không có tiếng rõ'))
                else:
                    embeddings.append(extract_embedding(sample, profile['threads']))
                    valid.append(index)
            notify(dict(progress=(done + 1) / len(indices) * .9, completed=done + 1, total=len(indices),
                        message=f'Đối chiếu chất giọng {done + 1}/{len(indices)} câu'))
            if done + 1 < len(indices):
                pacer.rest(began)
    if not embeddings:
        raise ValueError('Chưa có câu đủ dài và rõ để gợi ý nhóm người nói. Chọn đoạn nói rõ từ 1,5 giây trở lên.')
    clustered = cluster_embeddings(embeddings, threshold, max_groups)
    suggestions, groups = [], {}
    for index, assignment in zip(valid, clustered):
        group_index = assignment['group_index']
        group_id = 'speaker-' + str(group_index + 1) if group_index is not None else None
        suggestions.append(dict(segment_index=index, speaker_id=group_id, group_id=group_id,
            similarity=assignment['similarity'], confidence=assignment['similarity'], needs_review=True,
            confidence_type=assignment['confidence_type'], start=rows[index]['start'], end=rows[index]['end']))
        if group_id:
            group = groups.setdefault(group_id, dict(id=group_id, label='Nhóm giọng ' + str(group_index + 1),
                                                     segment_indices=[], representative_index=index))
            group['segment_indices'].append(index)
        else:
            warnings.append(f'Câu {index + 1}: chưa gán nhóm vì đã đạt giới hạn {max_groups} nhóm và độ giống thấp.')
    for group in groups.values():
        group['count'] = len(group['segment_indices'])
    if signature != speaker_groups_signature(project, indices, options):
        raise ValueError('Video nguồn đã thay đổi khi đang phân nhóm. Chạy lại để đối chiếu đúng bản ghi.')
    document = dict(schema=1, groups=list(groups.values()), suggestions=suggestions, skipped=skipped,
                    warnings=warnings, source_signature=signature, threshold=threshold, max_groups=max_groups,
                    analyzed=len(valid), requested=len(indices), segment_indices=indices, automatic_voice_assignment=False)
    temporary = output / 'speaker-groups.json.part'
    try:
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(output / 'speaker-groups.json')
    finally:
        temporary.unlink(missing_ok=True)
    notify(dict(progress=1, message=f'Đã gợi ý {len(groups)} nhóm; cần nghe lại và gán giọng'))
    return document | {'type': 'speaker_groups', 'files': ['speaker-groups.json']}
