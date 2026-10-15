/* Local workspace controls. Server preferences survive a changed loopback port. */
"use strict";
window.NPKUpgrade = (() => {
  const keys = {'npk-tts-text':'tts_text','npk-tts-voice':'tts_voice','npk-tts-speed':'tts_speed',
    'npk-tts-job':'tts_job','npk-favorite-voices':'favorite_voices','npk-viewer-ratio':'viewer_ratio',
    'npk-current':'current_project','npk-sample-text':'sample_text'};
  let pending = {}, timer, flushing = false, failures = 0, hardware = null, health = {},
      speechOptions = {tts_device:'auto',workload_mode:'quiet',pronunciation_dictionary:''}, paragraphs = [];
  let syncingParagraphs = false;
  const touched = new Set();
  function schedule(values) { Object.keys(values).forEach(key=>touched.add(key)); Object.assign(pending, values); clearTimeout(timer); timer = setTimeout(flush, 350); }
  async function flush(keepalive = false) {
    if (flushing || !Object.keys(pending).length) return;
    const patch = pending; pending = {}; flushing = true;
    try { await api('/api/studio/preferences', patch, 'PATCH', {keepalive}); }
    catch { pending = {...patch, ...pending}; }
    finally { flushing = false; if (Object.keys(pending).length && !keepalive) timer = setTimeout(flush, 4000); }
  }
  function store(key,value) {
    const field = keys[key]; if (!field) return;
    if (['tts_speed','viewer_ratio'].includes(field)) value = Number(value);
    if (field === 'favorite_voices') { try { value = JSON.parse(value); } catch { return; } }
    schedule({[field]: value});
  }
  async function hydrate() {
    try {
      const saved = await api('/api/studio/preferences');
      for (const [key, field] of Object.entries(keys)) {
        if (touched.has(field)) continue;
        // Only migrate old local drafts before any server preferences exist.
        // A saved empty string/list is intentional and clears stale port data.
        const value = saved[field];
        if (value !== undefined && (saved._persisted === true || (value !== '' && !(Array.isArray(value) && !value.length))))
          localStorage.setItem(key, Array.isArray(value) ? JSON.stringify(value) : value);
        else if (localStorage.getItem(key)) store(key, localStorage.getItem(key));
      }
      if (!touched.has('tts_params')) speechOptions = {...speechOptions,...saved.tts_params};
      if (!touched.has('narration_segments')) {
        if (touched.has('tts_text')) { paragraphs = []; saveParagraphs(); }
        else paragraphs = saved.narration_segments || [];
      }
      standaloneSpeech.text = localStorage.getItem('npk-tts-text') || '';
      standaloneSpeech.voiceId = localStorage.getItem('npk-tts-voice') || '';
      standaloneSpeech.speed = Number(localStorage.getItem('npk-tts-speed')) || 1;
      standaloneSpeech.jobId = localStorage.getItem('npk-tts-job') || '';
      $('#viewerRatio').value = localStorage.getItem('npk-viewer-ratio') || 54;
      document.documentElement.style.setProperty('--viewer', $('#viewerRatio').value + '%');
      if (area === 'text_to_speech' && !panelHasFocusedEditor()) renderPanel();
    } catch (e) { connectionError(e); }
  }
  window.addEventListener('pagehide', () => { clearTimeout(timer); flush(true); });
  function connectionError(error) {
    failures++;
    const bar = $('#backendConnectionStatus');
    bar.hidden = false;
    bar.textContent = error.status === 403 || !token
      ? 'Phiên truy cập không hợp lệ. Mở lại NPK từ biểu tượng ứng dụng; bản nháp đã lưu vẫn còn.'
      : 'Chưa đọc được bộ xử lý. NPK sẽ thử kết nối lại; hãy giữ cửa sổ này để tiếp tục. ' + (error.message || '');
  }
  function state(data) {
    failures = 0; $('#backendConnectionStatus').hidden = true;
    health = data.queue_health || {}; hardware = data.hardware || hardware;
    queueControls();
  }
  function queueControls() {
    const list = $('#jobList'); if (!list) return;
    let controls = $('#queueControls');
    if (!controls) { controls = document.createElement('div'); controls.id='queueControls'; list.before(controls); }
    const queued = jobs.filter(j=>j.state === 'queued').sort((a,b)=>(a.queue_order ?? a.created)-(b.queue_order ?? b.created));
    controls.innerHTML = `<div class="row"><button id="pauseQueue">${health.paused ? 'Tiếp tục hàng đợi' : 'Tạm dừng hàng đợi'}</button><small>${health.queued_count ?? queued.length} tác vụ chờ${health.paused ? ' · dừng trước tác vụ kế tiếp' : ''}</small></div>${health.healthy === false ? `<p class="warning">Hàng đợi đang thử phục hồi: ${esc(health.last_error)}</p>` : ''}${queued.length>1 ? '<div id="queueOrder"></div>' : ''}`;
    $('#pauseQueue').onclick = guard(async()=> { await api('/api/studio/queue', {paused: !health.paused}); await poll(); });
    if ($('#queueOrder')) {
      $('#queueOrder').innerHTML = queued.map((j,i)=>`<div class="row queue-item"><small>${i+1}. ${esc(j.project_name || kindNames[j.kind] || j.kind)}</small><span class="grow"></span><button data-queue-up="${j.id}" ${i===0?'disabled':''} aria-label="Đưa lên trước">↑</button><button data-queue-down="${j.id}" ${i===queued.length-1?'disabled':''} aria-label="Đưa xuống sau">↓</button></div>`).join('');
      for (const direction of ['up','down']) $$(`[data-queue-${direction}]`).forEach(b=> b.onclick=guard(async()=>{
        const id = b.dataset[direction==='up'?'queueUp':'queueDown'], ids=queued.map(j=>j.id), i=ids.indexOf(id), next=i+(direction==='up'?-1:1);
        [ids[i],ids[next]]=[ids[next],ids[i]];
        await api('/api/studio/queue/reorder',{ids}); await poll();
      }));
    }
  }
  const deviceOptions = [['auto','Tự động theo bộ đã cài'],['cpu','CPU'],['cuda','CUDA · NVIDIA']];
  const loadOptions = [['quiet','Êm · 1 luồng'],['balanced','Cân bằng · 2 luồng'],['fast','Nhanh · 4 luồng']];
  const options = (items,value) => items.map(([v,n])=>`<option value="${esc(v)}" ${value===v?'selected':''}>${esc(n)}</option>`).join('');
  function panel() {
    if (area === 'text_to_speech') enhanceSpeech();
    if (area === 'voice' && P) {
      $('#voiceSourceBody').insertAdjacentHTML('beforeend', `<details id="voiceAdvanced"><summary>Thiết bị và cách phát âm</summary><div>${field('tts_device','Thiết bị VieNeu','select',deviceOptions)}${field('workload_mode','Mức tải tạo giọng','select',loadOptions)}${field('pronunciation_dictionary','Từ điển phát âm · mỗi dòng: chữ viết => cách đọc','textarea')}<p class="muted">Ví dụ: NPK => en pi kê. Chỉ đổi lời được đọc; phụ đề hiển thị giữ nguyên.</p></div></details>`);
      $('#previewMix').before(Object.assign(document.createElement('div'),{innerHTML:field('voice_normalize','Cân bằng độ lớn giọng khi xuất (chuẩn LUFS)','check')+field('bgm_ducking','Hạ âm nguồn / nhạc nền khi có lời đọc','check')}));
      $('#previewMix').after(Object.assign(document.createElement('p'),{className:'muted',textContent:'Cân bằng LUFS và hạ nhạc áp dụng vào video xuất. Nghe phối âm trực tiếp hiện chỉ phản ánh âm lượng và mốc thời gian.'}));
      bindParams();
      $('#voiceSourceBody').insertAdjacentHTML('beforeend','<div class="row section-end"><button id="analyzeVoiceSource">Phân tích nhịp giọng nguồn</button><button id="suggestSpeakers">Gợi ý nhóm người nói</button><button id="enrollCustomVoice">Tạo giọng riêng từ mẫu</button><button id="separateSourceAudio">Tách giọng / nhạc nguồn · CPU</button></div><p class="muted">Tách âm chạy offline trên CPU và có thể lâu hơn độ dài video. Kết quả lưu riêng để nghe lại; chỉ đổi nhạc nền khi bạn chọn áp dụng.</p>');
      $('#analyzeVoiceSource').onclick=guard(audioAnalysis); $('#enrollCustomVoice').onclick=customVoice;
      $('#suggestSpeakers').onclick=guard(async()=>{const job=await queue('speaker_groups',selected.size?{segment_indices:[...selected]}:{});setProgressExpanded(true);await showJob(job.id);});
      $('#separateSourceAudio').onclick=guard(async()=>{const job=await queue('separate_audio');setProgressExpanded(true);await showJob(job.id);});
    }
    if (area==='projects') enhanceProjects();
    if (area==='subtitles' && P && subtab==='style') {
      $('#panel').insertAdjacentHTML('beforeend', field('subtitle_karaoke','Tô sáng từng từ khi có mốc ASR khớp nội dung','check')+'<p class="muted">Chỉ áp dụng khi lời hiển thị khớp từng từ đã nhận dạng; bản dịch khác ngôn ngữ giữ kiểu phụ đề thông thường.</p>'); bindParams();
    }
    if (area==='subtitles' && P && subtab==='content') {
      $('#cueSearch').before(Object.assign(document.createElement('div'), {className:'row section-end',innerHTML:'<button id="audioTiming">Waveform và nhịp nghỉ</button><button id="rewriteSpeech">Gợi ý rút gọn lời đọc</button>'}));
      $('#audioTiming').onclick=guard(audioAnalysis);
      $('#rewriteSpeech').onclick=guard(async()=>{
        if(!selected.size)throw new Error('Chọn các câu cần rút gọn lời đọc trước.');
        if(await ask('Gợi ý lời đọc vừa thời gian','Gửi các câu đã chọn và ngữ cảnh tới nhà cung cấp dịch đang chọn; có thể tính phí. Kết quả là đề xuất để bạn duyệt, không tự thay lời phụ đề.','Tạo gợi ý'))await queue('optimize_spoken',{segment_indices:[...selected]});
      });
    }
    if (area==='settings' && settingsTab==='performance') hardwareCard();
  }
  function hardwareCard() {
    const body=$('#settingsBody'); if (!body || $('#hardwareCard')) return;
    body.insertAdjacentHTML('beforeend', `<div class="card" id="hardwareCard"><h3>Bộ nhớ đang có</h3><p>${hardware ? `RAM trống ${(hardware.ram_available_mb/1024).toFixed(1)} / ${(hardware.ram_total_mb/1024).toFixed(1)} GiB · ${hardware.cpu_threads} luồng CPU` : 'Đang đọc phần cứng…'}</p>${(hardware?.gpus||[]).map(g=>`<p>${esc(g.name)} · VRAM trống ${g.free_mb} / ${g.total_mb} MiB</p>`).join('')}<p class="muted">Kiểm tra lại trước khi nhận dạng Whisper. Không đổi thiết bị hoặc giảm chất lượng ngầm.</p>${P ? field('asr_compute','Độ chính xác tính toán Whisper','select',[['auto','Tự động theo thiết bị'],['float16','float16 · CUDA'],['int8_float16','int8 + float16 · CUDA'],['int8','int8 · giảm bộ nhớ']]) : ''}</div>`); bindParams();
  }
  function enhanceSpeech() {
    const compose=$('.tts-compose'); if (!compose || $('#speechExtras')) return;
    compose.insertAdjacentHTML('beforeend', `<details id="speechExtras"><summary>Thiết bị, phát âm và đọc nhiều giọng</summary><div class="two"><label>Thiết bị VieNeu<select id="speechDevice">${options(deviceOptions,speechOptions.tts_device)}</select></label><label>Mức tải<select id="speechLoad">${options(loadOptions,speechOptions.workload_mode)}</select></label></div><label>Từ điển phát âm<textarea id="speechDictionary" maxlength="20000" rows="3" placeholder="NPK => en pi kê">${esc(speechOptions.pronunciation_dictionary)}</textarea></label><div class="row"><button id="splitNarration">Chia đoạn để phân giọng</button><button id="clearNarration">Đọc một giọng</button><button id="importNarration">Nhập TXT / SRT</button></div><p class="muted">Chia theo dòng trống. Mỗi đoạn có giọng, tốc độ và khoảng nghỉ riêng; sửa một đoạn sẽ tái dùng các đoạn còn khớp trong bộ nhớ đệm.</p><div id="narrationSegments"></div></details>`);
    $('#ttsRefreshVoices').after(Object.assign(document.createElement('button'),{id:'speechLibrary',textContent:'Thư viện bản đọc'}));
    $('#speechLibrary').after(Object.assign(document.createElement('button'),{id:'customVoice',textContent:'Tạo giọng riêng'}));
    $('#customVoice').onclick=customVoice;
    $('#speechLibrary').onclick=guard(()=>library());
    for (const [id,key] of [['speechDevice','tts_device'],['speechLoad','workload_mode'],['speechDictionary','pronunciation_dictionary']]) $('#'+id).onchange=()=>{speechOptions[key]=$('#'+id).value;schedule({tts_params:speechOptions});};
    $('#splitNarration').onclick=guard(()=>{
      const pieces=$('#ttsText').value.trim().split(/\n\s*\n/).filter(Boolean);
      if (!pieces.length || pieces.length>80) throw new Error('Nhập từ 1 đến 80 đoạn, phân cách bằng dòng trống.');
      paragraphs=pieces.map(text=>({text,voice_id:standaloneSpeech.voiceId,rate:standaloneSpeech.speed,pause_after_ms:300}));
      saveParagraphs(); renderParagraphs();
    });
    $('#clearNarration').onclick=()=>{paragraphs=[];saveParagraphs();renderParagraphs();};
    $('#ttsText').addEventListener('input',()=>{if (!syncingParagraphs && paragraphs.length) {paragraphs=[];saveParagraphs();renderParagraphs();}});
    $('#importNarration').onclick=()=>{
      const input=document.createElement('input');input.type='file';input.accept='.txt,.srt';
      input.onchange=guard(async()=>{const f=input.files[0];if(!f)return;if(f.size>200000)throw new Error('Tệp vượt 200 KB.');let text=await f.text();
        if(f.name.toLowerCase().endsWith('.srt'))text=text.replace(/^\uFEFF/,'').split(/\r?\n\s*\r?\n/).map(block=>block.split(/\r?\n/).filter(line=>!/^\s*\d+\s*$/.test(line)&&!line.includes('-->')).join(' ')).join('\n\n');
        if(text.length>10000)throw new Error('Nội dung vượt 10.000 ký tự; chia thành nhiều bài.');$('#ttsText').value=text;$('#ttsText').dispatchEvent(new Event('input'));});input.click();
    };
    renderParagraphs();
  }
  function saveParagraphs() { schedule({narration_segments:paragraphs}); }
  function syncText() { syncingParagraphs=true; $('#ttsText').value=paragraphs.map(p=>p.text).join('\n\n'); $('#ttsText').dispatchEvent(new Event('input'));syncingParagraphs=false; }
  function renderParagraphs() {
    const container=$('#narrationSegments'); if(!container)return;
    const voices=(catalog?.voices||[]).filter(v=>v.installed && v.available!==false);
    container.innerHTML=paragraphs.map((p,i)=>`<section class="narration-row"><h4>Đoạn ${i+1}</h4><textarea data-paragraph="${i}" data-value="text" rows="3" aria-label="Lời đọc đoạn ${i+1}">${esc(p.text)}</textarea><div class="two"><label>Giọng<select data-paragraph="${i}" data-value="voice_id">${voices.map(v=>`<option value="${esc(v.id)}" ${p.voice_id===v.id?'selected':''}>${esc(v.name)}</option>`).join('')}</select></label><label>Tốc độ<input type="number" min=".5" max="2" step=".05" value="${p.rate}" data-paragraph="${i}" data-value="rate"></label><label>Nghỉ sau (ms)<input type="number" min="0" max="10000" step="100" value="${p.pause_after_ms}" data-paragraph="${i}" data-value="pause_after_ms"></label></div></section>`).join('');
    container.querySelectorAll('[data-paragraph]').forEach(el=>el.onchange=()=>{const key=el.dataset.value;paragraphs[Number(el.dataset.paragraph)][key]=['rate','pause_after_ms'].includes(key)?Number(el.value):el.value;saveParagraphs();syncText();});
    $('#clearNarration').disabled=!paragraphs.length;
  }
  function speechPayload() {
    if(!paragraphs.length)return {};
    const text=paragraphs.map(p=>p.text.trim()).join('\n\n');
    if(text.length>10000 || paragraphs.some(p=>!p.text.trim() || !Number.isFinite(p.rate)||p.rate<.5||p.rate>2||!Number.isInteger(p.pause_after_ms)||p.pause_after_ms<0||p.pause_after_ms>10000))throw new Error('Kiểm tra lời đọc, tốc độ và khoảng nghỉ từng đoạn.');
    return {sample_text:text,narration_segments:paragraphs};
  }
  async function library(query='',offset=0) {
    const response=await api('/api/studio/speech?limit=15&offset='+offset+'&query='+encodeURIComponent(query));
    openModal('Thư viện bản đọc',`<div class="row"><input id="librarySearch" type="search" maxlength="200" value="${esc(query)}" placeholder="Tìm tên bản đọc"><button id="searchSpeech">Tìm</button></div><p class="muted">${response.total} bản đọc · đổi tên hoặc chuyển bản không cần dùng vào thùng rác.</p><div id="libraryRows">${response.items.map(item=>`<section class="card"><div class="row"><strong>${esc(item.name)}</strong><span class="grow"></span><small>${clock(item.duration)} · ${sizeText(item.bytes)}</small></div><p>${esc(item.voice || '')} · ${esc(stateNames[item.state] || item.state)}</p>${item.files.includes('speech.wav')?`<audio controls preload="none" src="${esc(mediaUrl('/media/job/'+item.id+'/speech.wav'))}"></audio>`:''}<div class="row">${item.files.map(f=>`<a class="tts-download" href="${esc(mediaUrl('/media/job/'+item.id+'/'+f))}" download>${esc(f.endsWith('mp3')?'MP3':'WAV')}</a>`).join('')}<button data-speech-rename="${item.id}">Đổi tên</button><button data-speech-trash="${item.id}" ${['queued','running'].includes(item.state)?'disabled':''}>Dọn bản đọc</button></div></section>`).join('')||'<p>Chưa có bản đọc phù hợp.</p>'}</div><div class="row"><button id="libraryPrev" ${offset===0?'disabled':''}>Trang trước</button><button id="libraryNext" ${offset+15>=response.total?'disabled':''}>Trang sau</button></div>`,[{label:'Đóng',run:closeModal}]);
    $('#searchSpeech').onclick=guard(()=>library($('#librarySearch').value,0));
    $('#libraryPrev').onclick=guard(()=>library(query,Math.max(0,offset-15))); $('#libraryNext').onclick=guard(()=>library(query,offset+15));
    $$('[data-speech-rename]').forEach(b=>b.onclick=()=>{
      const item=response.items.find(x=>x.id===b.dataset.speechRename);
      openModal('Đổi tên bản đọc',`<label>Tên bản đọc<input id="speechName" maxlength="160" value="${esc(item.name)}"></label>`,[{label:'Hủy',run:()=>library(query,offset)},{label:'Lưu',primary:true,run:async()=>{await api('/api/studio/speech/'+item.id+'/rename',{name:$('#speechName').value});await library(query,offset);}}]);
    });
    $$('[data-speech-trash]').forEach(b=>b.onclick=guard(async()=>{
      const id=b.dataset.speechTrash, plan=await api('/api/studio/storage/speech/'+id+'/plan',{});
      openModal('Dọn bản đọc',`<p>Chuyển ${sizeText(plan.bytes)} của bản đọc này vào thùng rác NPK. Bạn có thể khôi phục trong Dung lượng & thùng rác.</p>${plan.blocked?`<p class="warning">${esc(plan.blocked)}</p>`:''}`, [{label:'Hủy',run:()=>library(query,offset)},...(!plan.blocked?[{label:'Chuyển vào thùng rác',primary:true,run:async()=>{await api('/api/studio/storage/execute',{action:'speech',speech_id:id,confirmed:true,signature:plan.signature});await library(query,offset);}}]:[])]);
    }));
  }
  function customVoice() {
    openModal('Tạo giọng riêng trên máy',`<p>Dùng bản ghi của bạn hoặc mẫu bạn được phép sử dụng. Chọn 3–8 giây tiếng nói rõ, một người nói, ít nhạc và tiếng ồn để NPK tạo đặc trưng giọng.</p><label>Tên giọng<input id="customVoiceName" maxlength="80" placeholder="Giọng của tôi"></label><label>Tệp âm thanh tham chiếu<input id="customVoicePath" placeholder="D:\\Audio\\giong-cua-toi.wav"></label><div class="two"><label>Bắt đầu (giây)<input id="customVoiceStart" type="number" min="0" step=".1" value="0"></label><label>Lấy mẫu (3–8 giây)<input id="customVoiceDuration" type="number" min="3" max="8" step=".1" value="6"></label></div><p class="muted">Chất giọng và phát âm phụ thuộc mẫu; nên nghe thử rồi điều chỉnh. NPK xử lý mẫu offline bằng CAM++ và codec VieNeu.</p>`,[{label:'Chọn âm thanh',run:async()=>{if(window.pywebview?.api?.pick_audio)$('#customVoicePath').value=(await window.pywebview.api.pick_audio())||'';else notice('Dán đường dẫn tệp âm thanh vào ô tham chiếu.');}},{label:'Hủy',run:closeModal},{label:'Tạo giọng riêng',primary:true,run:async()=>{
      const job=await api('/api/jobs',{kind:'enroll_voice',name:$('#customVoiceName').value,reference_path:$('#customVoicePath').value,reference_start:Number($('#customVoiceStart').value),reference_duration:Number($('#customVoiceDuration').value),params:ttsParams(),request_id:crypto.randomUUID()});
      jobs.unshift(job);lastDetail=job.id;closeModal();renderProgress();setProgressExpanded(true);await showJob(job.id);notice('Đã xếp tạo giọng riêng. Khi xong, bấm Làm mới giọng để chọn và nghe thử.');
    }}]);
  }
  function ttsParams() { return {...speechOptions}; }
  async function audioAnalysis() {
    if(!P)throw new Error('Mở video có âm thanh trước.');
    await save();
    const existing=await api('/api/studio/projects/'+P.id+'/audio-insights');
    if(existing.ready && existing.insights) { showAudioInsights(existing.insights); return; }
    const job=await queue('analyze_audio');setProgressExpanded(true);await showJob(job.id);
    notice('Đang phân tích năng lượng và nhịp nghỉ offline. Khi xong, bấm lại để mở waveform.');
  }
  function job(detail) {
    const result=detail.result;if(!result)return;
    const add=(label,run)=>{const b=document.createElement('button');b.textContent=label;b.className='primary';b.onclick=guard(run);$('#jobDetails').prepend(b);};
    if(detail.kind==='analyze_audio' && detail.state==='done')add('Xem waveform và nhịp nguồn',async()=>{if(P?.id!==detail.project_id)await loadProject(detail.project_id);await audioAnalysis();});
    if(detail.kind==='enroll_voice' && detail.state==='done')add('Làm mới và chọn giọng riêng',async()=>{catalog=await api('/api/catalog?refresh_voices=true');standaloneSpeech.voiceId=result.voice_id;storePreference('npk-tts-voice',result.voice_id);setArea('text_to_speech');setProgressExpanded(false);});
    if(detail.kind==='optimize_spoken') {
      $('#applyJobResult')?.remove();
      if(result.suggestions?.length)add('Duyệt từng gợi ý lời đọc',async()=>{if(P?.id!==detail.project_id)await loadProject(detail.project_id);reviewSpeech(detail);});
    }
    if(detail.kind==='speaker_groups' && detail.state==='done')add('Duyệt nhóm và phân giọng',async()=>{if(P?.id!==detail.project_id)await loadProject(detail.project_id);await reviewSpeakers(detail);});
    if(detail.kind==='separate_audio' && detail.state==='done')add('Nghe giọng / nhạc đã tách',async()=>{if(P?.id!==detail.project_id)await loadProject(detail.project_id);await reviewSeparation(detail);});
  }
  async function reviewSeparation(detail) {
    const checked=await api('/api/studio/jobs/'+detail.id+'/audio-separation');
    if(!checked.ready)throw new Error('Kết quả tách âm đã cũ hoặc thiếu tệp. Chạy lại với nguồn hiện tại.');
    const result=checked.separation,id=P.id;
    openModal('Nghe và dùng nhạc nguồn đã tách',`<p class="muted">UVR MDX Inst_HQ_4 · CPU offline · ${clock(result.duration)}. Nghe cả hai bản trước khi phối. Model có thể để sót giọng hoặc làm mất một phần nhạc.</p>${(result.warnings||[]).map(w=>`<p class="warning">${esc(w)}</p>`).join('')}<section class="card"><h3>Nhạc / âm nền</h3><audio controls preload="none" src="${esc(mediaUrl('/media/job/'+detail.id+'/instrumentals.wav'))}"></audio><a class="tts-download" href="${esc(mediaUrl('/media/job/'+detail.id+'/instrumentals.wav'))}" download>Tải WAV nhạc nền</a></section><section class="card"><h3>Giọng tách khỏi nguồn</h3><audio controls preload="none" src="${esc(mediaUrl('/media/job/'+detail.id+'/vocals.wav'))}"></audio><a class="tts-download" href="${esc(mediaUrl('/media/job/'+detail.id+'/vocals.wav'))}" download>Tải WAV giọng nguồn</a></section><p>Khi dùng nhạc đã tách, NPK đặt âm nguồn bằng 0 và nhạc nền bằng 1 để tránh phát lại lời gốc. Bạn có thể chỉnh âm lượng và bật hạ nhạc lúc có lời trước khi xuất.</p>`,[{label:'Đóng',run:closeModal},{label:'Dùng nhạc đã tách',primary:true,run:async()=>{
      if(P?.id!==id)throw new Error('Dự án đã đổi. Mở lại kết quả tách âm.');
      await save();const updated=await api('/api/studio/jobs/'+detail.id+'/audio-separation/apply',{expected_revision:P.updated});
      closeModal();await loadProject(updated.id);setArea('voice');notice('Đã chọn nhạc đã tách và tắt âm nguồn. Nghe phối âm rồi chỉnh độ lớn trước khi xuất.');
    }}]);
  }
  function reviewSpeech(detail) {
    const result=detail.result,id=P.id;
    openModal('Duyệt lời đọc rút gọn',`<p class="muted">Chỉ thay lời được đọc của câu được chọn. Phụ đề hiển thị, giọng và mốc thời gian được giữ. Nghe lại để kiểm tra nghĩa và nhịp.</p>${result.suggestions.map(s=>`<section class="card"><label class="check"><input type="checkbox" data-spoken-proposal="${s.index}" ${s.accepted?'checked':'disabled'}>Câu ${s.index+1} · ${s.max_words} từ tối đa</label><p>${esc(P.segments[s.index]?.spoken_text || P.segments[s.index]?.translated || '')}</p><p><strong>Gợi ý:</strong> ${esc(s.spoken_text)}</p>${s.reason?`<p class="warning">${esc(s.reason)}</p>`:''}</section>`).join('')}`,[{label:'Hủy',run:closeModal},{label:'Áp dụng câu đã chọn',primary:true,run:()=>{
      if(P?.id!==id)throw new Error('Dự án đã đổi; mở lại đề xuất.');
      const indices=$$('[data-spoken-proposal]:checked').map(el=>Number(el.dataset.spokenProposal));
      for(const i of indices){const current=P.segments[i],proposal=result.segments[i],suggestion=result.suggestions.find(s=>s.index===i);if(!current || !proposal || typeof suggestion?.original_spoken_text!=='string' || (current.spoken_text||'')!==suggestion.original_spoken_text || ['start','end','text','translated'].some(k=>(current[k]??'')!==(proposal[k]??'')))throw new Error('Câu '+(i+1)+' đã sửa sau khi tạo gợi ý hoặc đề xuất cũ thiếu mốc đối chiếu; cần tạo gợi ý lại.');}
      change(project=>{for(const i of indices)project.segments[i].spoken_text=result.segments[i].spoken_text;});closeModal();setProgressExpanded(false);renderPanel();notice('Đã áp dụng '+indices.length+' lời đọc riêng. Tạo lại giọng để nghe kiểm tra.');
    }}]);
  }
  async function reviewSpeakers(detail) {
    const checked=await api('/api/studio/jobs/'+detail.id+'/speaker-groups');
    if(!checked.ready)throw new Error('Video hoặc mốc câu đã đổi. Chạy lại gợi ý nhóm người nói.');
    const result=checked.result,id=P.id,voices=(catalog?.voices||[]).filter(v=>v.installed&&v.available!==false);
    openModal('Duyệt nhóm và phân giọng',`<p class="warning">${esc((result.warnings||[]).join(' '))}</p>${result.groups.map(g=>`<section class="card"><h3>${esc(g.label)} · ${g.count} câu</h3><p class="muted">Câu ${g.segment_indices.map(i=>i+1).join(', ')}</p><label>Giọng dùng cho nhóm<select data-speaker-group="${esc(g.id)}"><option value="">Giữ giọng hiện tại</option>${voices.map(v=>`<option value="${esc(v.id)}">${esc(v.name)}</option>`).join('')}</select></label><button data-speaker-listen="${g.representative_index}">Nghe câu đại diện</button></section>`).join('')}${result.skipped?.length?`<p class="muted">${result.skipped.length} câu quá ngắn/không rõ được bỏ qua.</p>`:''}`,[{label:'Hủy',run:closeModal},{label:'Áp dụng phân giọng',primary:true,run:async()=>{
      const latest=await api('/api/studio/jobs/'+detail.id+'/speaker-groups');
      if(!latest.ready)throw new Error('Video hoặc mốc câu đã đổi; phân tích lại trước khi áp dụng.');
      if(P?.id!==id || detail.source_revision!==P.source_revision)throw new Error('Video nguồn đã đổi; phân tích lại trước khi áp dụng.');
      const mapping=new Map($$('[data-speaker-group]').map(el=>[el.dataset.speakerGroup,el.value]));
      for(const s of result.suggestions){const row=P.segments[s.segment_index];if(mapping.get(s.group_id) && (!row || row.start!==s.start || row.end!==s.end))throw new Error('Mốc câu đã thay đổi. Cần phân tích lại nhóm người nói.');}
      change(project=>{for(const s of result.suggestions){const voice=mapping.get(s.group_id);if(voice && project.segments[s.segment_index])project.segments[s.segment_index].voice_id=voice;}});
      closeModal();setArea('voice');setProgressExpanded(false);notice('Đã gán giọng cho các nhóm bạn chọn. Tạo lại lồng tiếng để nghe kiểm tra.');
    }}]);
    $$('[data-speaker-listen]').forEach(b=>b.onclick=()=>{const row=P.segments[Number(b.dataset.speakerListen)];seek(row.start);video.play().catch(()=>{});});
  }
  function showAudioInsights(insights) {
    if(!P)return;
    const source=insights.cues||[], waveform=insights.waveform||[], projectId=P.id, sourceRevision=P.source_revision;
    openModal('Waveform và nhịp giọng nguồn',`<p class="muted">${esc((insights.warnings||[]).join(' '))} Gợi ý dựa trên năng lượng / nhịp, có thể bị ảnh hưởng bởi nhạc và tiếng ồn. Không tự suy đoán cảm xúc hoặc đổi giọng.</p><canvas id="sourceWaveform" width="1000" height="120" role="img" aria-label="Sóng âm nguồn, bấm để chuyển thời gian"></canvas><p id="waveformPosition" class="muted">Bấm waveform để nghe tại vị trí đó.</p><div class="row"><button id="selectProsody">Chọn các câu đang chọn trong bảng</button><button id="applyProsody">Áp dụng tốc độ / nghỉ cho câu đã đánh dấu</button><button id="snapWords">Căn câu đánh dấu theo từ ASR đầu / cuối</button></div><div class="insight-table">${source.map(c=>{const p=c.source_prosody;return `<div class="card"><label class="check"><input type="checkbox" data-insight="${c.index}" ${selected.has(c.index)?'checked':''}>Câu ${c.index+1} · ${clock(c.start)}–${clock(c.end)}</label><p>${esc(P.segments[c.index]?.translated||P.segments[c.index]?.text||'')}</p><small>${p.rms_dbfs} dBFS · im lặng ${Math.round(p.silence_ratio*100)}% · tốc độ gợi ý ${p.suggested_rate}× · nghỉ ${p.pause_before_ms}/${p.pause_after_ms} ms</small></div>`;}).join('')}</div>`,[{label:'Đóng',run:closeModal}]);
    const canvas=$('#sourceWaveform'),ctx=canvas.getContext('2d'),duration=P.media.duration;
    ctx.fillStyle='#111927';ctx.fillRect(0,0,1000,120);ctx.fillStyle='#55c7c0';
    const peak=Math.max(.01,...waveform.map(p=>p.peak));
    for(const point of waveform){const x=point.time/duration*1000,h=Math.max(1,point.peak/peak*50);ctx.fillRect(x,60-h,Math.max(1,1000/waveform.length),h*2);}
    canvas.onclick=e=>{const rect=canvas.getBoundingClientRect(),time=Math.max(0,Math.min(duration,(e.clientX-rect.left)/rect.width*duration));seek(time);$('#waveformPosition').textContent='Vị trí '+clock(time);};
    const indices=()=>$$('[data-insight]:checked').map(e=>Number(e.dataset.insight));
    const validateTiming=chosen=>{if(P?.id!==projectId || P.source_revision!==sourceRevision)throw new Error('Video nguồn đã đổi. Mở lại phân tích.');for(const i of chosen){const cue=source.find(c=>c.index===i),row=P.segments[i];if(!cue||!row||row.start!==cue.start||row.end!==cue.end)throw new Error('Mốc câu đã đổi. Phân tích lại nhịp nguồn trước khi áp dụng.');}};
    $('#selectProsody').onclick=()=>$$('[data-insight]').forEach(e=>e.checked=selected.has(Number(e.dataset.insight)));
    $('#applyProsody').onclick=guard(()=>{
      if(P?.id!==projectId)throw new Error('Dự án đã đổi. Mở lại phân tích.');
      const chosen=indices();if(!chosen.length)throw new Error('Đánh dấu ít nhất một câu.');
      validateTiming(chosen);
      change(project=>{for(const i of chosen){const row=project.segments[i],p=source.find(c=>c.index===i)?.source_prosody;if(!row||!p||!Number.isFinite(p.suggested_rate)||p.silence_ratio>=.99)continue;const budget=Math.min(2000,Math.max(0,Math.floor((row.end-row.start)*250)));row.speech_rate=p.suggested_rate;row.pause_before_ms=Math.min(budget,Math.round(p.pause_before_ms));row.pause_after_ms=Math.min(budget,Math.round(p.pause_after_ms));}});
      closeModal();renderPanel();notice('Đã áp dụng gợi ý cho lời đọc. Có thể hoàn tác; hãy tạo lại giọng và nghe kiểm tra.');
    });
    $('#snapWords').onclick=guard(()=>{const chosen=indices();if(!chosen.length)throw new Error('Đánh dấu các câu có mốc ASR.');validateTiming(chosen);change(project=>{for(const i of chosen){const row=project.segments[i];if(row?.words?.length){const a=row.words[0].start,b=row.words.at(-1).end;if(b>a){row.start=a;row.end=b;row.pause_before_ms=0;row.pause_after_ms=0;}}}});closeModal();renderPanel();notice('Đã căn các câu có mốc từ ASR; nghe kiểm tra trước khi tạo giọng.');});
  }
  function enhanceProjects() {
    if($('#bundleProject'))return;
    $('#backupProject').parentElement.insertAdjacentHTML('afterend',`<div class="row section-end"><button id="bundleProject" ${P?'':'disabled'}>Sao lưu kèm video / âm thanh</button><button id="restoreBundle">Khôi phục ZIP dự án</button><button id="batchImport">Nhập nhiều video</button></div>`);
    $('#bundleProject').onclick=guard(async()=>{
      await save(); const id=P.id;
      openModal('Sao lưu dự án kèm dữ liệu',`<p>${esc(P.name)} · ZIP có manifest SHA-256 để kiểm tra khi khôi phục.</p><label class="check"><input id="bundleSource" type="checkbox" checked>Kèm video nguồn</label><label class="check"><input id="bundleAudio" type="checkbox" checked>Kèm nhạc / giọng ngoài đang dùng</label><label class="check"><input id="bundleFinal" type="checkbox" ${P.artifacts?.render || P.artifacts?.pipeline ? '' : 'disabled'}>Kèm video thành phẩm hiện có</label><p class="muted">Model, runtime và API key được cài/lưu riêng, không đưa vào gói dự án.</p>`,[{label:'Hủy',run:closeModal},{label:'Tạo ZIP',primary:true,run:()=>{const a=document.createElement('a');a.href=mediaUrl('/api/studio/projects/'+id+'/bundle')+'&source='+$('#bundleSource').checked+'&audio='+$('#bundleAudio').checked+'&final='+$('#bundleFinal').checked;a.download='NPK-du-an.zip';a.click();closeModal();notice('Đang tạo gói dự án. Giữ ứng dụng mở đến khi tải xong.');}}]);
    });
    $('#restoreBundle').onclick=()=>{const input=document.createElement('input');input.type='file';input.accept='.zip';input.onchange=guard(async()=>{const file=input.files[0];if(!file)return;
      openModal('Khôi phục ZIP dự án',`<p>${esc(file.name)} · NPK kiểm tra SHA-256 rồi tạo dự án mới.</p><label>Video nguồn nếu ZIP không kèm video<input id="bundleRelink" placeholder="Để trống nếu ZIP đã kèm nguồn"></label>`,[{label:'Chọn nguồn',run:async()=>{if(window.pywebview?.api?.pick_video)$('#bundleRelink').value=(await window.pywebview.api.pick_video())||'';else notice('Nhập đường dẫn video nếu ZIP không kèm nguồn.');}},{label:'Hủy',run:closeModal},{label:'Khôi phục',primary:true,run:async()=>{const data=new FormData();data.append('file',file);if($('#bundleRelink').value.trim())data.append('source',$('#bundleRelink').value.trim());notice('Đang kiểm tra checksum và khôi phục gói dự án…');const restored=await api('/api/studio/restore-bundle',data,'POST',{timeout:900000});closeModal();await loadProject(restored.id);await poll();setArea('subtitles');notice('Đã khôi phục vào dự án mới.');}}]);});input.click();};
    $('#batchImport').onclick=()=>batchImport();
  }
  function batchImport() {
    openModal('Nhập nhiều video',`<label>Đường dẫn video · mỗi dòng một tệp<textarea id="batchPaths" rows="6" placeholder="D:\\Videos\\01.mp4&#10;D:\\Videos\\02.mp4"></textarea></label><label class="check"><input id="batchPreset" type="checkbox" ${P?'checked':'disabled'}>Dùng thông số dự án đang mở</label><p class="muted">Tạo dự án riêng cho từng video. Kiểm tra vùng chữ / nguồn nhận dạng rồi xếp tác vụ; hàng đợi chỉ chạy một tác vụ mỗi lần.</p>`,[{label:'Chọn nhiều tệp',run:async()=>{if(window.pywebview?.api?.pick_videos)$('#batchPaths').value=(await window.pywebview.api.pick_videos()).join('\n');else notice('Dán các đường dẫn video, mỗi dòng một tệp.');}},{label:'Hủy',run:closeModal},{label:'Tạo các dự án',primary:true,run:async()=>{await save();const paths=[...new Set($('#batchPaths').value.split(/\r?\n/).map(s=>s.trim().replace(/^"|"$/g,'')).filter(Boolean))];if(!paths.length||paths.length>50)throw new Error('Chọn từ 1 đến 50 video.');const use=$('#batchPreset').checked,preset=use&&P?{...P.params,voice_path:'',bgm_path:'',extraction_start:0,extraction_end:0}:null;const created=[],errors=[];for(const path of paths){try{let p=await api('/api/projects',{path,name:path.split(/[\\/]/).pop()});if(preset)await api('/api/projects/'+p.id,{expected_revision:p.updated,params:preset},'PUT');created.push(p.id);}catch(e){errors.push(path+': '+e.message);}}closeModal();await poll();renderPanel();notice(`Đã tạo ${created.length}/${paths.length} dự án.`+(errors.length?' '+errors.join(' · '):''),!!errors.length);}}]);
  }
  return {store,hydrate,state,panel,connectionError,reset:(field,value)=>schedule({[field]:value}),retryDelay:()=>failures?Math.min(30000,2000*2**Math.min(failures,4)):0,
    ttsParams,speechPayload,library,showAudioInsights,job};
})();
