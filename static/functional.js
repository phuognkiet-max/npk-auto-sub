'use strict';
// Service settings and workflow controls; all work is performed by the local job queue.
let installedVoices=[],serviceCatalog={},keyRows=[],keyLoadRevision=0,sampleJob=null;
const providerCatalogs={};
const stylePresets={
 'Dịch sát nghĩa':'Dịch tự nhiên, chính xác; giữ ý nghĩa và tên riêng, không thêm tình tiết.',
 'Review phim hiện đại':'Lời dẫn tiếng Việt rõ ràng, gọn, giàu nhịp điệu; xưng hô nhất quán; không bịa nội dung.',
 'Hội thoại đời thường':'Hội thoại tiếng Việt tự nhiên, ngắn gọn, đúng quan hệ nhân vật và sắc thái câu nói.',
 'Tài liệu / khoa học':'Văn phong thuyết minh chuẩn xác, thuật ngữ nhất quán; không thêm khẳng định ngoài nguyên bản.'
};
taskNames.voice_preview='Nghe thử giọng';taskNames.pipeline='Quy trình A–Z';
schemas.voice.unshift(['voice_id','Giọng đọc đã cài','select',[]]);
schemas.voice.push(['tts_require_translation','Chỉ đọc bản dịch đã hoàn thiện','check']);
schemas.extract.push(['ocr_confidence','Ngưỡng tin cậy OCR','number',.1,1,.05],['ocr_similarity','Ngưỡng gộp câu giống nhau','number',.1,1,.05]);
schemas.translate.push(['translation_batch','Số câu mỗi yêu cầu API','number',1,60,1],['words_per_second','Ngân sách từ / giây','number',1,8,.1]);
schemas.clean[0][3].unshift(['blur','Blur · che mờ nhẹ'],['box','Hộp màu · che kín'],['stretch','Kéo nền cạnh vùng · nhẹ']);
schemas.clean.push(['cover_color','Màu hộp che','color'],['blur_radius','Bán kính mờ (px)','number',2,64,1],['stretch_height','Dải nền lấy mẫu (px)','number',2,100,1],['stretch_direction','Lấy nền từ','select',[['above','Phía trên'],['below','Phía dưới']]]);
schemas.audio.push(['video_encoder','Bộ mã hóa H.264','select',[['auto','Auto · thử NVIDIA, dự phòng CPU'],['cpu','CPU · x264'],['nvenc','NVIDIA NVENC · báo lỗi nếu không có']]],['video_quality','CRF / CQ (nhỏ = nét, tệp lớn)','number',14,30,1]);
schemas.pipeline=[['pipeline_extract','1. Nguồn phụ đề','select',[['existing','Dùng bảng phụ đề hiện có'],['ocr','OCR · đọc chữ trên hình'],['asr','ASR · nhận giọng nói']]],['pipeline_translate','2. Dịch với API đã chọn','check'],['pipeline_tts','3. Tạo lồng tiếng','check'],['pipeline_clean','4. Che / xóa sub gốc','check']];

async function loadCatalog(){serviceCatalog=await api('/catalog');installedVoices=serviceCatalog.voices.filter(v=>v.installed);return serviceCatalog;}
function modelOptions(provider){return providerCatalogs[provider]||serviceCatalog.models?.[provider]||[];}
function updateModelOptions(){const provider=$('#keyProvider').value;$('#providerModels').replaceChildren(...modelOptions(provider).map(m=>new Option(m,m)));if(!$('#checkModel').value||!$('#checkModel').value.startsWith(provider+'-'))$('#checkModel').value=(project?.params.provider===provider?project.params.translation_model:provider==='gemini'?'gemini-3.7-flash':'deepseek-flash');}
for(const option of $('#settingsGroup').options){const tab=button(option.text,()=>{$('#settingsGroup').value=option.value;renderSettings();});tab.dataset.group=option.value;$('#settingsTabs').append(tab);}
const settingsHeading=elem('div',undefined,'settings-heading'),settingsBody=elem('div',undefined,'settings-body');
settingsHeading.append($('#settingsDialog > .panel-head'),$('#settingsTabs'));
settingsBody.append(...$('#settingsDialog').childNodes);$('#settingsDialog').append(settingsHeading,settingsBody);
wire('apiSettings',()=>{if(project)$('#keyProvider').value=project.params.provider;openSettings('keys');});
wire('pipelineSettings',()=>{requireProject();openSettings('pipeline');});

