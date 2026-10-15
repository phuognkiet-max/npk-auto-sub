"""Real cached Whisper on a short user clip; no downloads and no live API."""
import json
import os
from pathlib import Path
import sys
import time

root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
out=root/'tests/studio-asr-results'
out.mkdir(exist_ok=True)
os.environ['VIETDUB_DATA_DIR']=str(out/'isolated-data')
os.environ['HF_HUB_OFFLINE']='1'
os.environ['TRANSFORMERS_OFFLINE']='1'
from core import DEFAULTS
from performance import configure_compute
from worker import asr
from job_progress import verify_outputs

project=json.loads((root/'data/jobs/a396bb4189544951bb355495ace72685/input.json').read_text(encoding='utf-8'))['project']
project['params']=DEFAULTS|project['params']|dict(asr_model='small',asr_device='cpu',
    source_language='zh',extraction_start=0,extraction_end=12,workload_mode='quiet')
configure_compute(project['params'])
began=time.perf_counter()
result=asr(project,{'kind':'asr'},out)
result['verification']=verify_outputs(result,project,{'kind':'asr'},out)
assert result['segments'],'No speech recognized in the known spoken excerpt'
assert all(0<=s['start']<s['end']<=12.3 for s in result['segments'])
assert result['coverage']['analyzed_end']<13,'Partial excerpt must not claim full video coverage'
report={'engine':'Cached Whisper small · CPU int8 · quiet','network':'disabled',
        'elapsed_seconds':time.perf_counter()-began,'range':[0,12],
        'segments':result['segments'],'coverage':result['coverage'],'verification':result['verification']}
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(report,ensure_ascii=False))
