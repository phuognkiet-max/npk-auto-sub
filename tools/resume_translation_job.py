"""Inspect/recover partial translation; the open app owns any queued execution.

Default is read-only. --restore uses the revision-checked normal apply endpoint.
--enqueue restores first, then queues only matching still-missing original cues
with the current private snapshot. Completed/manual translations are not sent
again. No provider/key/model switching and no second backend/worker loop.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('job_id')
    parser.add_argument('--restore',action='store_true')
    parser.add_argument('--enqueue',action='store_true')
    args=parser.parse_args()
    if len(args.job_id)!=32 or any(c not in '0123456789abcdef' for c in args.job_id):
        raise ValueError('Mã tác vụ không hợp lệ.')
    from core import DATA,get,all_items,save_srt
    from studio_api import translation_restore_candidates
    old=get('job',args.job_id)
    if not old or old.get('kind')!='translate' or old.get('state') not in ('error','cancelled'):
        raise ValueError('Chỉ khôi phục lượt dịch dở đã lỗi hoặc hủy.')
    project=get('project',old['project_id'])
    if not project or old.get('source_revision')!=project.get('source_revision'):
        raise ValueError('Video nguồn đã đổi; không khôi phục tự động từ tác vụ cũ.')
    candidates=translation_restore_candidates(old,project)
    original=json.loads((DATA/'jobs'/old['id']/'input.json').read_text(encoding='utf-8'))['project']
    selected=old.get('segment_indices')
    permitted={(r['start'],r['end'],r['text']) for i,r in enumerate(original['segments'])
               if (selected is None or i in selected) and (original['params'].get('translation_scope')!='missing' or
                                                          not r.get('translated','').strip())}
    report={'previous_job':old['id'],'project_id':project['id'],
            'project_cues':len(project['segments']),'restorable_cues':len(candidates),
            'translated_before':sum(bool(r.get('translated','').strip()) for r in project['segments']),
            'restored':False,'enqueued':False}
    if not (args.restore or args.enqueue):
        print(json.dumps(report,ensure_ascii=False));return
    backup=DATA/'recoveries'/('translate-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    backup.mkdir(parents=True,exist_ok=False)
    (backup/'project-before.json').write_text(json.dumps(project,ensure_ascii=False,indent=2),encoding='utf-8')
    for name in ('translation-checkpoint.json','input.json'):
        shutil.copy2(DATA/'jobs'/old['id']/name,backup/name)
    import server
    if candidates:
        endpoint=next(route.endpoint for route in server.app.routes
                      if getattr(route,'path','')=='/api/studio/jobs/{identifier}/apply' and
                         'POST' in getattr(route,'methods',set()))
        project=endpoint(old['id'],{'expected_revision':project['updated']})
        report['restored']=True
    report.update(translated_after=sum(bool(r.get('translated','').strip()) for r in project['segments']),backup=str(backup))
    save_srt(project['segments'],backup/'restored-subtitles.srt')
    if args.enqueue:
        for key in ('provider','translation_model','target_language'):
            if project['params'].get(key)!=original['params'].get(key):
                raise ValueError('Nhà cung cấp/model/ngôn ngữ đã đổi; phần dịch dở được giữ, chưa gọi dịch tiếp.')
        pending=[i for i,r in enumerate(project['segments']) if not r.get('translated','').strip() and
                 (r['start'],r['end'],r['text']) in permitted]
        report['remaining_cues']=len(pending)
        busy=[j for j in all_items('job') if j.get('project_id')==project['id'] and j.get('state') in ('queued','running')]
        if busy:
            raise ValueError('Dự án đang có tác vụ khác; đã giữ phần dịch khôi phục và không tạo lượt trùng.')
        if pending:
            job=server.enqueue({'kind':'translate','project_id':project['id'],
                                'expected_revision':project['updated'],'segment_indices':pending,
                                'replace_segments':True,'request_id':uuid.uuid4().hex})
            report.update(enqueued=True,job_id=job['id'],state=job['state'])
    (backup/'recovery.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
