"""Isolated native acceptance window. Never opens the user's project database."""
import copy
import json
import os
from pathlib import Path
import sys
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
os.environ['VIETDUB_DATA_DIR']=str(root/'tests/native-acceptance')
from core import DATA,DEFAULTS,put
original=root/'data/jobs/a396bb4189544951bb355495ace72685/input.json'
project=json.loads(original.read_text(encoding='utf-8'))['project']
result=json.loads((root/'tests/acceptance-0.7-final/isolated-data/jobs/acceptance-final/result.json').read_text(encoding='utf-8'))
project.update(name='Nghiệm thu · Mèo thuê nhà',segments=result['segments'],coverage=result['coverage'],artifacts={})
project['params']=DEFAULTS|project['params']
project['params'].update(use_clean=False,pipeline_translate=False,voice_gain=0,original_gain=1)
project['extraction_regions']=copy.deepcopy(project['regions'])
put('project',project['id'],project)
import webview
create_window=webview.create_window
def named_window(title,*args,**kwargs):
    return create_window('NPK Auto Sub — Kiểm thử riêng',*args,**kwargs)
webview.create_window=named_window
import app
app.main()
