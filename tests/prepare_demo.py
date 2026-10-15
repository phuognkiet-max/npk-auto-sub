"""Keep one coherent synthetic demo in the UI; archive test-only database records."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
import time
from core import ROOT, all_items, connect, get, put

fixture=(ROOT/'tests/fixtures/demo.mp4').resolve()
results=json.loads((ROOT/'tests/integration-results.json').read_text(encoding='utf-8'))
rendered=next(r for r in results if r['kind']=='render')
job=get('job',rendered['job_id'])
snapshot=json.loads((ROOT/'data/jobs'/job['id']/'input.json').read_text(encoding='utf-8'))['project']
assert Path(snapshot['media']['path']).resolve()==fixture
projects=[p for p in all_items('project') if Path(p['media']['path']).resolve()==fixture and p['name']=='Demo · Kiểm thử độc lập']
identifiers={p['id'] for p in projects}
jobs=[j for j in all_items('job') if j['project_id'] in identifiers]
archive=ROOT/'tests/archived-test-state.json'
archive.write_text(json.dumps({'projects':projects,'jobs':jobs},ensure_ascii=False,indent=2),encoding='utf-8')
with connect() as db:
    for p in projects:
        db.execute('DELETE FROM objects WHERE kind=? AND id=?',('project',p['id']))
    for j in jobs:
        db.execute('DELETE FROM objects WHERE kind=? AND id=?',('job',j['id']))
snapshot['name']='Demo · Xóa chữ và lồng tiếng'
snapshot['updated']=time.time()
for record in results:
    if not record.get('job_id'):
        continue
    item=next(j for j in jobs if j['id']==record['job_id'])
    item['project_name']=snapshot['name']
    put('job',item['id'],item)
    snapshot['artifacts'][item['kind']]=item['result']|{'job_id':item['id']}
put('project',snapshot['id'],snapshot)
print('Prepared one demo; older synthetic test records archived. No media files removed.')
