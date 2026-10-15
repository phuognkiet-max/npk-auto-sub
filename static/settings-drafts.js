'use strict';
// Keep one draft per scope, shared across tabs. No implicit saves on navigation.
const settingsSessions=new Map();let settingsContext=null,settingsSaving=false;
$('#settingsDialog').setAttribute('aria-labelledby','settingsHeading');$('#settingsDialog h2').id='settingsHeading';
$('#importDialog').setAttribute('aria-labelledby','importHeading');$('#importDialog h2').id='importHeading';
const settingsChangeStatus=elem('p','', 'muted');settingsChangeStatus.id='settingsChangeStatus';settingsChangeStatus.setAttribute('role','status');
const settingsActions=elem('div',undefined,'settings-actions');settingsActions.id='settingsActions';
const settingsApply=$('#settingsForm button[type="submit"]');settingsApply.id='applySettings';settingsApply.setAttribute('form','settingsForm');
settingsActions.append(settingsChangeStatus,settingsApply);$('#settingsDialog').append(settingsActions);
// One persistent action area; the field list can scroll independently.
$('#settingsForm > .notice').remove();
function settingsEntry(){
 const scope=$('#settingsScope').value,key=scope==='defaults'?'defaults':'project:'+settingsContext;
 if(!settingsSessions.has(key)){
  const values=clone(scope==='defaults'?app.settings:project?.params||app.settings);
  settingsSessions.set(key,{values,baseline:clone(values),scope});
 }
 return settingsSessions.get(key);
}
function settingsChanges(entry){return Object.fromEntries(Object.entries(entry.values).filter(([k,v])=>JSON.stringify(v)!==JSON.stringify(entry.baseline[k])));}
function hasSettingsChanges(){return [...settingsSessions.values()].some(e=>Object.keys(settingsChanges(e)).length);}
function settingsChangeHint(){
 const count=Object.keys(settingsChanges(settingsEntry())).length;
 settingsChangeStatus.textContent=count?`${count} thông số chưa áp dụng trong phạm vi này. Chuyển tab vẫn giữ bản nháp.`:'Không có thay đổi chưa áp dụng trong phạm vi này.';
}
function openSettings(group='keys'){
 if(!$('#settingsDialog').open){settingsSessions.clear();settingsContext=project?.id||null;$('#settingsScope').value=project?'project':'defaults';}
 $('#settingsScope option[value="project"]').disabled=!project;
 $('#settingsGroup').value=group;renderSettings();if(!$('#settingsDialog').open)$('#settingsDialog').showModal();placeSampleDock();
}
function renderSettings(){
 settingsGroup=$('#settingsGroup').value;$('.settings-body').scrollTop=0;
 const keys=settingsGroup==='keys';$('#settingsParams').hidden=keys||settingsGroup==='engines';$('#keyPanel').hidden=!keys;
 settingsActions.hidden=keys||settingsGroup==='engines';
 $('#settingsExtra').replaceChildren();$$('#settingsTabs button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.group===settingsGroup)));
 $('#settingsNotice').textContent='Đổi tab không làm mất bản nháp. Áp dụng sẽ lưu các thay đổi trong cùng phạm vi; không đổi cấu hình tác vụ đang chạy.';
 if(keys){renderKeyStatus();loadKeyList().catch(fail);return;}
 settingsDraft=settingsEntry().values;
 paramFields($('#settingsFields'),settingsGroup,settingsDraft,(key,value)=>{settingsDraft[key]=value;settingsChangeHint();});
 settingsExtras(settingsGroup);settingsChangeHint();placeSampleDock();
}
async function commitSettingsDrafts(){
 if(settingsSaving)throw Error('Đang lưu cài đặt.');
 const entry=settingsEntry(),changed=settingsChanges(entry);if(!Object.keys(changed).length)return;
 if(entry.scope==='project'&&project?.id!==settingsContext)throw Error('Dự án đã đổi. Đóng và mở lại cài đặt.');
 settingsSaving=true;
 try{
  if(entry.scope==='defaults')app.settings=await api('/settings','PUT',changed);
  else{requireProject();checkpoint();project.params={...project.params,...changed};markDirty();await save();renderAll();}
  entry.baseline=clone(entry.values);settingsChangeHint();
 }finally{settingsSaving=false;}
}
function mayCloseSettings(){
 if(settingsSaving){notify('Chờ lưu cài đặt hoàn tất.');return false;}
 return !hasSettingsChanges()||confirm('Còn thông số chưa áp dụng. Đóng và bỏ các thay đổi cài đặt này?');
}
$('#settingsDialog').addEventListener('cancel',e=>{if(!mayCloseSettings())e.preventDefault();});
$('#settingsDialog').addEventListener('close',()=>settingsSessions.clear());
$('#settingsDialog [data-close]').onclick=()=>{if(mayCloseSettings())$('#settingsDialog').close();};
$('#settingsForm').onsubmit=async e=>{e.preventDefault();try{await commitSettingsDrafts();notify('Đã lưu cài đặt cho phạm vi đã chọn.');}catch(error){fail(error);}};
$('#settingsGroup').onchange=renderSettings;$('#settingsScope').onchange=renderSettings;
