"""Read-only audit of the reported video; writes samples only under tests/."""
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
out=root/'tests/vlog-cleanup-audit'
out.mkdir(exist_ok=True)
with sqlite3.connect(f'file:{root.as_posix()}/data/studio.sqlite3?mode=ro',uri=True) as db:
    row=db.execute('SELECT value FROM objects WHERE kind=? AND id=?',('project','29463f3e388847e3afd7e1eabf93ada7')).fetchone()
    project=json.loads(row[0])
with tempfile.TemporaryDirectory(prefix='npk-clean-audit-',dir=root/'tests') as tmp:
    os.environ['VIETDUB_DATA_DIR']=tmp
    import cv2
    from core import DEFAULTS, MODELS
    from cleaner import Cleaner
    from fast_cover import preview_fast,clean_fast
    from worker import read_frame,write_image
    cv2.setNumThreads(1)
    project['params']=DEFAULTS | project['params']
    frame=read_frame(project['media']['path'],0)
    write_image(out/'source.jpg',frame)
    checks=[]
    for name,params in [
        ('blur-32',dict(method='blur',blur_radius=32,cover_opacity=1,edge_feather=4)),
        ('stretch-offset',dict(method='stretch',stretch_height=8,stretch_offset=16,cover_opacity=1,edge_feather=4)),
        ('inpaint-2',dict(method='telea',inpaint_radius=2,temporal=False,cover_opacity=1,edge_feather=0)),
        ('inpaint-glyph',dict(method='telea',inpaint_radius=2,mask_mode='glyph',temporal=False,cover_opacity=1,edge_feather=0)),
    ]:
        p=copy.deepcopy(project);p['params'].update(params)
        folder=out/name;folder.mkdir(exist_ok=True)
        began=time.perf_counter()
        if params['method']=='telea':
            image,mask=Cleaner(MODELS/'unused',p['params'])(frame,p['regions'],0)
            write_image(folder/'after.jpg',image)
        else:
            preview_fast(p,0,folder)
        checks.append(dict(sample=name,seconds=round(time.perf_counter()-began,3),time=0,params=params))
        if name=='blur-32':
            result=clean_fast(p,dict(kind='clip',time=0),folder,lambda **event:None)
            checks[-1]['clip_seconds']=round(result['elapsed_seconds'],3)
            checks[-1]['clip_duration']=result['duration']
    (out/'measurements.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(checks,ensure_ascii=False),flush=True)
