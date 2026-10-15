"""One actual local-model frame; isolated data, no real projects or output writes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
with tempfile.TemporaryDirectory(prefix='npk-live-lama-',dir=root/'tests') as tmp:
    os.environ['VIETDUB_DATA_DIR']=tmp
    from core import DEFAULTS, probe
    from live_clean_preview import render_frame, KEYS
    params=DEFAULTS | dict(method='lama',max_side=256,dilate=4)
    spec=dict(media=probe(root/'tests/fixtures/demo.mp4'),params={k:params[k] for k in KEYS},
              regions=[dict(x=100,y=265,w=420,h=75,start=0,end=8)],time=0)
    began=time.perf_counter()
    jpeg=render_frame(spec)
    elapsed=time.perf_counter()-began
    import cv2
    import numpy as np
    image=cv2.imdecode(np.frombuffer(jpeg,np.uint8),cv2.IMREAD_COLOR)
    assert image is not None and image.shape[:2]==(360,640)
    out=root/'tests/live-clean-ui-results'
    out.mkdir(exist_ok=True)
    (out/'lama-frame.jpg').write_bytes(jpeg)
    result=dict(method='lama',seconds=round(elapsed,3),bytes=len(jpeg),shape=list(image.shape))
    (out/'lama-smoke.json').write_text(json.dumps(result),encoding='utf-8')
    print(json.dumps(result),flush=True)
