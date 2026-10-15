"""Exercise actual preference JavaScript with delayed GET and stale port storage."""
from pathlib import Path
import shutil
import subprocess
import unittest

ROOT=Path(__file__).resolve().parents[1]
NODE=shutil.which('node') or str(Path('C:/Users/developer/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node.exe'))

SCRIPT=r'''
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const source=fs.readFileSync(process.argv[1],'utf8');
const defaults={tts_text:'',tts_voice:'',tts_speed:1,tts_job:'',favorite_voices:[],viewer_ratio:54,current_project:'',sample_text:'',tts_params:{tts_device:'auto',workload_mode:'quiet',pronunciation_dictionary:''},narration_segments:[]};
function fixture(saved,initial={}) {
  const storage=new Map(Object.entries(initial)), elements=new Map();
  const context={window:{addEventListener(){}},document:{documentElement:{style:{setProperty(){}}}},
    localStorage:{getItem:key=>storage.get(key)??null,setItem:(key,value)=>storage.set(key,String(value))},
    api:()=>Promise.resolve(saved),setTimeout:()=>1,clearTimeout(){},standaloneSpeech:{},area:'projects',
    panelHasFocusedEditor:()=>false,renderPanel(){throw Error('Unexpected render')},
    $:selector=>{if(!elements.has(selector))elements.set(selector,{});return elements.get(selector)}};
  vm.createContext(context);vm.runInContext(source,context);return {context,storage};
}
(async()=>{
  const first=fixture({...defaults,_persisted:false},{'npk-tts-text':'Legacy draft'});
  await first.context.window.NPKUpgrade.hydrate();
  assert.equal(first.context.standaloneSpeech.text,'Legacy draft');
  const cleared=fixture({...defaults,_persisted:true},{'npk-tts-text':'Old deleted draft','npk-tts-job':'a'.repeat(32),'npk-favorite-voices':'["old"]'});
  await cleared.context.window.NPKUpgrade.hydrate();
  assert.equal(cleared.context.standaloneSpeech.text,'');assert.equal(cleared.context.standaloneSpeech.jobId,'');
  assert.equal(cleared.storage.get('npk-favorite-voices'),'[]');
  let finish;const delay=new Promise(resolve=>finish=resolve),race=fixture(delay);
  const hydration=race.context.window.NPKUpgrade.hydrate();
  race.storage.set('npk-tts-text','New edit while GET pending');
  race.context.window.NPKUpgrade.store('npk-tts-text','New edit while GET pending');
  finish({...defaults,tts_text:'Older server draft',_persisted:true,narration_segments:[{text:'Old segmented narration',voice_id:'piper:vais1000',rate:1,pause_after_ms:0}]});await hydration;
  assert.equal(race.context.standaloneSpeech.text,'New edit while GET pending');
  assert.equal(Object.keys(race.context.window.NPKUpgrade.speechPayload()).length,0);
  console.log('PASS first migration, persisted empty fields, delayed hydration preserves new edits and rejects old narration');
})().catch(error=>{console.error(error);process.exitCode=1});
'''


class PreferenceHydrationTests(unittest.TestCase):
    @unittest.skipUnless(Path(NODE).is_file(),'Node runtime unavailable')
    def test_actual_ui_hydration_migration_empty_and_pending_edit(self):
        completed=subprocess.run([NODE,'-e',SCRIPT,str(ROOT/'static/workspace-upgrade.js')],
            cwd=ROOT,capture_output=True,text=True,timeout=15)
        self.assertEqual(completed.returncode,0,completed.stderr or completed.stdout)


if __name__=='__main__':unittest.main()
