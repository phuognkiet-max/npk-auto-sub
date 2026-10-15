'use strict';
if (document.body.classList.contains('workflow')) {
  const steps=['media','sub','translate','clean','voice','audio'];
  const titles={media:'Nguồn video',sub:'Tách phụ đề',translate:'Dịch phụ đề',clean:'Che phụ đề gốc',voice:'Lồng tiếng',audio:'Trộn âm & xuất video'};
  const descriptions={
    media:'Chọn video và dự án. Bản nguồn luôn được giữ nguyên.',
    sub:'Đọc chữ trên hình bằng OCR, nhận lời nói bằng ASR hoặc nhập SRT. Chọn câu phía dưới để sửa mốc và nội dung.',
    translate:'Chọn nhà cung cấp, kiểm tra key và model. Bản dịch có thể sửa từng câu trước khi tạo giọng.',
    clean:'Kéo vùng trên video nguồn. Thử 3 giây bằng đúng bộ xử lý rồi mới chạy toàn video.',
    voice:'Chọn giọng offline và nghe thử, hoặc dùng audio đã tạo trên dịch vụ ngoài.',
    audio:'Chọn bản sạch, chỉnh âm gốc / giọng / nhạc, kiểm tra rồi xuất. Xem đầu ra trong danh sách trên video.'
  };
  let listPage=0,listKey='',listEntries=[],lastActive=-1,lastInspectorTool=null;
  const pageSize=60;
  let audioRevision=0, inspected=null;
  const originalSetTool=setTool, originalInspector=renderInspector, originalResults=updateResults;
  window.arrangeStageFields=function(){
    $('#essentials').replaceChildren();
    const basic={media:['workload_mode'],sub:['source_language','ocr_fps'],translate:['provider','translation_model','target_language'],clean:['method','mask_source','workload_mode'],voice:['voice_speed'],audio:['original_gain','voice_gain','bgm_gain','bgm_path','use_clean','burn_subtitles']}[tool];
    for(const child of [...$('#parameters').children]){
      const key=child.querySelector('[data-param]')?.dataset.param;
      if(basic.includes(key)||child.classList.contains('voice-picker'))$('#essentials').append(child);
    }
    if(tool==='audio'){
      const path=$('#parameters [data-param="voice_path"]');if(path)path.closest('label').hidden=true;
      $('#essentials').append(button(project?.params.voice_path?'Đổi / kiểm tra giọng ngoài ở bước Lồng tiếng':'Chọn giọng ở bước Lồng tiếng',()=>setTool('voice')));
    }
  };
  // No hidden timeline canvas rasterization or 50k-cue iteration on resize.
  drawTimeline=()=>{};
  setTool=function(next){
    const changed=tool!==next;
    const track={media:'video',sub:'source',translate:'viet',clean:'mask',voice:'voice',audio:'bgm'}[next];
    if(selection.track!==track)selection={track,index:Math.min(selection.index,Math.max(0,rows(track).length-1))};
    originalSetTool(next);
    const index=steps.indexOf(next);
    $('#previousStage').disabled=index<=0;
    $('#nextStage').textContent=index===5?'Thiết lập chạy cả quy trình':'Tiếp tục → '+titles[steps[index+1]];
    $('#stageSummary').textContent=`Bước ${index+1} / 6 · chuyển bước không tự chạy tác vụ`;
    $$('[data-tool]').forEach(b=>{if(b.dataset.tool===next)b.setAttribute('aria-current','step');else b.removeAttribute('aria-current');});
    if(changed)$('#stageContent').scrollTop=0; requestAnimationFrame(layoutViewer);
  };
  selectObject=function(track,index,seekTo=true){
    selection={track,index}; if(seekTo&&selected())seek(selected().start);
    const next=track==='mask'?'clean':track==='voice'?'voice':track==='viet'?'translate':track==='source'?'sub':track==='bgm'?'audio':'media';
    setTool(next); drawOverlay();
  };
  function paintRows(){
    const host=$('#objectList');host.replaceChildren();
    const start=listPage*pageSize;
    for(const {r,i,track} of listEntries.slice(start,start+pageSize)){
      const b=button('',()=>selectObject(track,i),'cue-row');b.dataset.row=i;
      b.setAttribute('aria-pressed',String(selection.track===track&&selection.index===i));
      b.append(elem('small',`${i+1} · ${fmt(r.start)}\n${fmt(r.end)}`),elem('span',objectText(track,r,i)));
      host.append(b);
    }
    if(!listEntries.length)host.append(elem('p',tool==='clean'?'Chưa có vùng. Kéo trên video nguồn để vẽ vùng che.':'Chưa có câu phù hợp. Nhập SRT hoặc trích phụ đề trước.','muted'));
    const pages=Math.max(1,Math.ceil(listEntries.length/pageSize));
    $('#listCount').textContent=`${listEntries.length} mục · trang ${listPage+1}/${pages}`;
    $('#listPrevious').disabled=listPage===0;$('#listNext').disabled=listPage>=pages-1;lastActive=-1;highlightCue();
  }
  renderList=function(){
    const host=$('#objectList'),track=listTrack(),isList=['sub','translate','clean','voice'].includes(tool);
    $('#listTitle').textContent=isList?trackNames[track]:'Thông tin dự án';
    $('#listPanel').hidden=!isList;$('#addObject').disabled=!project;$('#searchObjects').hidden=!isList;
    if(!project||!isList){host.replaceChildren();listEntries=[];return;}
    const query=$('#searchObjects').value.toLocaleLowerCase();
    const key=project.id+'|'+track+'|'+query;if(key!==listKey){listKey=key;listPage=0;}
    listEntries=rows(track).map((r,i)=>({r,i,track})).filter(({r,i})=>objectText(track,r,i).toLocaleLowerCase().includes(query));
    listPage=Math.min(listPage,Math.max(0,Math.ceil(listEntries.length/pageSize)-1));paintRows();
  };
  function highlightCue(){
    if(!project||!$('#followVideo').checked||!['sub','translate','voice'].includes(tool))return;
    const index=project.segments.findIndex(r=>r.start<=video.currentTime&&r.end>video.currentTime);
    if(index===lastActive)return;lastActive=index;
    for(const b of $$('#objectList [data-row]'))b.classList.toggle('is-current',Number(b.dataset.row)===index);
    // Follow only within the visible page; never replace fields while a user edits.
    const b=$(`#objectList [data-row="${index}"]`);if(b&&!video.paused){const host=$('#objectList');host.scrollTop=Math.max(0,b.offsetTop-host.offsetTop-host.clientHeight/2);}
  }
  video.addEventListener('timeupdate',highlightCue);
  $('#followVideo').onchange=()=>{lastActive=-1;highlightCue();};
  wire('listPrevious',()=>{listPage=Math.max(0,listPage-1);paintRows();});
  wire('listNext',()=>{listPage++;paintRows();});
  $('#searchObjects').oninput=renderList;
  wire('addObject',()=>{requireProject();if(tool==='clean'){notify('Kéo vùng trên video nguồn để thêm.');return;}checkpoint();const start=Math.min(video.currentTime,duration()-.05);project.segments.push({start,end:Math.min(duration(),start+2),text:'',translated:''});markDirty();const target=tool==='translate'?'viet':tool==='voice'?'voice':'source';selectObject(target,project.segments.length-1);listPage=Math.floor((project.segments.length-1)/pageSize);renderList();});
  renderInspector=function(){
    $('#essentials').replaceChildren();
    $$('#stageOptions .extra-actions').forEach(e=>e.remove());
    originalInspector();$('#inspectorTitle').textContent=titles[tool];$('#stageDescription').textContent=descriptions[tool];
    $('#viewerHint').textContent=tool==='clean'?'Kéo trên video nguồn để vẽ vùng che. Giữ Alt và kéo để di chuyển hình đã phóng to.':'Bấm Phát hoặc Space để xem. Chọn câu để đến đúng thời điểm trong video.';
    $('#objectDetails').hidden=!$('#properties').children.length;
    $('#objectDetailsTitle').textContent=tool==='clean'&&selected()?`Vùng ${selection.index+1} · ${Math.round(selected().w)} × ${Math.round(selected().h)} px · chỉnh vùng & thời gian`:'Chỉnh nội dung & thời gian câu';
    $('#objectDetails').open=tool!=='clean';
    $('#stageOptions').hidden=!project;$('#externalVoice').hidden=tool!=='voice'||!project;
    const editable=!!selected()&&['mask','source','viet'].includes(selection.track);
    for(const id of ['splitObject','duplicateObject','deleteObject'])$('#'+id).disabled=!editable||trackLocked(selection.track);
    for(const id of ['toStart','toEnd'])$('#'+id).disabled=!selected();
    $('#selectionLabel').textContent=project&&selected()?`${trackNames[selection.track]} · ${selection.index+1}`:'Chọn một câu hoặc vùng';
    if(!project){$('#actions').replaceChildren(button('Nhập video để bắt đầu',()=>$('#importDialog').showModal(),'primary'));return;}
    if(tool==='media'){
      const card=elem('div',undefined,'source-info');
      card.append(elem('h3',project.name),elem('p',project.media.path,'muted'),elem('p',`${project.media.width} × ${project.media.height} · ${fmt(duration())} · ${project.media.fps.toFixed(2)} fps`),elem('p',`${project.segments.length} câu · ${project.regions.length} vùng che`));
      $('#properties').append(card);
      $('#objectDetails').hidden=false;$('#objectDetailsTitle').textContent='Thông tin nguồn';
    }
    if(tool==='voice'){
      $('#externalAudioPath').value=project.params.voice_path||'';inspected=null;audioRevision++;
      $('#externalAudioStatus').textContent=project.params.voice_path?'Đang dùng tệp ngoài đã lưu. Bấm Kiểm tra tệp để đo lại metadata.':'Chưa chọn tệp ngoài. Có thể dùng giọng offline phía trên.';
      $('#externalAudioPlayer').hidden=!project.params.voice_path;
      if(project.params.voice_path)$('#externalAudioPlayer').src=mediaUrl(`/media/external/${project.id}/voice`);else $('#externalAudioPlayer').removeAttribute('src');
    }
    if(lastInspectorTool!==tool){$('#stageOptions').open=false;lastInspectorTool=tool;}
    $('#stageOptions > summary').textContent='Thông số nâng cao';
    if(tool==='clean'){
      const extra=elem('details',undefined,'extra-actions');extra.append(elem('summary','Dò vùng / công cụ nâng cao'));
      for(const b of $$('#actions button')){
        if(['Dò chữ ở khung','Quét mask theo thời gian','Xem thử 1 khung'].includes(b.textContent)){b.classList.remove('primary');extra.append(b);}
        if(b.textContent==='Xem thử 3 giây')b.classList.add('primary');
      }
      $('#stageOptions').append(extra);
    }
    $('#stageSettings').title='Cài đặt '+titles[tool]+' và mặc định dự án mới';
  };
  // Each result change updates the current stage without conflating ready/done/quality.
  updateResults=function(){originalResults();if(project&&tool==='voice'){$('#externalAudioPlayer').hidden=!project.params.voice_path;}};
  wire('voiceSettings',()=>openSettings('voice'));
  wire('previousStage',()=>setTool(steps[Math.max(0,steps.indexOf(tool)-1)]));
  wire('nextStage',()=>{requireProject();const index=steps.indexOf(tool);if(index===5)openSettings('pipeline');else setTool(steps[index+1]);});
  wire('export',()=>{requireProject();setTool('audio');});
  wire('resetLayout',()=>{document.documentElement.style.removeProperty('--video-share');$('#rightSplitter').setAttribute('aria-valuenow','47');zoom=0;pan={x:0,y:0};layoutViewer();});
  const splitter=$('#rightSplitter');
  function setVideoShare(value){const n=Math.max(35,Math.min(60,value));document.documentElement.style.setProperty('--video-share',n+'%');splitter.setAttribute('aria-valuenow',String(Math.round(n)));layoutViewer();}
  splitter.setAttribute('aria-valuenow','47');
  splitter.onkeydown=e=>{if(['ArrowLeft','ArrowRight','Home'].includes(e.key)){e.preventDefault();setVideoShare(e.key==='Home'?47:Number(splitter.getAttribute('aria-valuenow'))+(e.key==='ArrowLeft'?-5:5));}};
  splitter.onpointerdown=e=>{
    if(e.button!==0)return;splitter.setPointerCapture(e.pointerId);
    splitter.onpointermove=ev=>{const r=$('#workspace').getBoundingClientRect();setVideoShare((ev.clientX-r.left)/r.width*100);};
    splitter.onpointerup=splitter.onpointercancel=()=>splitter.onpointermove=null;
  };
  wire('exportVoiceSrt',async()=>{
    requireProject();if(!project.segments.length)throw Error('Chưa có phụ đề để xuất.');
    if(project.params.tts_require_translation&&project.segments.some(r=>!r.translated.trim()))throw Error('Hoàn thiện bản dịch trước khi xuất SRT lồng tiếng. Nếu nguồn đã là tiếng Việt, có thể bỏ yêu cầu bản dịch trong Giọng đọc.');
    await exportSrt();notify('Đã xuất SRT. Tự nhập tệp này trên SaydiVoice; chưa gửi nội dung lên dịch vụ.');
  });
  $('#externalAudioPath').oninput=()=>{audioRevision++;inspected=null;$('#externalAudioStatus').textContent='Đường dẫn đã thay đổi; cần kiểm tra lại.';};
  wire('pickExternalAudio',async()=>{
    if(!window.pywebview?.api){$('#externalAudioPath').focus();notify('Trong trình duyệt, nhập đường dẫn đầy đủ. Bản desktop có hộp chọn tệp.');return;}
    const path=await window.pywebview.api.pick_audio();if(path){$('#externalAudioPath').value=path;$('#externalAudioPath').dispatchEvent(new Event('input'));}
  });
  async function inspectAudio(apply=false){
    requireProject();const path=$('#externalAudioPath').value.trim(),id=project.id,revision=++audioRevision;
    if(!path)throw Error('Chọn hoặc nhập đường dẫn audio trước.');
    $('#externalAudioStatus').textContent='Đang đọc metadata audio trên máy…';
    for(const name of ['inspectExternalAudio','applyExternalAudio'])$('#'+name).disabled=true;
    try{
      const result=await api(`/projects/${id}/audio/inspect`,'POST',{path});
      if(revision!==audioRevision||id!==project?.id||path!==$('#externalAudioPath').value.trim())return;
      inspected=result;$('#externalAudioStatus').textContent=`${result.duration.toFixed(2)} giây · ${result.sample_rate} Hz · ${result.channels} kênh\nVideo ${duration().toFixed(2)} giây · lệch ${result.delta.toFixed(2)} giây\n${result.warnings.join('\n')||'Thời lượng gần video. Hãy nghe kiểm tra từng câu; không suy ra đồng bộ từ độ dài.'}`;
      if(!apply)return;
      if(result.warnings.length&&!confirm(result.warnings.join('\n')+'\nVẫn dùng tệp này? Không tự kéo giãn audio.'))return;
      checkpoint();project.params.voice_path=result.path;project.params.pipeline_tts=false;markDirty();await save();
      if(id!==project?.id)return;
      $('#externalAudioPath').value=result.path;$('#externalAudioPlayer').src=mediaUrl(`/media/external/${id}/voice`);$('#externalAudioPlayer').hidden=false;
      updateResults();syncVoice(true);notify('Đã dùng tệp ngoài và bỏ bước tạo giọng offline. Có thể phát cùng video nguồn, rồi xuất ở bước 6.');
    }catch(e){$('#externalAudioStatus').textContent=e.message;throw e;}finally{for(const name of ['inspectExternalAudio','applyExternalAudio'])$('#'+name).disabled=false;}
  }
  wire('inspectExternalAudio',()=>inspectAudio());wire('applyExternalAudio',()=>inspectAudio(true));
  wire('clearExternalAudio',async()=>{requireProject();checkpoint();project.params.voice_path='';markDirty();await save();voiceAudio.pause();voiceJobId=null;voiceAudio.removeAttribute('src');renderInspector();updateResults();notify('Đã bỏ đường dẫn trong dự án; không xóa tệp. Bật lại bước tạo giọng nếu muốn chạy offline.');});
  setTool(tool);
}
