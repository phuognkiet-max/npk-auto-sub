"use strict";
// Guided setup shares the studio's saved project, queue and advanced editors.
window.NPKSingleVideoSetup = (() => {
  let serial = 0, running = false;
  const originalGains = new Map();
  const stepNames = {ocr: "Đọc chữ", asr: "Nghe lời thoại", auto_subtitles: "Tự xử lý phụ đề", translate: "Dịch phụ đề", tts: "Tạo giọng", clean: "Xử lý chữ gốc", render: "Xuất video"};
  function continuationPresentation(plan, editorCount = 0) {
    const completed = Array.isArray(plan?.completed_steps) ? plan.completed_steps : [];
    const remaining = Array.isArray(plan?.remaining_steps) ? plan.remaining_steps : [];
    const preRender = plan?.review_stage === "pre_render", reviewPending = preRender && !plan.approved;
    const selectionPending = plan?.next_action === "continue_auto_subtitles" || remaining.includes("auto_subtitles") &&
      (plan?.recognition_complete === true || completed.some(step => ["ocr", "asr"].includes(step)));
    const rawCount = Number(plan?.recognition_cue_count ?? plan?.recognition_count);
    const reused = Number(plan?.reused_translation_count) || 0, missing = Number(plan?.missing_translation_count) || 0;
    const checkpointCount = reused + missing > 0 ? reused + missing
      : Number.isInteger(rawCount) && rawCount > 0 ? rawCount : editorCount;
    return {
      selectionPending, preRender, reviewPending,
      savedText: selectionPending
        ? (Number.isInteger(rawCount) && rawCount > 0 ? `Đã lưu ${rawCount} câu nhận dạng; đang chờ tự chọn nội dung phụ đề.` : "Đã lưu kết quả nhận dạng; bước tự xử lý phụ đề chưa hoàn tất.")
        : checkpointCount > 0 ? `Đã lưu ${checkpointCount} câu phụ đề.${editorCount > 0 && editorCount !== checkpointCount ? ` Bảng biên tập hiện có ${editorCount} câu; kết quả mới nằm trong checkpoint tác vụ.` : ""}` : "",
      translationText: !selectionPending && (reused > 0 || missing > 0)
        ? `Giữ ${reused} câu dịch đã lưu; còn ${missing} câu cần dịch. Các câu bạn đã sửa được giữ nguyên.` : "",
      reason: plan?.reason || (selectionPending
        ? "Tiếp tục tự xử lý phụ đề từ kết quả nhận dạng đã lưu, rồi chạy các bước còn lại."
        : "Tiếp tục các bước còn lại từ kết quả đã có."),
      button: preRender ? (reviewPending ? "Xem và duyệt để xuất" : "Xuất video đã duyệt") : plan?.next_action === "continue_auto_subtitles" || selectionPending ? "Tiếp tục tự xử lý phụ đề" : "Tiếp tục các bước còn lại",
      startedText: preRender ? "Đang xuất video từ kết quả đã duyệt." : selectionPending ? "Đã tiếp tục tự xử lý phụ đề từ kết quả nhận dạng đã lưu." : "Đã tiếp tục các bước còn lại từ bảng phụ đề đã lưu.",
    };
  }
  function continuationSubtitleCount(plan, editorCount = 0) {
    const reused = Number(plan?.reused_translation_count) || 0;
    const missing = Number(plan?.missing_translation_count) || 0;
    const total = reused + missing;
    if (plan?.job_id && editorCount === 0 && total > 0)
      return `${total} câu trong checkpoint · ${reused} đã dịch, ${missing} cần dịch`;
    return null;
  }
  function continuationCanAct(plan) {
    return plan?.review_stage === "pre_render" && !plan.approved
      ? plan.can_approve === true : plan?.can_continue === true;
  }
  const presets = {
    clean: {name: "Clean", note: "Rõ ràng · tối giản", font: "Arial", font_size: 46, font_color: "#FFFFFF", outline_color: "#000000", outline: 3, subtitle_shadow: 0, margin: 40, subtitle_background: false, subtitle_bold: false, subtitle_alignment: "bottom", subtitle_karaoke: false},
    pop: {name: "Pop", note: "Nổi bật · chuyển động nhẹ", font: "Arial", font_size: 54, font_color: "#FFD84D", outline_color: "#000000", outline: 4, subtitle_shadow: 2, margin: 48, subtitle_background: false, subtitle_bold: true, subtitle_alignment: "bottom", subtitle_karaoke: false},
    highlight: {name: "Highlight", note: "Đổi màu từ đang đọc · nền tương phản", font: "Arial", font_size: 48, font_color: "#FFFFFF", outline_color: "#000000", outline: 3, subtitle_shadow: 0, margin: 44, subtitle_background: true, subtitle_bold: true, subtitle_alignment: "bottom", subtitle_karaoke: false},
    karaoke: {name: "Karaoke", note: "Chạy màu theo lời đọc · cần căn âm thanh", font: "Arial", font_size: 48, font_color: "#53E0E4", outline_color: "#000000", outline: 3, subtitle_shadow: 1, margin: 44, subtitle_background: false, subtitle_bold: true, subtitle_alignment: "bottom", subtitle_karaoke: true},
  };
  function render(ctx) {
    const {panel, escape: esc, icon, api, change, save, queue, openArea, notice, guard} = ctx;
    const project = ctx.project(), params = project?.params || {};
    let root = null, continuation = null, restartRecognition = false;
    const mounted = () => !!root?.isConnected && panel.contains(root) && ctx.project()?.id === project?.id;
    // Scope callbacks to this rendered setup, not a later panel with the same IDs.
    const find = id => mounted() ? root.querySelector("#" + id) : null;
    const select = (id, label, value, options) => `<label>${label}<select id="${id}">${options.map(([key, name]) => `<option value="${esc(key)}" ${String(key) === String(value) ? "selected" : ""}>${esc(name)}</option>`).join("")}</select></label>`;
    const toggle = (id, label, value) => `<label class="check"><input id="${id}" type="checkbox" ${value ? "checked" : ""}>${label}</label>`;
    const heading = (n, title, suffix = "") => `<div class="setup-section-head"><span class="setup-number">${n}</span><h3>${title}</h3>${suffix}</div>`;
    const upload = `<div id="setupDrop" class="setup-drop" role="button" tabindex="0" aria-label="Chọn hoặc thả một video">${icon("video")}<strong>${project ? esc(project.name) : "Chọn một video để bắt đầu"}</strong><small>${project ? `${esc(ctx.clock(project.media.duration))} · ${project.media.width} × ${project.media.height}` : "Kéo thả MP4, MOV, MKV hoặc chọn tệp trên máy"}</small><button id="setupImport">${project ? "Nhập video khác" : "Chọn video"}</button><input id="setupVideoFile" type="file" accept="video/*,.mkv,.m4v" hidden></div>`;
    if (!project) {
      panel.innerHTML = `<div class="single-video-setup"><div class="panel-title"><h2>Thiết lập &amp; chạy</h2></div>${upload}<p class="muted setup-empty-note">Một video, một lượt thiết lập. Chọn cách xử lý chữ, phụ đề và giọng trước khi bắt đầu.</p></div>`;
      root = panel.firstElementChild;
      bindImport(); return null;
    }
    const source = params.tts_text_source || "edited", extract = params.pipeline_extract || "existing";
    const translated = project.segments.filter(row => row.translated?.trim()).length;
    const voice = (ctx.catalog()?.voices || []).find(row => row.id === params.voice_id);
    const usedVoices = new Set(project.segments.map(row => row.voice_id || params.voice_id));
    const voiceSummary = usedVoices.size > 1 ? `${usedVoices.size} giọng theo từng câu · mặc định: ${voice?.name || params.voice_id}` : voice?.name || params.voice_id;
    panel.innerHTML = `<div class="single-video-setup"><div class="panel-title setup-heading"><h2>Thiết lập &amp; chạy</h2><span class="pill">Một video</span></div>${upload}
      <section class="setup-section">${heading(1, "Xử lý chữ gốc", toggle("setupClean", "Bật", params.use_clean))}<div id="setupCleanFields">${select("setupMethod", "Cách xử lý", params.method, [["blur","Làm mờ · nhanh"],["box","Hộp màu · nhanh"],["stretch","Kéo nền"],["telea","Lấp nền"],["lama","Phục hồi AI · nặng hơn"]])}<div class="setup-inline"><span id="setupRegionCount">${project.regions.length} vùng đã chọn</span><button data-setup-area="clean">Vẽ / chỉnh vùng</button></div></div></section>
      <section class="setup-section">${heading(2, "Nội dung phụ đề")}${select("setupExtraction", "Lấy phụ đề từ", extract, [["existing",`Bảng hiện có · ${project.segments.length} câu`],["ocr","Đọc chữ trên hình (OCR)"],["asr","Nghe lời thoại (STT)"]])}<div class="setup-inline"><span id="setupSubtitleCount">${translated}/${project.segments.length} câu đã dịch</span><button id="setupImportSubtitles">Nhập SRT / ASS</button><button data-setup-area="subtitles">Chỉnh câu</button></div><div id="setupRecognitionDetails" class="setup-inline" hidden><span id="setupRecognitionHint"></span><button id="setupRecognitionEdit">Chọn vùng / ngôn ngữ</button></div>${toggle("setupTranslate","Dịch sang ngôn ngữ đích",params.pipeline_translate)}<div id="setupTranslationFields" class="two">${select("setupProvider","Dịch vụ dịch",params.provider,[["gemini","Gemini"],["deepseek","DeepSeek"],["router9","9Router"]])}<label>Ngôn ngữ đích<input id="setupTarget" value="${esc(params.target_language)}"></label></div><div class="setup-inline"><small id="setupConnection" role="status"></small><button id="setupConnectionEdit" class="link">Kết nối dịch vụ</button></div><details class="setup-details"><summary>Tùy chọn dịch</summary><label id="setupRouter9Endpoint" hidden>Địa chỉ API 9Router<input id="setupRouter9BaseUrl" value="${esc(params.router9_base_url || "http://127.0.0.1:20128/v1")}"><small>Chọn model từ danh mục của địa chỉ này trong Kết nối dịch vụ.</small></label><label>Mô hình<input id="setupModel" value="${esc(params.translation_model)}"></label><label>Văn phong<textarea id="setupStyle">${esc(params.style)}</textarea></label><small>Khi bật dịch, phụ đề được gửi đến dịch vụ đã chọn; có thể tính phí.</small></details></section>
      <section class="setup-section">${heading(3,"Hiển thị phụ đề",toggle("setupDisplay","Bật",params.burn_subtitles))}<div id="setupPresetCards" class="setup-presets">${Object.entries(presets).map(([key,preset]) => `<button type="button" data-setup-preset="${key}" class="setup-preset${params.subtitle_preset === key ? " selected" : ""}" aria-pressed="${params.subtitle_preset === key}" title="${esc(preset.note)}"><span class="setup-preset-sample preset-${key}"><span>Video</span> của bạn</span><strong>${preset.name}</strong><small>${preset.note}</small></button>`).join("")}</div><div class="setup-inline"><button id="setupExactPreview">Xem mẫu như khi xuất</button><button id="setupAdvancedStyle" class="link">Chỉnh kiểu chi tiết</button></div><div id="setupPreviewResult"></div></section>
      <section class="setup-section">${heading(4,"Lồng tiếng",toggle("setupDub","Bật",params.voice_gain > 0))}<div id="setupVoiceFields">${select("setupTextSource","Nội dung lồng tiếng",source,[["translated","Bản dịch"],["edited","Lời đã biên tập"],["source","Phụ đề gốc"]])}<small id="setupTextSourceHint" role="status"></small>${select("setupVoiceMethod","Cách tạo giọng",params.voice_path ? "external" : "local",[["local","Giọng AI trên máy"],["external","Tệp âm thanh có sẵn"]])}<div class="setup-voice-summary"><span id="setupVoiceName">${esc(params.voice_path ? "Tệp âm thanh đang chọn" : voice?.name || params.voice_id)}</span><button id="setupPickVoice">${params.voice_path ? "Chọn tệp / chỉnh âm" : "Chọn / nghe giọng"}</button></div><div class="two"><label>Tốc độ đọc<input id="setupVoiceSpeed" type="number" min=".5" max="2" step=".05" value="${esc(params.voice_speed)}"></label><label>Âm lượng giọng<input id="setupVoiceGain" type="range" min="0" max="2" step=".05" value="${esc(params.voice_gain)}"></label></div><details class="setup-details"><summary>Phối âm</summary><label>Âm lượng nguồn<input id="setupOriginalGain" type="range" min="0" max="2" step=".05" value="${esc(params.original_gain)}"></label><small title="Âm lượng nguồn bao gồm cả giọng gốc và nhạc; không phải tách giọng.">Âm nguồn có thể còn lời thoại gốc.</small><div class="setup-inline"><button id="setupAudioAdvanced">Tách giọng / chọn nhạc / phối âm</button></div></details></div></section>
      <section class="setup-section">${heading(5,"Video thành phẩm")}<label>Tên tệp<input id="setupOutputName" value="${esc(params.export_name)}" maxlength="120"></label><div class="two">${select("setupResolution","Độ phân giải",params.export_resolution,[["source","Giữ như nguồn"],["720","Tối đa 720p"],["1080","Tối đa Full HD"],["2160","Tối đa 4K"]])}${select("setupQuality","Chất lượng",[18,20,26].includes(+params.video_quality) ? params.video_quality : "custom",[[18,"Cao"],[20,"Cân bằng"],[26,"Gọn nhẹ"],["custom","Giữ tùy chỉnh"]])}</div><label>Thư mục xuất<input id="setupOutputDirectory" value="${esc(params.export_directory)}" placeholder="Mặc định: thư mục exports của ứng dụng"></label><button id="setupPickDirectory">Chọn thư mục</button></section>
      <section class="setup-summary"><h3>Kiểm tra trước khi chạy</h3><div id="setupCheck" role="status">Đang kiểm tra cấu hình…</div><button id="setupReview" class="link" hidden>Kiểm tra phụ đề</button><details id="setupWarnings" hidden><summary>Lưu ý trước khi chạy</summary><div></div></details><button id="setupRun" class="primary" disabled>Bắt đầu xử lý video</button></section></div>`;
    root = panel.firstElementChild;
    bindImport();
    find("setupExtraction").closest("label").insertAdjacentHTML("beforebegin", `${select("setupSubtitleAutomation", "Xử lý phụ đề", params.subtitle_automation || "manual", [["local", "Tự động trên máy"], ["gemini", "Tự động với Gemini"], ["router9", "Tự động với 9Router"], ["manual", "Thủ công · nâng cao"]])}<small id="setupAutomationHint" role="status"></small><p id="setupGeminiDisclosure" class="setup-gemini-disclosure" hidden>Gemini nhận nội dung OCR và một số ảnh mẫu của video để chọn phụ đề. Cần khóa Gemini; có thể tính phí. Chọn chế độ này để bật hỗ trợ Gemini.</p>`);
    find("setupPickDirectory").insertAdjacentHTML("afterend", '<div id="setupFinalResult" class="setup-final-result" hidden></div>');
    find("setupCheck").insertAdjacentHTML("beforebegin", '<div id="setupPipelineState" class="setup-pipeline-state" role="status" hidden></div>');
    find("setupCheck").insertAdjacentHTML("beforebegin", '<div id="setupAdmission" class="warning" role="status" hidden></div>');
    find("setupExtraction").closest("label").insertAdjacentHTML("afterend", `${toggle("setupTextCleanup", "Làm sạch chữ OCR, gộp câu lặp và mảnh câu trước khi dịch", params.subtitle_text_cleanup)}<small>Giữ bản gốc trong lịch sử và báo cáo để đối chiếu. Câu khác người nói hoặc có lời đọc đã biên tập cần được xem riêng.</small>`);
    find("setupOutputName").closest("label").insertAdjacentHTML("beforebegin", `${toggle("setupReviewBeforeRender", "Xem và duyệt kết quả trước khi xuất video", params.review_before_render)}<small>Khi bật, quy trình dừng sau chuẩn bị phụ đề, giọng và hình. Bạn chủ động xem, nghe và bấm duyệt để xuất.</small>`);
    find("setupVoiceFields").insertAdjacentHTML("beforebegin", toggle("setupKeepOriginal", "Giữ âm thanh gốc", params.original_gain > 0));
    if (params.original_gain > 0) originalGains.set(project.id, Number(params.original_gain));
    find("setupReview").insertAdjacentHTML("afterend", '<button id="setupRecognitionReset" class="link" hidden>Nhận dạng lại từ đầu</button>');
    find("setupReview").insertAdjacentHTML("afterend", '<button id="setupPrepareAgain" hidden>Chuẩn bị lại sau chỉnh sửa</button>');
    find("setupPrepareAgain").onclick = guard(async () => {
      if (running || activeJob()) return;
      running = true; find("setupPrepareAgain").disabled = true;
      try {
        const id = ctx.project().id;
        change(row => {row.params.pipeline_extract = "existing"; row.params.subtitle_automation = "manual";});
        find("setupExtraction").value = "existing"; find("setupSubtitleAutomation").value = "manual";
        await prepare();
        if (!mounted() || ctx.project()?.id !== id) throw new Error("Video đang mở đã thay đổi. Kiểm tra lại trước khi chuẩn bị.");
        await queue("pipeline", {replace_segments: true});
        notice("Đang chuẩn bị lại từ bảng hiện tại. Giữ bản dịch đã có; dùng lại giọng và hình còn khớp. Khi bật duyệt, quy trình sẽ dừng trước khi xuất.");
      } finally {
        running = false;
        if (find("setupPrepareAgain")) {find("setupPrepareAgain").disabled = false; await refresh();}
      }
    });
    find("setupReview").insertAdjacentHTML("afterend", '<button id="setupAutoSubtitles" class="primary" hidden>Tự xử lý phụ đề</button>');
    find("setupAutoSubtitles").onclick = guard(async () => {
      if(running || activeJob())return;
      const id=ctx.project().id;await save();
      if(!mounted() || ctx.project()?.id!==id)return;
      await queue("auto_subtitles",{replace_segments:true});notice("Đang tự chọn phụ đề và kiểm tra phần còn thiếu. Theo dõi trong Tiến trình.");
      await refresh();
    });
    find("setupRecognitionReset").onclick = guard(async () => {
      // A full recognition pass is a deliberate choice, never a consequence of
      // pressing Run again after a pipeline paused with its cues already saved.
      restartRecognition = true;
      await refresh();
      notice("Lượt mới sẽ nhận dạng lại video. Các bước đã chọn sẽ chạy tiếp sau nhận dạng.");
    });
    find("setupPresetCards").insertAdjacentHTML("afterend", `<div id="setupCaptionLayout" class="two">${select("setupCaptionFlow", "Cách hiển thị chữ", params.subtitle_flow || "wrap", [["single", "Một dòng · tự chuyển cụm"], ["wrap", "Cả câu · xuống dòng"]])}<label id="setupChunkWordsLabel">Tối đa từ mỗi cụm<input id="setupChunkWords" type="number" min="2" max="12" step="1" value="${esc(params.subtitle_chunk_words || 6)}"></label></div>`);
    find("setupTextSourceHint").insertAdjacentHTML("afterend",`${toggle("setupExpressive","Đọc tiếng cười theo ngữ cảnh · VieNeu",params.tts_expressive)}<small id="setupSpeechState" role="status"></small>`);
    find("setupExpressive").closest("label").title="Đổi cụm tiếng cười hi hi, hì hì, ha ha thành tiếng cười; giữ nguyên phụ đề. Chưa có thẻ tiếng hét cho Á!.";
    panel.querySelectorAll("[data-setup-area]").forEach(button => button.onclick = () => openArea(button.dataset.setupArea));
    const commit = (id,key,convert=value=>value) => find(id).onchange = guard(async event => {
      const control=event.target, value=convert(control.type==="checkbox" ? control.checked : control.value);
      if(typeof value==="number" && (!control.value.trim() || !Number.isFinite(value))) throw new Error("Nhập giá trị hợp lệ.");
      if(key==="subtitle_chunk_words" && (!Number.isInteger(value) || value<2 || value>12)) {
        control.value=ctx.project().params.subtitle_chunk_words || 6;
        throw new Error("Nhập số nguyên từ 2 đến 12 từ mỗi cụm.");
      }
      change(row=>{if(key==="provider" && row.params.provider!==value){row.params.translation_model="";find("setupModel").value="";}row.params[key]=value; if(key==="use_clean")row.params.pipeline_clean=value; if(key==="voice_gain")row.params.pipeline_tts=value>0&&!row.params.voice_path;});
      if(["burn_subtitles", "subtitle_flow", "subtitle_chunk_words"].includes(key))ctx.previewStyle?.();
      visibility(); await refresh();
    });
    commit("setupClean","use_clean"); commit("setupMethod","method"); commit("setupExtraction","pipeline_extract");
    commit("setupSubtitleAutomation","subtitle_automation");
    commit("setupTextCleanup","subtitle_text_cleanup"); commit("setupReviewBeforeRender","review_before_render");
    commit("setupTranslate","pipeline_translate"); commit("setupProvider","provider"); commit("setupTarget","target_language");
    commit("setupRouter9BaseUrl","router9_base_url",value=>{
      let endpoint=value.trim().replace(/\/+$/, "");
      try {const url=new URL(endpoint);if(/^\/dashboard\/?$/.test(url.pathname))endpoint=url.origin+"/v1";}catch(_){}
      find("setupRouter9BaseUrl").value=endpoint;return endpoint;
    });
    commit("setupModel","translation_model"); commit("setupStyle","style"); commit("setupDisplay","burn_subtitles");
    commit("setupCaptionFlow","subtitle_flow"); commit("setupChunkWords","subtitle_chunk_words",Number);
    commit("setupTextSource","tts_text_source"); commit("setupExpressive","tts_expressive"); commit("setupVoiceSpeed","voice_speed",Number); commit("setupVoiceGain","voice_gain",Number);
    commit("setupOriginalGain","original_gain",Number); commit("setupOutputName","export_name"); commit("setupOutputDirectory","export_directory");
    const originalGainChange = find("setupOriginalGain").onchange;
    find("setupOriginalGain").onchange = guard(async event => {
      await originalGainChange(event);
      if (!mounted()) return;
      const gain = Number(ctx.project().params.original_gain);
      find("setupKeepOriginal").checked = gain > 0;
      if (gain > 0) originalGains.set(project.id, gain);
    });
    find("setupKeepOriginal").onchange = guard(async event => {
      const row = ctx.project(), gain = Number(row.params.original_gain);
      if (gain > 0) originalGains.set(row.id, gain);
      change(item => item.params.original_gain = event.target.checked ? originalGains.get(row.id) || 1 : 0);
      find("setupOriginalGain").value = ctx.project().params.original_gain;
      await refresh();
    });
    commit("setupResolution","export_resolution");
    find("setupDub").onchange=guard(async event=>{change(row=>{row.params.voice_gain=event.target.checked?1:0;row.params.pipeline_tts=event.target.checked&&!row.params.voice_path;});find("setupVoiceGain").value=ctx.project().params.voice_gain;visibility();await refresh();});
    find("setupQuality").onchange=guard(async event=>{if(event.target.value!=="custom")change(row=>row.params.video_quality=+event.target.value);await refresh();});
    find("setupVoiceMethod").onchange=guard(async event=>{if(event.target.value==="local"){change(row=>row.params.voice_path="");await refresh();}else ctx.externalVoice();});
    panel.querySelectorAll("[data-setup-preset]").forEach(button=>button.onclick=guard(async()=>{
      const key=button.dataset.setupPreset,{name,note,...style}=presets[key];change(row=>Object.assign(row.params,style,{subtitle_preset:key}));
      panel.querySelectorAll("[data-setup-preset]").forEach(item=>{item.classList.toggle("selected",item===button);item.setAttribute("aria-pressed",String(item===button));});
      ctx.previewStyle?.();
      await refresh();
    }));
    find("setupImportSubtitles").onclick=()=>ctx.importSubtitles();
    find("setupRecognitionEdit").onclick=()=>openArea("subtitles");
    find("setupConnectionEdit").onclick=()=>openArea("settings","connections");
    find("setupAdvancedStyle").onclick=()=>openArea("subtitles","style");
    find("setupPickVoice").onclick=find("setupAudioAdvanced").onclick=()=>openArea("voice");
    find("setupReview").onclick=guard(async()=>continuation?.review_stage === "pre_render"
      ? ctx.reviewBeforeRender(continuation.job_id) : openArea("subtitles","quality"));
    find("setupExactPreview").onclick=guard(async()=>{const id=ctx.project().id;await save();if(ctx.project()?.id!==id)return;const rows=ctx.project().segments;if(!rows.length)throw new Error("Tạo hoặc nhập phụ đề trước để xem mẫu xuất.");if(!rows.some(row=>row.start<=ctx.time()&&row.end>ctx.time()))ctx.seek(rows[0].start+.01);await queue("subtitle_preview");notice("Đã xếp mẫu phụ đề tại vị trí đang xem.");});
    if(project.artifacts?.render||project.artifacts?.pipeline?.primary_file==="final.mp4"){
      find("setupExactPreview").insertAdjacentHTML("afterend",'<button id="setupStyleExport" title="Dùng lại video sạch và giọng đã tạo; không chạy lại nhận dạng hay dịch">Xuất lại kiểu chữ</button>');
      find("setupStyleExport").onclick=guard(async()=>{
        if(activeJob())return;
        const id=ctx.project().id;await save();if(ctx.project()?.id!==id)return;
        await queue("render");notice("Đang xuất lại với kiểu phụ đề mới, dùng giọng và video sạch đã có.");
      });
    }
    if(!params.voice_path)find("setupVoiceName").textContent=voiceSummary;
    const preview=project.artifacts?.subtitle_preview;
    if(preview)find("setupPreviewResult").innerHTML=`<img class="setup-exact-image" alt="Mẫu phụ đề từ bộ xuất video" src="${esc(ctx.mediaUrl('/media/job/'+preview.job_id+'/subtitle-preview.png'))}">`;
    find("setupPickDirectory").onclick=guard(async()=>{const path=await ctx.pickDirectory();if(path&&mounted()){change(row=>row.params.export_directory=path);find("setupOutputDirectory").value=path;await refresh();}});
    find("setupRun").onclick=guard(async()=>{
      if(running||activeJob())return;running=true;const button=find("setupRun");button.disabled=true;button.textContent="Đang xếp tác vụ…";
      let rejectedAdmission=null;
      try{
        if (continuation?.job_id && continuation.review_stage === "pre_render" && !restartRecognition) {
          if (!continuation.approved) await ctx.reviewBeforeRender(continuation.job_id);
          else { await ctx.continuePipeline(continuation.job_id); notice("Đang xuất video từ kết quả đã duyệt."); }
          return;
        }
        const id=ctx.project().id;await prepare();if(ctx.project()?.id!==id)throw new Error("Video đang mở đã thay đổi. Thiết lập lại trước khi chạy.");const row=ctx.project();
        if(continuation?.job_id && !restartRecognition){
          await ctx.continuePipeline(continuation.job_id);
          notice(continuationPresentation(continuation, ctx.project().segments.length).startedText);
          return;
        }
        if(row.params.pipeline_extract==="existing" && (row.params.burn_subtitles||row.params.voice_gain>0)){
          const quality=await api('/api/studio/projects/'+id+'/quality');
          const automatic=(ctx.project().params.subtitle_automation || "manual")!=="manual"&&quality.automation?.available;
          if((quality.errors.length||quality.requires_review)&&!automatic){openArea("subtitles","quality");throw new Error("Kiểm tra các câu và phạm vi phụ đề trước khi chạy.");}
        }
        const check=await api('/api/preflight',{project_id:id,kind:"pipeline"});
        if(!check.ok)throw new Error(check.errors.join("\n"));
        if(ctx.project()?.id!==id)throw new Error("Video đang mở đã thay đổi. Thiết lập lại trước khi chạy.");
        await queue("pipeline",{replace_segments:true});
        notice("Đã bắt đầu quy trình. Mở Tiến trình để theo dõi từng bước.");
      }catch(error){rejectedAdmission=error.execution_admission;throw error;
      }finally{running=false;if(find("setupRun")){await refresh();if(rejectedAdmission?.execution_allowed===false)showAdmission(rejectedAdmission);}}
    });
    visibility();guard(refresh)();
    return {refresh};

    function activeJob(){return ctx.jobs().find(job=>job.project_id===ctx.project()?.id&&["queued","running"].includes(job.state)&&["pipeline","render","auto_subtitles"].includes(job.kind));}
    function showAdmission(report){
      const target=find("setupAdmission");if(!target)return;
      const blocked=report?.execution_allowed===false;
      target.hidden=!blocked;if(!blocked){target.replaceChildren();return;}
      const labels={gemini:"Gemini",deepseek:"DeepSeek",router9:"9Router",unknown:"Chưa xác định"};
      const connections=(report.required_connections||[]).map(row=>labels[row.provider]||row.provider).filter(Boolean);
      target.innerHTML=`<strong>Chưa thể bắt đầu xử lý</strong><p>${esc(report.blocked_reason||"Kết nối API chưa sẵn sàng.")}</p>${connections.length?`<p>Kết nối cần dùng: ${esc([...new Set(connections)].join(", "))}.</p>`:""}`;
      if(/quota|audit|auth|provider|monitor/.test(report.blocked_code||"")){
        target.insertAdjacentHTML("beforeend",'<button id="setupAdmissionConnection">Kết nối dịch vụ</button><small>Xem kết nối và model đang dùng. Đổi kết nối không tự xác nhận quota đã phục hồi.</small>');
        find("setupAdmissionConnection").onclick=()=>openArea("settings","connections");
      }
      find("setupRun").disabled=true;find("setupRun").title=report.blocked_reason||"Chưa thể bắt đầu xử lý.";
      find("setupAutoSubtitles").disabled=true;
    }
    function matchingJobs(){return ctx.jobs().filter(job=>job.project_id===ctx.project()?.id&&(job.source_revision??null)===(ctx.project()?.source_revision??null)&&["pipeline","render"].includes(job.kind));}
    function showPipelineState(plan){
      const state=find("setupPipelineState"),latest=matchingJobs()[0];
      state.hidden=!plan?.job_id&&!latest;
      if(plan?.job_id){
        const view=continuationPresentation(plan,ctx.project().segments.length);
        if(view.selectionPending)find("setupSubtitleCount").textContent="Kết quả nhận dạng đã lưu · Chờ tự xử lý phụ đề";
        else {
          const checkpointCount=continuationSubtitleCount(plan,ctx.project().segments.length);
          if(checkpointCount)find("setupSubtitleCount").textContent=checkpointCount;
        }
        state.className="setup-pipeline-state warning";
        state.innerHTML=`<strong>${view.preRender ? "Đã chuẩn bị xong · Chờ duyệt và xuất video" : "Quy trình chưa hoàn tất · Chưa có video thành phẩm"}</strong><p>${view.savedText ? esc(view.savedText)+" " : ""}${esc(view.reason)}</p><div class="setup-stage-list">${(plan.completed_steps||[]).map(step=>`<span data-stage-state="done">✓ ${esc(stepNames[step]||step)}</span>`).join("")}${(plan.remaining_steps||[]).map(step=>`<span data-stage-state="pending">○ ${esc(stepNames[step]||step)}</span>`).join("")}</div>`;
        if(plan.recovery_job_id&&view.translationText)state.insertAdjacentHTML('beforeend',`<p>${esc(view.translationText)}</p>`);
        find("setupRecognitionReset").hidden=!['ocr','asr'].includes(ctx.project().params.pipeline_extract);
        if(restartRecognition)state.insertAdjacentHTML("beforeend",'<p>Lượt tiếp theo sẽ nhận dạng lại từ đầu theo lựa chọn của bạn.</p>');
      }else{
        find("setupRecognitionReset").hidden=true;
        state.className="setup-pipeline-state "+(latest?.state==='done'?"info":"warning");
        state.innerHTML=latest?.state==='done'?'<strong>Lượt xử lý gần nhất đã hoàn tất</strong>':latest?`<strong>${esc(latest.state==='review'?"Quy trình tạm dừng · Chưa có video thành phẩm":latest.state==='error'?"Quy trình dừng do lỗi · Chưa có video thành phẩm":"Lượt xử lý trước đã dừng")}</strong><p>${esc(latest.message||"")}</p>`:"";
      }
    }
    async function showFinalResult(current){
      const target=find("setupFinalResult"),project=ctx.project();
      const resolved=await api('/api/studio/projects/'+project.id+'/final-result');
      if(current!==serial||!mounted()||resolved.expected_revision!==ctx.project()?.updated)return;
      const result=resolved.current||resolved.historical;
      target.hidden=!result;if(!result)return;
      const id=result.job_id,path=result.path,older=!result.current;
      target.innerHTML=`<strong>${older?"Video hoàn tất của lượt trước":"Đã xuất video thành phẩm"}</strong>${older?`<p>${esc(result.reason||"Lượt đang dừng ở trên chưa tạo video mới.")}</p>`:""}<p class="setup-result-path">${esc(path)}</p><div class="setup-inline"><button id="setupOpenFinal" class="primary">${older?"Mở video lượt trước":"Mở video thành phẩm"}</button><button id="setupOpenFinalFolder">Mở thư mục</button></div>`;
      find("setupOpenFinal").onclick=guard(()=>api('/api/studio/jobs/'+id+'/open-result',{}));
      find("setupOpenFinalFolder").onclick=guard(()=>api('/api/studio/jobs/'+id+'/open-result-folder',{}));
    }
    function visibility(){
      const row=ctx.project();if(!mounted()||!row||!find("setupCleanFields"))return;const p=row.params;
      const automation=p.subtitle_automation || "manual";
      find("setupGeminiDisclosure").hidden=!["gemini","router9"].includes(automation);
      find("setupGeminiDisclosure").textContent=automation==="router9"
        ? "9Router nhận bằng chứng OCR và ảnh mẫu để chọn phụ đề qua model đã chọn. Chọn model hỗ trợ ảnh trong Kết nối dịch vụ; có thể tính phí."
        : "Gemini nhận nội dung OCR và một số ảnh mẫu của video để chọn phụ đề. Cần khóa Gemini; có thể tính phí. Chọn chế độ này để bật hỗ trợ Gemini.";
      find("setupRouter9Endpoint").hidden=p.provider!=="router9";
      find("setupAutomationHint").textContent=({local:"Tự chọn lớp phụ đề, lọc chữ nền và kiểm tra phần còn thiếu. Chỉ báo khi chưa đủ bằng chứng.",gemini:"Gemini hỗ trợ chọn nội dung từ bằng chứng OCR; NPK kiểm tra kết quả trước khi dùng.",router9:"9Router hỗ trợ chọn nội dung từ bằng chứng OCR bằng model bạn chọn; NPK kiểm tra kết quả trước khi dùng.",manual:"Tự chọn lớp chữ, sửa câu và xác nhận phạm vi trong mục Phụ đề."})[automation];
      find("setupCleanFields").hidden=!p.use_clean;find("setupTranslationFields").hidden=!p.pipeline_translate;find("setupVoiceFields").hidden=p.voice_gain<=0;
      find("setupPresetCards").hidden=!p.burn_subtitles;find("setupExactPreview").disabled=!p.burn_subtitles;
      find("setupCaptionLayout").hidden=!p.burn_subtitles;
      find("setupChunkWordsLabel").hidden=(p.subtitle_flow || "wrap")!=="single";
      let timingHint = find("setupWordTimingHint");
      if (!timingHint) {
        timingHint = document.createElement("small"); timingHint.id = "setupWordTimingHint";
        timingHint.setAttribute("role", "status"); find("setupPresetCards").after(timingHint);
      }
      timingHint.hidden = !p.burn_subtitles || !["highlight", "karaoke"].includes(p.subtitle_preset);
      const displayedCues = row.segments.filter(cue => cue.translated?.trim() || cue.text?.trim());
      const aligned = displayedCues.filter(cue => window.NPKCaptionTiming?.timeline(cue, cue.translated?.trim() || cue.text)?.source === "aligned").length;
      timingHint.textContent = aligned ? `Căn theo âm thanh: ${aligned}/${displayedCues.length} câu. ${aligned < displayedCues.length ? "Các câu còn lại dùng nhịp từ ước lượng." : ""}`
        : "Nhịp từ ước lượng · Căn theo âm thanh lồng tiếng khi xử lý video. Xem mẫu dùng mốc đã có.";
      find("setupRecognitionDetails").hidden=p.pipeline_extract==="existing";
      const regions=(row.extraction_regions??row.regions).length;
      find("setupRecognitionHint").textContent=p.pipeline_extract==="ocr"?(regions?`${regions} vùng đọc · toàn video`:"Đọc toàn khung · chọn vùng để tập trung vào phụ đề"):"Nghe âm thanh của video";
      const different=row.segments.filter(cue=>cue.spoken_text?.trim()&&cue.spoken_text.trim()!==cue.translated?.trim()).length;
      find("setupTextSourceHint").textContent=({translated:"Đọc đầy đủ bản dịch; lời biên tập riêng không được dùng.",edited:"Ưu tiên lời đã biên tập, sau đó bản dịch.",source:"Đọc nguyên nội dung phụ đề gốc, không dùng bản dịch."})[p.tts_text_source||"edited"]+((p.tts_text_source||"edited")==="edited"&&different?` ${different} câu có lời đọc khác phụ đề.`:"");
    }
    async function prepare(forRun=true){
      const row=ctx.project(),p=row.params;const translate=!!p.pipeline_translate&&(p.pipeline_extract!=="existing"||row.segments.some(cue=>!cue.translated?.trim()));
      const additions={pipeline_clean:!!p.use_clean,pipeline_tts:p.voice_gain>0&&!p.voice_path,translation_scope:"missing"};
      if(forRun&&!p.tts_text_source)additions.tts_text_source=find("setupTextSource").value;
      if(p.pipeline_extract!=="existing"){additions.extraction_start=0;additions.extraction_end=0;}
      // Automatic selection can replace the OCR rows, introducing untranslated
      // cues. Keep the user's translation choice until the worker knows its corpus.
      if(!translate&&(p.subtitle_automation || "manual")==="manual")additions.pipeline_translate=false;
      if(Object.entries(additions).some(([key,value])=>p[key]!==value))change(project=>Object.assign(project.params,additions));
      await save();
    }
    async function refresh(){
      const current=++serial,id=ctx.project()?.id;if(!id||!find("setupCheck"))return;
      find("setupRun").disabled=true;find("setupCheck").textContent="Đang kiểm tra cấu hình…";
      find("setupCheck").hidden=false;
      find("setupRun").title="";find("setupAutoSubtitles").disabled=false;
      const active=activeJob();
      if(active){
        find("setupAdmission").hidden=true;
        find("setupCheck").className="info";find("setupCheck").textContent="Video đang được xử lý. Cấu hình được giữ nguyên cho đến khi tác vụ kết thúc.";
        find("setupRun").textContent="Đang xử lý video…";
        find("setupPrepareAgain").hidden=true;
        find("setupFinalResult").hidden=true;find("setupReview").hidden=true;find("setupAutoSubtitles").hidden=true;find("setupRecognitionReset").hidden=true;
        find("setupPipelineState").hidden=false;find("setupPipelineState").className="setup-pipeline-state info";
        find("setupPipelineState").innerHTML='<strong>Quy trình đang chạy · Chưa có video mới</strong><p>Theo dõi từng bước trong Tiến trình xử lý. Video thành phẩm sẽ hiện ở đây khi đã kiểm tra và lưu xong.</p>';
        panel.querySelectorAll("input:not([type=file]),select,[data-setup-preset],#setupExactPreview,#setupStyleExport,#setupPickDirectory,#setupPickVoice,#setupAudioAdvanced").forEach(control=>control.disabled=true);
        return;
      }
      try{
        await prepare(false);
        if(current!==serial||!mounted())return;
        find("setupTranslate").checked=!!ctx.project().params.pipeline_translate;visibility();
        const [check,status,quality,plan,resume]=await Promise.all([api('/api/preflight',{project_id:id,kind:"pipeline"}),api('/api/keys/'+ctx.project().params.provider+'/status'),api('/api/studio/projects/'+id+'/quality'),api('/api/studio/projects/'+id+'/export-plan'),api('/api/studio/projects/'+id+'/pipeline-continuation')]);
        if(current!==serial||ctx.project()?.id!==id||!mounted())return;
        continuation=resume;showPipelineState(resume);
        find("setupConnection").textContent=status.present?`Đã lưu kết nối: ${ctx.project().params.provider} · ${ctx.project().params.translation_model || "Chưa chọn model"}${ctx.project().params.provider === "router9" ? " · " + ctx.project().params.router9_base_url : ""}`:(ctx.project().params.provider==="router9"?"Chưa có token 9Router":"Chưa có khóa kết nối dịch vụ");
        const staleSpeech=!!ctx.project().artifacts?.tts&&plan.steps.some(step=>step.id==="tts"&&!step.reuse);
        find("setupSpeechState").textContent=staleSpeech?"Cần tạo lại giọng để khớp nội dung đang chọn. Thành phẩm đang xem vẫn dùng giọng đã xuất.":"";
        if(find("setupStyleExport")){find("setupStyleExport").disabled=staleSpeech;find("setupStyleExport").title=staleSpeech?"Bấm Bắt đầu xử lý video để tạo giọng khớp nội dung trước khi xuất lại":"Dùng lại video sạch và giọng đã tạo";}
        const continuing=!!resume.job_id&&!restartRecognition;
        const admission=continuing?(resume.execution_admission||(typeof resume.execution_allowed === "boolean" ? resume : check.execution_admission)):check.execution_admission;
        const preRender=continuing&&resume.review_stage==="pre_render", reviewPending=preRender&&!resume.approved;
        find("setupPrepareAgain").hidden=!(preRender&&!resume.can_approve);
        const automation=ctx.project().params.subtitle_automation || "manual";
        const canAutomate=automation!=="manual"&&!!quality.automation?.available;
        const messages=continuing?[...(resume.errors||[])]:[...check.errors], review=continuing?!!resume.requires_review:ctx.project().params.pipeline_extract==="existing"&&quality.requires_review&&(ctx.project().params.burn_subtitles||ctx.project().params.voice_gain>0);
        const missingCues=ctx.project().segments.length===0&&ctx.project().params.pipeline_extract==="existing"&&messages.some(message=>message.includes("Chưa có phụ đề"));
        const geminiUnavailable=automation==="gemini"&&messages.some(message=>message.includes("kết nối Gemini")||message.includes("API key Gemini"));
        const offerAutomaticRecognition=!continuing&&(missingCues||geminiUnavailable);
        if(review&&!preRender&&!canAutomate&&!messages.some(text=>text.includes("kiểm tra phạm vi")))messages.push("Phụ đề cần xem lại trước khi xuất.");
        if(continuing&&!resume.can_continue&&!(reviewPending&&resume.can_approve)&&!messages.length)messages.push(resume.reason||"Chưa thể tiếp tục; kiểm tra phụ đề và cấu hình.");
        if(admission?.execution_allowed===false){
          for(let i=messages.length-1;i>=0;i--)if(messages[i]===admission.blocked_reason)messages.splice(i,1);
          find("setupCheck").hidden=!messages.length;
        }
        find("setupReview").hidden=!review;find("setupReview").textContent=preRender?"Xem và duyệt kết quả":"Kiểm tra phụ đề";find("setupCheck").className=messages.length?"warning":"setup-ready";
        find("setupAutoSubtitles").hidden=preRender||!(canAutomate&&(review||quality.errors?.length));
        const continuationView=continuationPresentation(resume,ctx.project().segments.length);
        const active=activeJob();find("setupCheck").innerHTML=active?`<span>${icon("video")}Video đang có tác vụ xử lý.</span>`:messages.length?messages.map(text=>`<p>${esc(text)}</p>`).join("")+ (offerAutomaticRecognition?'<button id="setupAutomaticRecognition" class="primary">Tự nhận dạng phụ đề trên máy</button><small>Quét toàn khung video và tự chọn lớp chữ. Dịch vẫn dùng dịch vụ, model và khóa hiện tại; phần chưa rõ sẽ được giữ để xem lại.</small>':""):`<span>${icon("check")}${continuing?"Sẵn sàng tiếp tục các bước còn lại":"Cấu hình sẵn sàng"}</span><small>${continuing?(continuationView.selectionPending?"Dùng kết quả nhận dạng đã lưu · Tự xử lý phụ đề trước":"Dùng phụ đề đã nhận dạng · Không nhận dạng lại"):ctx.project().params.pipeline_extract==="existing"?"Dùng bảng phụ đề hiện có":"Nhận dạng phụ đề trước"} · ${ctx.project().params.use_clean?"Xử lý chữ gốc":"Giữ hình gốc"} · ${ctx.project().params.voice_gain>0?"Có lồng tiếng":"Không lồng tiếng"}</small>`;
        if(find("setupAutomaticRecognition"))find("setupAutomaticRecognition").onclick=guard(async()=>{
          if(running||activeJob())return;
          change(row=>{row.params.pipeline_extract="ocr";row.params.subtitle_automation="local";row.params.extraction_start=0;row.params.extraction_end=0;});
          await save();
          if(!mounted()||ctx.project()?.id!==id)return;
          find("setupExtraction").value="ocr";find("setupSubtitleAutomation").value="local";visibility();
          notice("Đã chọn OCR toàn khung và tự xử lý phụ đề trên máy. Dịch vẫn dùng dịch vụ hiện tại.");
          await refresh();
        });
        const warnings=continuing?resume.warnings||[]:check.warnings;
        find("setupWarnings").hidden=!warnings.length;find("setupWarnings").querySelector("div").innerHTML=warnings.map(text=>`<p>${esc(text)}</p>`).join("");
        find("setupRun").textContent=continuing?continuationView.button:restartRecognition?"Bắt đầu nhận dạng lại và xử lý video":"Bắt đầu xử lý video";
        find("setupRun").disabled=running||!!active||messages.length>0||(continuing&&!continuationCanAct(resume));
        showAdmission(admission);
        await showFinalResult(current);
      }catch(error){if(current!==serial||!mounted())return;if(find("setupCheck")){find("setupCheck").className="warning";find("setupCheck").textContent=error.message;}throw error;}
    }
    function bindImport(){
      const drop=find("setupDrop"),file=find("setupVideoFile"),button=find("setupImport");
      const pick=()=>{if(ctx.nativePicker())ctx.importVideo();else file.click();};
      button.onclick=event=>{event.stopPropagation();pick();};drop.onclick=event=>{if(event.target===drop||event.target.closest("strong,small"))pick();};
      drop.onkeydown=event=>{if(event.target===drop&&["Enter"," "].includes(event.key)){event.preventDefault();pick();}};
      file.onchange=guard(async()=>{const chosen=file.files[0];file.value="";if(chosen)await ctx.upload(chosen);});
      drop.ondragover=event=>{event.preventDefault();drop.classList.add("dragover");};drop.ondragleave=()=>drop.classList.remove("dragover");
      drop.ondrop=guard(async event=>{event.preventDefault();drop.classList.remove("dragover");const files=[...event.dataTransfer.files];if(files.length!==1)throw new Error("Chọn một video cho mỗi lượt thiết lập.");await ctx.upload(files[0]);});
    }
  }
  return {render, continuationPresentation, continuationSubtitleCount, continuationCanAct, stepNames};
})();
