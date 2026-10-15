import json
from pathlib import Path
import sys
import time
import numpy as np
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from core import binary,run
from performance import configure_compute
from rapidocr_onnxruntime import RapidOCR
p=json.loads((root/'data/jobs/a396bb4189544951bb355495ace72685/input.json').read_text(encoding='utf-8'))['project']
configure_compute({'workload_mode':'quiet'})
images=[]
for t in (0,6.5,11):
    raw=run([binary('ffmpeg'),'-v','error','-threads','1','-ss',str(t),'-i',p['media']['path'],'-frames:v','1','-pix_fmt','bgr24','-f','rawvideo','-']).stdout
    images.append(np.frombuffer(raw,dtype=np.uint8).reshape(1080,1920,3)[926:1055,560:1338])
reports=[]
for mode in ('min','max'):
    engine=RapidOCR(intra_op_num_threads=1,inter_op_num_threads=1,det_limit_type=mode)
    for t,image in zip((0,6.5,11),images):
        start=time.perf_counter();cpu=time.process_time();result,_=engine(image)
        row=dict(mode=mode,time=t,seconds=time.perf_counter()-start,cpu_seconds=time.process_time()-cpu,
            detector_shape=list(engine.text_det.preprocess_op(image).shape),text=[v[1] for v in result or []])
        reports.append(row);print(json.dumps(row,ensure_ascii=False),flush=True)
    del engine
(root/'tests/acceptance-0.7/ocr-scale-benchmark.json').write_text(json.dumps(reports,ensure_ascii=False,indent=2),encoding='utf-8')