async function loadKeyList(){
 const provider=$('#keyProvider').value,revision=++keyLoadRevision;
 keyRows=[];
 const rows=await api(`/keys/${provider}`);if(revision!==keyLoadRevision||provider!==$('#keyProvider').value)return;
 keyRows=rows;const host=$('#keyList');host.replaceChildren();
 for(const row of rows){const card=elem('div',undefined,'key-card');card.append(elem('strong',`${row.active?'● Đang dùng':'○ Đã lưu'} · ${row.masked}`));
  card.append(elem('small',row.check?`${new Date(row.check.checked_at*1000).toLocaleString('vi-VN')} · ${row.check.message}`:'Chưa gọi kiểm tra với nhà cung cấp.'));
  const controls=elem('div',undefined,'row');if(!row.active)controls.append(button('Dùng key này',async()=>{await api(`/keys/${provider}/select`,'POST',{id:row.id});await refresh();renderKeyStatus();await loadKeyList();}));
  controls.append(button('Xóa',async()=>{if(!confirm(`Xóa key ${row.masked}? Không xóa key khác.`))return;await api(`/keys/${provider}/delete`,'POST',{id:row.id});await refresh();renderKeyStatus();await loadKeyList();}));card.append(controls);host.append(card);
 }
 if(!rows.length)host.append(elem('p','Chưa có key đã lưu.','muted'));updateModelOptions();
}
$('#keyProvider').onchange=()=>{$('#apiKey').value='';$('#checkModel').value='';$('#checkResult').textContent='Chưa thực hiện kiểm tra trong phiên này.';renderKeyStatus();loadKeyList().catch(fail);};
wire('saveKey',async()=>{const provider=$('#keyProvider').value,keys=$('#apiKey').value.trim();if(!keys)throw Error('Nhập key trước khi lưu.');await api(`/keys/${provider}/add`,'POST',{keys});$('#apiKey').value='';await refresh();renderKeyStatus();await loadKeyList();notify('Đã lưu và đọc lại key. Kiểm tra API bằng nút bên dưới.');});
wire('removeKey',async()=>{const active=keyRows.find(k=>k.active);if(!active)throw Error('Chưa có key đang dùng.');if(!confirm(`Xóa key ${active.masked}?`))return;await api(`/keys/${$('#keyProvider').value}/delete`,'POST',{id:active.id});await refresh();renderKeyStatus();await loadKeyList();});
async function checkKey(mode){
 const provider=$('#keyProvider').value,active=keyRows.find(k=>k.active),model=$('#checkModel').value.trim();
 if(!active)throw Error('Lưu và chọn một key trước khi kiểm tra.');
 if(mode==='generation'&&!confirm(`Gửi một yêu cầu thử rất ngắn đến ${provider} / ${model}? Có thể tính phí API.`))return;
 const controls=['checkCatalog','checkGeneration'];controls.forEach(id=>$('#'+id).disabled=true);$('#checkResult').textContent='Đang liên hệ nhà cung cấp…';
 try{const result=await api(`/keys/${provider}/check`,'POST',{id:active.id,mode,model,confirm_cost:mode==='generation'});
  if(result.models)providerCatalogs[provider]=result.models;
  if(provider===$('#keyProvider').value){$('#checkResult').textContent=`${result.ok?'✓':'!'} ${active.masked} · ${provider}\n${result.message}\n${new Date(result.checked_at*1000).toLocaleString('vi-VN')}${result.models?' · '+result.models.length+' model trả về':''}`;updateModelOptions();}
  await refresh();renderKeyStatus();await loadKeyList();
 }catch(e){$('#checkResult').textContent=e.message;throw e;}finally{controls.forEach(id=>$('#'+id).disabled=false);}
}
wire('checkCatalog',()=>checkKey('catalog'));wire('checkGeneration',()=>checkKey('generation'));
wire('useCheckedModel',async()=>{const changes={provider:$('#keyProvider').value,translation_model:$('#checkModel').value.trim()};if(project){checkpoint();Object.assign(project.params,changes);markDirty();await save();renderAll();}else app.settings=await api('/settings','PUT',changes);notify('Đã chọn model cho '+(project?'dự án hiện tại':'dự án mới')+'. Không tự chuyển model khi dịch.');});

