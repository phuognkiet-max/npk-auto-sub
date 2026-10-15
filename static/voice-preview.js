'use strict';
// One preview player shared by every voice row; listening never selects a voice.
const samplePlayer=elem('audio');samplePlayer.id='samplePlayer';samplePlayer.preload='metadata';
const sampleDock=elem('div',undefined,'sample-dock');sampleDock.id='sampleDock';sampleDock.hidden=true;
const sampleStatus=elem('span');sampleStatus.id='sampleStatus';sampleStatus.setAttribute('role','status');
const sampleStop=button('Dừng',stopVoiceSample);sampleStop.id='stopVoiceSample';
sampleDock.append(sampleStatus,sampleStop,samplePlayer);$('footer').before(sampleDock);
let sampleRequest=null,samplePending=false,sampleAutoplay=false,sampleLoaded=null;
const sampleDefault='Xin chào, đây là giọng đọc thử của NPK Auto Sub.';
const speakerSvg='<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9h4l5-4v14l-5-4H3z"/><path d="M16 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/></svg>';

function placeSampleDock(){
 const parent=$('#settingsDialog').open?$('.settings-body'):null;
 if(parent){if(sampleDock.parentNode!==parent)parent.prepend(sampleDock);}else if(sampleDock.nextElementSibling!==$('footer'))$('footer').before(sampleDock);
}
$('#settingsDialog').addEventListener('close',()=>{placeSampleDock();stopVoiceSample().catch(fail);});
video.addEventListener('play',()=>{samplePlayer.pause();sampleAutoplay=false;paintSampleButtons();});
function buildVoicePicker(host,values,onChoose){
 const picker=elem('section',undefined,'voice-picker');picker.setAttribute('aria-label','Chọn và nghe thử giọng');
 const header=elem('div',undefined,'voice-picker-head'),selectedName=elem('strong'),count=elem('small',`${installedVoices.length} giọng offline`);
 header.append(selectedName,count);picker.append(header);
 const search=elem('input');search.type='search';search.placeholder='Tìm giọng…';search.setAttribute('aria-label','Tìm giọng');picker.append(search);
 const sampleOptions=elem('details'),text=elem('textarea');text.value=sampleDefault;text.maxLength=400;text.setAttribute('aria-label','Nội dung nghe thử');
 sampleOptions.append(elem('summary','Tùy chỉnh câu nghe thử'),text);picker.append(sampleOptions);
 const library=elem('div',undefined,'voice-library');picker.append(library);
 const empty=elem('p','Không có giọng phù hợp.','muted');empty.hidden=true;picker.append(empty);
 const rows=[];
 for(const voice of installedVoices){
  const row=elem('div',undefined,'voice-row');row.dataset.voice=voice.id;
  const choose=button('',()=>{onChoose(voice.id);paintSelection();},'voice-select');choose.dataset.chooseVoice=voice.id;
  choose.append(elem('span',voice.name),elem('small',voice.language+' · offline'));
  const listen=button('',()=>startVoiceSample({...values,voice_id:voice.id},text.value),'voice-listen');
  listen.dataset.listenVoice=voice.id;listen.dataset.voiceName=voice.name;listen.innerHTML=speakerSvg;
  row.append(choose,listen);library.append(row);rows.push({row,choose,voice});
 }
 function paintSelection(){
  selectedName.textContent='Đang chọn: '+(installedVoices.find(v=>v.id===values.voice_id)?.name||'Chưa chọn');
  for(const {choose,voice} of rows)choose.setAttribute('aria-pressed',String(voice.id===values.voice_id));
 }
 search.oninput=()=>{const q=search.value.trim().toLocaleLowerCase();let shown=0;for(const {row,voice} of rows){row.hidden=!voice.name.toLocaleLowerCase().includes(q);if(!row.hidden)shown++;}empty.hidden=shown!==0;};
 host.append(picker);paintSelection();paintSampleButtons();
}
function paintSampleButtons(){
 for(const b of $$('[data-listen-voice]')){
  const selected=b.dataset.listenVoice===sampleRequest?.voice_id;
  const playing=selected&&!samplePlayer.paused;
  const waiting=selected&&(samplePending||sampleAutoplay&&sampleLoaded!==sampleJob);
  const label=`${playing?'Dừng nghe':waiting?'Hủy nghe thử':'Nghe thử'} ${b.dataset.voiceName}`;
  b.title=label;b.setAttribute('aria-label',label);b.setAttribute('aria-pressed',String(playing));b.setAttribute('aria-busy',String(waiting));
  b.classList.toggle('is-playing',playing);b.classList.toggle('is-waiting',waiting);
 }
}
async function stopVoiceSample(){
 sampleAutoplay=false;samplePlayer.pause();sampleDock.hidden=true;
 sampleStatus.textContent='Đã dừng nghe thử.';paintSampleButtons();
 const job=app.jobs.find(j=>j.id===sampleJob);
 if(job&&['queued','running'].includes(job.state)){
  await api(`/jobs/${job.id}/cancel`,'POST');await refresh();
 }
}
async function startVoiceSample(params,text=sampleDefault){
 text=text.trim();if(!text||text.length>400)throw Error('Nhập câu nghe thử từ 1–400 ký tự.');
 const request={voice_id:params.voice_id,voice_speed:params.voice_speed,workload_mode:params.workload_mode,text};
 const same=JSON.stringify(request)===JSON.stringify(sampleRequest);
 if(samplePending)return;
 if(same&&(!samplePlayer.paused||sampleAutoplay)){await stopVoiceSample();return;}
 video.pause();voiceAudio.pause();samplePlayer.pause();samplePending=true;sampleRequest=request;sampleAutoplay=true;
 sampleDock.hidden=false;placeSampleDock();sampleStatus.textContent='Đang chuẩn bị '+(installedVoices.find(v=>v.id===params.voice_id)?.name||'mẫu giọng')+'…';paintSampleButtons();
 try{
  const old=app.jobs.find(j=>j.id===sampleJob);
  if(!same&&old&&['queued','running'].includes(old.state))await api(`/jobs/${old.id}/cancel`,'POST');
  const job=await api('/jobs','POST',{kind:'voice_preview',params:{voice_id:params.voice_id,voice_speed:params.voice_speed,workload_mode:params.workload_mode},sample_text:text});
  sampleJob=job.id;sampleLoaded=null;await refresh();updateSamplePlayer();
 }catch(e){sampleAutoplay=false;sampleStatus.textContent=e.message;throw e;}finally{samplePending=false;paintSampleButtons();}
}
function updateSamplePlayer(){
 if(!sampleJob)return;const job=app.jobs.find(j=>j.id===sampleJob);if(!job)return;
 if(job.state==='done'){
  if(sampleLoaded!==job.id){
   samplePlayer.src=mediaUrl(`/media/job/${job.id}/sample.wav`);sampleLoaded=job.id;
   sampleStatus.textContent=(job.result?.voice_name||'Giọng')+' · sẵn sàng';
   if(sampleAutoplay){sampleAutoplay=false;samplePlayer.play().catch(()=>{sampleStatus.textContent='Trình duyệt chặn tự phát. Bấm loa lần nữa để nghe.';paintSampleButtons();});}
  }
 }else if(['error','cancelled'].includes(job.state)){
  sampleAutoplay=false;sampleStatus.textContent=(job.state==='error'?'Không tạo được mẫu: ':'')+job.message;
 }else sampleStatus.textContent=(installedVoices.find(v=>v.id===sampleRequest?.voice_id)?.name||'Giọng')+' · '+(job.state==='queued'?'Đang chờ; không chen ngang tác vụ video':job.message);
 paintSampleButtons();
}
samplePlayer.addEventListener('play',()=>{sampleStatus.textContent=(installedVoices.find(v=>v.id===sampleRequest?.voice_id)?.name||'Giọng')+' · đang phát';paintSampleButtons();});
samplePlayer.addEventListener('pause',paintSampleButtons);
samplePlayer.addEventListener('ended',()=>{sampleStatus.textContent='Nghe xong · bấm loa để nghe lại.';paintSampleButtons();});
samplePlayer.addEventListener('error',()=>{sampleAutoplay=false;sampleStatus.textContent='Không phát được mẫu. Bấm loa để kiểm tra/tạo lại.';paintSampleButtons();});
