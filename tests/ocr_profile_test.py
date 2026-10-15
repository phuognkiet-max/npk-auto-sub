"""Same cropped samples, two CPU profiles, no quality/algorithm change."""
import json
import os
from pathlib import Path
import sys
import time
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
os.environ['VIETDUB_DATA_DIR']=str(root/'tests/profile-data')
from core import DEFAULTS,binary,run
from performance import configure_compute,create_ocr,FramePacer
import numpy as np
import psutil
project=json.loads((root/'data/jobs/a396bb4189544951bb355495ace72685/input.json').read_text(encoding='utf-8'))['project']
frames=[]
for moment in (0,6.5,11):
    raw=run([binary('ffmpeg'),'-v','error','-threads','1','-ss',str(moment),'-i',project['media']['path'],'-frames:v','1','-pix_fmt','bgr24','-f','rawvideo','pipe:1']).stdout
    frames.append(np.frombuffer(raw,np.uint8).reshape(1080,1920,3)[926:1055,560:1338])
reports=[]
for mode in ('quiet','balanced'):
    params=DEFAULTS|{'workload_mode':mode};configure_compute(params)
    engine=create_ocr(params);engine(frames[0]);pacer=FramePacer(params)
    before=time.process_time();began=time.perf_counter();texts=[]
    for frame in frames*6:
        started=time.perf_counter();result,_=engine(frame);texts.append([r[1] for r in result or []]);pacer.rest(started)
    wall=time.perf_counter()-began;cpu=time.process_time()-before
    reports.append({'mode':mode,'wall_seconds':wall,'cpu_seconds':cpu,'average_logical_cores':cpu/wall,'texts':texts})
assert reports[0]['texts']==reports[1]['texts']
assert reports[0]['average_logical_cores']<reports[1]['average_logical_cores']
(root/'tests/studio-results/ocr-profiles.json').write_text(json.dumps({'profiles':reports,'logical_cores':psutil.cpu_count(),'temperature':'Not measured','scope':'OCR crop only, not LaMa/render'},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps([{k:v for k,v in r.items() if k!='texts'} for r in reports]),flush=True)
