"""User-provided two-minute clip, isolated DB; existing clean copied, never modified.
Network probe uses only the previously selected provider/key/model. No fallback model.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
root=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(root))
out=root/('tests/acceptance-0.7-final' if '--wide' in sys.argv else 'tests/acceptance-0.7-optimized');out.mkdir(exist_ok=True)
os.environ['VIETDUB_DATA_DIR']=str(out/'isolated-data')
from core import DATA,DEFAULTS
from performance import configure_compute
from worker import ocr,pipeline
from job_progress import verify_outputs
from subtitle_quality import inspect_subtitles
import keyring
import providers
original=root/'data/jobs/a396bb4189544951bb355495ace72685'
project=json.loads((original/'input.json').read_text(encoding='utf-8'))['project']
project['params']=DEFAULTS|project['params']
project['params'].update(workload_mode='quiet',ocr_fps=2,extraction_start=0,extraction_end=0)
# A real mid-frame subtitle was visually located at 85s, outside the old bottom
# mask. Read that area separately; do not silently change the user's clean mask.
project['extraction_regions']=copy.deepcopy(project['regions'])+[
    dict(x=350,y=630,w=1000,h=230,start=74,end=103.5)]
if '--wide' in sys.argv:
    project['extraction_regions']=[dict(x=79,y=918,w=1770,h=135,start=0,end=project['media']['duration']),
                                  dict(x=350,y=630,w=1000,h=230,start=84,end=88)]
report={'source_seconds':project['media']['duration'],'original_data_modified':False,'clean_reprocessed':False,
        'reading_region_added':'Mid-frame caption at 85s; this does not alter the old clean mask.'}
with sqlite3.connect(f'file:{(root/"data/studio.sqlite3").as_posix()}?mode=ro',uri=True) as db:
    row=db.execute("SELECT value FROM objects WHERE kind='credential_slots' AND id=?",(project['params']['provider'],)).fetchone()
    selected=(json.loads(row[0]) if row else {}).get('active','default')
provider=project['params']['provider']
secret=keyring.get_password('VietDubStudio',provider if selected=='default' else provider+':'+selected) if selected!='none' else None
check=providers.check_connection(provider,secret,project['params']['translation_model'],'catalog')
report['provider_check']={k:v for k,v in check.items() if k!='models'}
report['selected_model_available']=project['params']['translation_model'] in check.get('models',[])
print('PROVIDER',json.dumps(report,ensure_ascii=False),flush=True)
configure_compute(project['params'])
folder=out/'ocr';folder.mkdir(exist_ok=True)
if (folder/'result.json').is_file():
    extracted=json.loads((folder/'result.json').read_text(encoding='utf-8'))
else:
    began=time.perf_counter()
    extracted=ocr(project,{'kind':'ocr'},folder)
    extracted['elapsed_seconds']=time.perf_counter()-began
    verify_outputs(extracted,project,{'kind':'ocr'},folder)
    (folder/'result.json').write_text(json.dumps(extracted,ensure_ascii=False,indent=2),encoding='utf-8')
project['segments']=extracted['segments'];project['coverage']=extracted['coverage']
if '--wide' in sys.argv:
    previous=json.loads((root/'tests/acceptance-0.7-optimized/isolated-data/jobs/acceptance-final/result.json').read_text(encoding='utf-8'))
    translations={r['text']:r['translated'] for r in previous['segments']}
    for row in project['segments']:row['translated']=translations.get(row['text'],'')
    report['reused_translations']=sum(bool(r['translated']) for r in project['segments'])
qa=inspect_subtitles(project)
report.update(ocr_cues=len(project['segments']),last_cue_end=max((r['end'] for r in project['segments']),default=0),
              ocr_seconds=extracted['elapsed_seconds'],review_required=qa['requires_review'],issues=qa['issues'])
print('OCR_COMPLETE',report['ocr_cues'],report['last_cue_end'],flush=True)
if not report['selected_model_available']:
    report['blocked']='Nhà cung cấp chưa xác nhận mô hình đã chọn. Không tự đổi model/tài khoản; chưa thử dịch thật.'
elif qa['requires_review'] or qa['errors']:
    report['blocked']='Cần duyệt phụ đề từ video thật trước khi dịch và xuất; không tự xác nhận khoảng trống.'
elif '--run-live' not in sys.argv:
    report['blocked']='Nhận dạng đã kiểm xong. Chưa chạy API sinh bản dịch trong lượt kiểm tra này.'
else:
    from credentials import secret_name
    # Pass a fixed credential reference for the key already selected in the user's app.
    import hashlib
    clean_folder=DATA/'jobs'/'reused-clean';clean_folder.mkdir(exist_ok=True)
    shutil.copy2(original/'clean.mp4',clean_folder/'clean.mp4')
    old_result=json.loads((original/'result.json').read_text(encoding='utf-8'))
    clean_artifact=old_result.get('artifacts',{}).get('clean')
    if not clean_artifact:
        raise RuntimeError('Không tìm thấy dấu xác nhận bản che cũ; không chạy lại AI tự động.')
    project['artifacts']['clean']=clean_artifact|{'job_id':'reused-clean'}
    project['params'].update(pipeline_extract='existing',pipeline_translate=True,translation_scope='missing',pipeline_tts=True,
        pipeline_clean=True,use_clean=True,original_gain=0,voice_gain=1)
    target=DATA/'jobs'/'acceptance-final';target.mkdir(exist_ok=True)
    spec={'id':'acceptance-final','kind':'pipeline','credential_ref':{'id':selected,'fingerprint':hashlib.sha256(secret.encode()).hexdigest()}}
    from subtitle_quality import clean_ready
    if not clean_ready(project):
        report['blocked']='Bản che cũ không khớp dấu nhận dạng hiện tại; chưa chạy lại xử lý nặng.'
    else:
        try:
            result=pipeline(project,spec,target)
            result['verification']=verify_outputs(result,project,spec,target)
            (target/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            report['pipeline']={k:result.get(k) for k in ('steps','reused','warnings','primary_file','verification')}
            shutil.copy2(target/'final.mp4',out/'Video-thu-nghiem-NPK-0.7.mp4')
        except Exception as exc:
            report['blocked']=str(exc)
(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print('ACCEPTANCE',json.dumps({k:v for k,v in report.items() if k!='issues'},ensure_ascii=False),flush=True)
