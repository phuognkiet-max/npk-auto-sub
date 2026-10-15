import json
from pathlib import Path
import sys
import time
import cv2
import numpy as np
root=Path(__file__).resolve().parents[1];sys.path.insert(0,str(root))
from core import binary,run
p=json.loads((root/'data/jobs/a396bb4189544951bb355495ace72685/input.json').read_text(encoding='utf-8'))['project']
src=p['media']['path'];cap=cv2.VideoCapture(src,cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,1])
rows=[]
try:
    for t in (0,3,6,6.5,7,10.5,11,14,14.5):
        start=time.perf_counter();cap.set(cv2.CAP_PROP_POS_MSEC,t*1000);ok,a=cap.read();elapsed=time.perf_counter()-start
        out=run([binary('ffmpeg'),'-v','error','-threads','1','-ss',str(t),'-i',src,'-frames:v','1','-pix_fmt','bgr24','-f','rawvideo','-'])
        b=np.frombuffer(out.stdout,dtype=np.uint8).reshape(a.shape)
        crop=(slice(926,1055),slice(560,1338))
        rows.append({'time':t,'seek_seconds':elapsed,'crop_mae':float(np.abs(a[crop].astype('float32')-b[crop]).mean())})
finally:cap.release()
(root/'tests/acceptance-0.7/seeking.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
print(json.dumps(rows),flush=True)