function settingsExtras(group){
 const host=$('#settingsExtra');
 if(group==='voice'){
  const info=elem('details');info.append(elem('summary','Nguồn và quyền sử dụng giọng'),elem('p','Tên theo catalog nguồn. Hai mục Ngọc Huyền dùng cùng trọng số. Quyền phân phối thương mại các giọng chưa được xác minh.','muted'));host.append(info);
  placeSampleDock();updateSamplePlayer();
 }
 if(group==='engines'){
  host.append(elem('p','Trạng thái kiểm tra tệp/runtime trên máy, không phải cam kết mọi model đã chạy thành công.','notice'));
  for(const item of serviceCatalog.engines?.items||[]){const row=elem('div',undefined,'engine-card');row.append(elem('strong',`${item.available?'Có':'Chưa có'} · ${item.name}`),elem('p',item.note,'muted'));host.append(row);}
 }
 if(group==='general')host.append(elem('p','Hàng đợi xử lý tuần tự một tác vụ để hạn chế tranh tài nguyên. Chế độ Êm giới hạn luồng CPU; đây không phải bộ điều khiển nhiệt độ và không bảo đảm nhiệt máy cụ thể.','notice'));
 if(group==='pipeline'){
  host.append(elem('p','5. Xuất H.264 + SRT/ASS và kiểm tra tệp, thời lượng, âm thanh. Thiết lập chi tiết ở các nhóm Dịch thuật, Giọng đọc, Che sub và Âm thanh / xuất.','notice'));
  const status=elem('pre','Chưa kiểm tra đầu vào.');status.id='preflightStatus';host.append(status,button('Kiểm tra đầu vào',()=>runPreflight(false)),button('Bắt đầu quy trình → xuất video',()=>runPreflight(true),'primary'));
 }
}
async function runPreflight(start){
 requireProject();if($('#settingsScope').value!=='project')throw Error('Chọn phạm vi Dự án hiện tại để kiểm tra/chạy quy trình.');
 if([...settingsSessions.values()].some(e=>e.scope==='defaults'&&Object.keys(settingsChanges(e)).length))throw Error('Còn bản nháp Mặc định dự án mới. Áp dụng hoặc bỏ bản nháp đó trước khi chạy.');
 await commitSettingsDrafts();await save();
 const result=await api('/preflight','POST',{project_id:project.id,kind:'pipeline'});
 $('#preflightStatus').textContent=[result.ok?'✓ Đầu vào đạt kiểm tra cơ bản.':'Chưa thể chạy:',...result.errors,...result.warnings.map(x=>'Lưu ý: '+x)].join('\n');
 if(!start||!result.ok)return;
 if(!confirm('Chạy tuần tự trên bản chụp dự án hiện tại?\n'+result.warnings.join('\n')+'\nBảng phụ đề có thể được cập nhật khi xong. Video nguồn giữ nguyên.'))return;
 const job=await api('/jobs','POST',{project_id:project.id,kind:'pipeline',replace_segments:true});activeJob=job.id;$('#settingsDialog').close();$('#jobDetails').hidden=false;$('#jobToggle').setAttribute('aria-expanded','true');await refresh();notify('Đã xếp hàng quy trình A–Z. Bấm thanh tiến trình để xem từng bước hoặc hủy.');
}
