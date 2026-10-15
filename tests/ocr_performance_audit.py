"""Opt-in few-crop CPU benchmark; never runs or changes production OCR jobs."""
import json
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'tests/ocr-performance-results'
JOB = 'f38249f49767476cbe7927536c222cbd'
sys.path.insert(0, str(ROOT))


def main():
    OUT.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='npk-ocr-audit-', dir=ROOT / 'tests') as owned:
        os.environ['VIETDUB_DATA_DIR'] = owned
        import cv2
        import numpy as np
        from core import binary, run
        from performance import configure_compute, create_ocr, profile_for
        spec = json.loads((ROOT / 'data/jobs' / JOB / 'input.json').read_text(encoding='utf-8'))
        checkpoint = json.loads((ROOT / 'data/jobs' / JOB / 'recognition-checkpoint.json').read_text(encoding='utf-8'))
        rows = checkpoint['rows']
        moments = [rows[index]['start'] for index in (0, len(rows) // 2, len(rows) - 1)]
        media, original = spec['project']['media'], spec['project']['params']
        source = Path(media['path'])
        source_stat = source.stat()
        images = []
        for index, moment in enumerate(moments):
            raw = run([binary('ffmpeg'), '-v', 'error', '-threads', '1', '-ss', str(moment),
                '-i', source, '-frames:v', '1', '-pix_fmt', 'bgr24', '-f', 'rawvideo', 'pipe:1'], timeout=30).stdout
            frame = np.frombuffer(raw, np.uint8).reshape(media['height'], media['width'], 3)
            image = np.ascontiguousarray(frame[int(media['height'] * .6):])
            images.append(image)
            cv2.imwrite(str(OUT / f'crop-{index+1}.png'), image)
        report = dict(job_id=JOB, platform=platform.platform(), source_unmodified=False,
                      scope='Three real subtitle crops, CPU only; not a full-video benchmark', moments=moments,
                      original_params={key: original[key] for key in ('workload_mode', 'ocr_fps', 'ocr_scale_mode')},
                      profiles=[])
        connection = sqlite3.connect((ROOT / 'data/studio.sqlite3').as_uri() + '?mode=ro', uri=True)
        try:
            value = connection.execute('SELECT value FROM objects WHERE kind=? AND id=?', ('job', JOB)).fetchone()
            job = json.loads(value[0])
            report['recorded_job_wall_seconds'] = job['ended'] - job['started']
            report['recorded_job_state'] = job['state']
        finally:
            connection.close()
        for mode in ('quiet', 'balanced', 'fast'):
            params = original | {'workload_mode': mode}
            configure_compute(params)
            began = time.perf_counter()
            engine = create_ocr(params)
            sessions = [engine.text_det.infer.session, engine.text_cls.infer.session, engine.text_rec.session.session]
            session_info = [dict(providers=session.get_providers(), intra=session.get_session_options().intra_op_num_threads,
                                 inter=session.get_session_options().inter_op_num_threads) for session in sessions]
            assert all(row['providers'] == ['CPUExecutionProvider'] for row in session_info)
            construction = time.perf_counter() - began
            engine(images[0])  # one warm crop, separate from the three measurements.
            measured = []
            for moment, image in zip(moments, images):
                began, cpu = time.perf_counter(), time.process_time()
                result, elapsed = engine(image)
                wall, active = time.perf_counter() - began, time.process_time() - cpu
                measured.append(dict(moment=moment, wall_seconds=wall, cpu_seconds=active,
                    stage_seconds=elapsed, crop_shape=list(image.shape),
                    detector_shape=list(engine.text_det.preprocess_op(image).shape),
                    recognized=[dict(text=row[1], confidence=float(row[2]), box=np.asarray(row[0]).tolist()) for row in result or []]))
            profile = profile_for(params)
            value = dict(mode=mode, construction_seconds=construction, sessions=session_info, measured=measured,
                mean_active_seconds=statistics.mean(row['wall_seconds'] for row in measured),
                estimated_mean_with_pacing=statistics.mean(row['wall_seconds'] for row in measured) / profile['duty'])
            report['profiles'].append(value)
            print(json.dumps({key: value[key] for key in ('mode', 'construction_seconds', 'mean_active_seconds', 'estimated_mean_with_pacing')}, ensure_ascii=True), flush=True)
            del engine
        report['same_texts_across_profiles'] = all(
            [[row['text'] for row in sample['recognized']] for sample in value['measured']] ==
            [[row['text'] for row in sample['recognized']] for sample in report['profiles'][0]['measured']]
            for value in report['profiles'])
        current = source.stat()
        report['source_unmodified'] = (source_stat.st_size, source_stat.st_mtime_ns) == (current.st_size, current.st_mtime_ns)
        assert report['source_unmodified']
        (OUT / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: report[key] for key in ('recorded_job_wall_seconds', 'same_texts_across_profiles', 'source_unmodified')}), flush=True)


if __name__ == '__main__':
    main()
