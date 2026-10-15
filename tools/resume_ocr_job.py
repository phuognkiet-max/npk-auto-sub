"""Inspect or resume an existing failed OCR job without starting another app/queue.

Default is read-only inspection. --enqueue uses the normal server.enqueue path;
the already-open NPK queue owns execution and revision-checked publication.
"""
import argparse
import copy
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--enqueue', action='store_true')
    args = parser.parse_args()
    if len(args.job_id) != 32 or any(c not in '0123456789abcdef' for c in args.job_id):
        raise ValueError('Mã tác vụ không hợp lệ.')
    from core import DATA, all_items, get, save_srt
    from ocr_checkpoint import validate_resume
    previous = get('job', args.job_id)
    if not previous or previous.get('kind') != 'ocr' or previous.get('state') not in ('error', 'cancelled'):
        raise ValueError('Chỉ tiếp tục tác vụ OCR đã lỗi hoặc đã hủy.')
    project = get('project', previous['project_id'])
    if not project or previous.get('source_revision') != project.get('source_revision'):
        raise ValueError('Dự án hoặc video nguồn đã thay đổi; không tự áp dụng checkpoint cũ.')
    checkpoint = DATA/'jobs'/args.job_id/'recognition-checkpoint.json'
    saved = validate_resume(checkpoint, project)
    rows = copy.deepcopy(saved['rows'])
    if saved.get('current'):
        rows.append(copy.deepcopy(saved['current']))
    report = {'previous_job':args.job_id, 'project_id':project['id'],
              'checkpoint_cues':len(rows), 'next_time':saved['next_time'],
              'project_cues_before':len(project.get('segments',[])),
              'previous_elapsed_seconds':round(previous.get('ended',0)-previous.get('started',0),3),
              'queue_paused':bool((get('queue_control','main') or {}).get('paused')),
              'enqueued':False}
    if not args.enqueue:
        print(json.dumps(report, ensure_ascii=False))
        return
    existing = [j for j in all_items('job') if j.get('resume_from') == args.job_id and
                j.get('state') in ('queued','running','done','review')]
    if existing:
        latest = max(existing,key=lambda j:j['created'])
        report.update(job_id=latest['id'], state=latest['state'], existing=True)
        print(json.dumps(report, ensure_ascii=False))
        return
    backup = DATA/'recoveries'/('ocr-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True, exist_ok=False)
    (backup/'project-before.json').write_text(json.dumps(project,ensure_ascii=False,indent=2),encoding='utf-8')
    shutil.copy2(checkpoint,backup/checkpoint.name)
    shutil.copy2(DATA/'jobs'/args.job_id/'input.json',backup/'input-before.json')
    save_srt(rows,backup/'checkpoint-partial-source.srt',translated=False)
    from server import enqueue
    # Do not call start_workers() or shutdown(): the open app owns that queue.
    job = enqueue({'kind':'ocr','project_id':project['id'],'expected_revision':project['updated'],
                   'replace_segments':True,'resume_from':args.job_id,'request_id':uuid.uuid4().hex})
    report.update(enqueued=True,job_id=job['id'],state=job['state'],backup=str(backup))
    (backup/'recovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
