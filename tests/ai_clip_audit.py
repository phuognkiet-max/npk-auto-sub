"""Reproduce the reported clip with real LaMa; read-only source, isolated outputs."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
with tempfile.TemporaryDirectory(prefix='npk-ai-audit-', dir=root/'tests') as isolated:
    os.environ['VIETDUB_DATA_DIR'] = isolated
    from core import DEFAULTS, probe, binary, run
    from performance import configure_compute
    from job_progress import verify_outputs
    from worker import clean, read_frame, write_image
    from cleaner import make_mask
    import cv2
    import numpy as np
    original = json.loads((root/'data/jobs/6eef9724e245463caf09904b11dfdf6f/input.json').read_text(encoding='utf-8'))
    source = original['project']
    keys = {'method','mask_mode','mask_source','max_side','context','dilate','temporal',
            'workload_mode','cover_opacity','edge_feather','inpaint_radius'}
    project = dict(media=probe(source['media']['path']), regions=source['regions'], artifacts={},
                   params=DEFAULTS | {k:source['params'][k] for k in keys})
    configure_compute(project['params'])
    out = root/'tests/ai-clip-audit-results'/time.strftime('%Y%m%d-%H%M%S')
    out.mkdir(parents=True)
    job = dict(kind='clip', time=float(sys.argv[1]) if len(sys.argv)>1 else original['job']['time'])
    result = clean(project, job, out)
    result['verification'] = verify_outputs(result, project, job, out)
    assert result['frames'] == 90 and abs(result['duration']-3) < .001, result
    assert result['modified_frames'] == 90 and result['ai_inference'], result
    rows = []
    for label, offset in [('first',0), ('middle',1.5), ('last',89/30)]:
        moment = result['start'] + offset
        before = read_frame(project['media']['path'], moment)
        after = read_frame(out/'clean.mp4', offset)
        mask = make_mask(before, project['regions'], project['params'], moment)
        difference = cv2.absdiff(before, after)
        roi_mae = float(difference[mask>0].mean())
        outside_mae = float(difference[mask==0].mean())
        assert roi_mae > 2, (label, roi_mae)
        assert outside_mae < 8, (label, outside_mae)
        write_image(out/f'{label}-before.jpg', before)
        write_image(out/f'{label}-after.jpg', after)
        rows.append(dict(frame=label,time=moment,roi_mae=roi_mae,outside_mae=outside_mae))
    # Decode every packet of audio and video, not just metadata / first frame.
    run([binary('ffmpeg'),'-v','error','-xerror','-i',out/'clean.mp4','-f','null','-'],timeout=60)
    report = result | dict(frames_sampled=rows, settings={k:project['params'][k] for k in keys})
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(output=str(out), result=report),ensure_ascii=False),flush=True)
