// Execute the production helpers in isolated DOM/API stubs. No browser, native
// app, provider, inference, credentials or production state is contacted.
const fs = require('node:fs'), path = require('node:path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const setup = fs.readFileSync(path.join(root, 'static/single-video-setup.js'), 'utf8');
const start = setup.indexOf('    async function showFinalResult(current){');
const end = setup.indexOf('    function visibility(){', start);
assert.ok(start >= 0 && end > start);
const helper = setup.slice(start, end);
const project = {id:'private-project', updated:200, source_revision:'same-source'};
const elements = new Map(), find = id => {
  if(!elements.has(id))elements.set(id,{hidden:true,innerHTML:'',onclick:null});return elements.get(id);
};
let response, calls=[];
const api=async url=>{calls.push(url);return response;};
const ctx={project:()=>project};
const fn=new Function('ctx','find','api','mounted','guard','esc','serial',helper+'\nreturn showFinalResult;')(
  ctx,find,api,()=>true,fn=>fn,str=>String(str),1);
async function run(){
  response={expected_revision:200,current:null,historical:{job_id:'old',path:'D:\\old.mp4',
    current:false,reason:'Nội dung đã đổi; đây là bản trước.'}};
  await fn(1);
  assert.equal(find('setupFinalResult').hidden,false);
  assert.ok(find('setupFinalResult').innerHTML.includes('Video hoàn tất của lượt trước'));
  assert.ok(!find('setupFinalResult').innerHTML.includes('Đã xuất video thành phẩm'));
  await find('setupOpenFinal').onclick();
  assert.equal(calls.at(-1),'/api/studio/jobs/old/open-result');
  response={expected_revision:200,current:{job_id:'current',path:'D:\\new.mp4',current:true},historical:null};
  await fn(1);
  assert.ok(find('setupFinalResult').innerHTML.includes('Đã xuất video thành phẩm'));
  response={expected_revision:200,current:null,historical:null};await fn(1);
  assert.equal(find('setupFinalResult').hidden,true);
  const studio=fs.readFileSync(path.join(root,'static/studio.js'),'utf8');
  const a=studio.indexOf('function latestFinalArtifact() {'), b=studio.indexOf('function requestFinalArtifacts(',a);
  const source=studio.slice(a,b);
  const resolutions=new Map([['private-project',{expected_revision:200,source_revision:'same-source',
    current:{job_id:'current',current:true,artifact:{}},historical:null}]]);
  const resolve=new Function('P','finalResolutions','dirty',source+'\nreturn latestFinalArtifact();');
  assert.equal(resolve(project,resolutions,false)._final_current,true);
  assert.equal(resolve({...project,updated:201},resolutions,false)._final_current,false);
  assert.equal(resolve(project,resolutions,true)._final_current,false);
  assert.equal(resolve({...project,source_revision:'different'},resolutions,false),null);
  const mergeSource=studio.match(/jobs = (\[\.\.\.new Map\(\[\.\.\.\(data\.active_jobs[\s\S]+?\.sort\(\(a,b\)=>\(b\.created\|\|0\)-\(a\.created\|\|0\)\));/);
  assert.ok(mergeSource,'production polling merge is present');
  const merge=new Function('data','return '+mergeSource[1]+';');
  const data={active_jobs:[{id:'active',state:'running',created:1}],jobs:[{id:'preview',state:'done',created:2}],
    job_updates:[{id:'active',state:'done',created:1}]};
  const jobs=merge(data);assert.equal(jobs.length,2);assert.equal(jobs.find(row=>row.id==='active').state,'done');
  console.log(JSON.stringify({production_helpers_executed:true,assertions:14,provider_calls:0,native_interactions:0}));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
