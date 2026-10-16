"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const token =
  new URLSearchParams(location.hash.slice(1)).get("token") ||
  sessionStorage.getItem("npk-token") ||
  "";
if (token) sessionStorage.setItem("npk-token", token);
history.replaceState(null, "", location.pathname);
const glyphs = {
  play: "M8 5l11 7-11 7z",
  pause: "M8 5v14M16 5v14",
  previous: "M5 5v14M19 5l-9 7 9 7z",
  next: "M19 5v14M5 5l9 7-9 7z",
  volume: "M11 5L6 9H3v6h3l5 4zM15 8a6 6 0 010 8M18 5a10 10 0 010 14",
  muted: "M11 5L6 9H3v6h3l5 4zM16 9l6 6M22 9l-6 6",
  captions: "M3 5h18v14H3zM6 10h5M14 10h4M6 14h3M12 14h6",
  eraser: "M4 16l10-12 7 6-10 11H8zM10 9l7 6M11 21h10",
  folder: "M3 7V4h6l3 3h9v13H3z",
  settings:
    "M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1M12 8a4 4 0 110 8 4 4 0 010-8",
  undo: "M3 10h12a6 6 0 010 12M3 10l6-6M3 10l6 6",
  redo: "M21 10H9a6 6 0 000 12M21 10l-6-6M21 10l-6 6",
  fit: "M3 9V3h6M15 3h6v6M21 15v6h-6M9 21H3v-6M8 8h8v8H8z",
  fullscreen: "M3 9V3h6M15 3h6v6M21 15v6h-6M9 21H3v-6",
  close: "M6 6l12 12M18 6L6 18",
  check: "M4 12l5 5L20 6",
  video: "M3 5h13v14H3zM16 10l5-3v10l-5-3",
  plus: "M12 5v14M5 12h14",
  download: "M12 3v12M6 9l6 6 6-6M4 17v4h16v-4",
  star: "M12 3l3 6 7 1-5 5 1 7-6-3-6 3 1-7-5-5 7-1z",
};
function icon(name) {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="${glyphs[name] || glyphs.check}"/></svg>`;
}
function icons(root = document) {
  root
    .querySelectorAll("[data-icon]")
    .forEach((el) => (el.innerHTML = icon(el.dataset.icon)));
}
function ib(id, name, label, attrs = "") {
  return `<button ${id ? `id="${id}"` : ""} class="icon" aria-label="${esc(label)}" title="${esc(label)}" ${attrs}>${icon(name)}</button>`;
}
const clock = (v) => {
  v = Math.max(0, Number(v) || 0);
  return `${String(Math.floor(v / 60)).padStart(2, "0")}:${(v % 60).toFixed(2).padStart(5, "0")}`;
};
const mediaUrl = (path) => `${path}?token=${encodeURIComponent(token)}`;
async function api(path, body, method, options = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout || (body ? 120000 : 20000));
  let response;
  try { response = await fetch(path, {
    method: method || (body ? "POST" : "GET"),
    headers: { "x-studio-token": token, ...(body instanceof FormData ? {} : {"Content-Type": "application/json"}) },
    body: body ? (body instanceof FormData ? body : JSON.stringify(body)) : undefined,
    signal: controller.signal, keepalive: !!options.keepalive,
  });
  if (!response.ok) {
    let data;
    try {
      data = await response.json();
    } catch {
      data = { detail: "Không kết nối được bộ xử lý." };
    }
    const e = new Error(
      typeof data.detail === "string"
        ? data.detail
        : "Thông số gửi chưa hợp lệ.",
    );
    e.status = response.status;
    e.execution_admission = data.execution_admission;
    throw e;
  }
  return await response.json();
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('Bộ xử lý phản hồi quá lâu. Kiểm tra kết nối hoặc tiến trình; chưa tự gửi lại tác vụ.');
    throw error;
  } finally { clearTimeout(timer); }
}
function storePreference(key, value) {
  localStorage.setItem(key, value);
  window.NPKUpgrade?.store(key, value);
}
let noticeTimer;
function notice(text, error = false) {
  clearTimeout(noticeTimer);
  $("#notice").textContent = text;
  $("#notice").className = error ? "error" : "";
  $("#notice").hidden = false;
  noticeTimer = setTimeout(
    () => ($("#notice").hidden = true),
    error ? 14000 : 7000,
  );
}
function guard(fn) {
  return async (...args) => {
    try {
      return await fn(...args);
    } catch (e) {
      notice(e.message, true);
      console.error("NPK action failed:", e.status || "local");
    }
  };
}
let modalResolve = null,
  modalOrigin = null;
function closeModal(value = false) {
  $("#modal").close();
  modalResolve?.(value);
  modalResolve = null;
  modalOrigin?.focus?.();
}
function openModal(title, html, buttons) {
  if ($("#modal").open) closeModal(false);
  modalOrigin = document.activeElement;
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = html;
  $("#modalActions").innerHTML = "";
  for (const b of buttons) {
    const el = document.createElement("button");
    el.textContent = b.label;
    if (b.primary) el.className = "primary";
    el.onclick = guard(async () => {
      el.disabled = true;
      try {
        await b.run();
      } finally {
        el.disabled = false;
      }
    });
    $("#modalActions").append(el);
  }
  $("#modal").showModal();
  $("#modalBody").querySelector("input,select,textarea")?.focus();
}
function ask(title, text, label = "Tiếp tục") {
  return new Promise((resolve) => {
    openModal(title, `<p>${esc(text).replace(/\n/g, "<br>")}</p>`, [
      { label: "Hủy", run: () => closeModal(false) },
      { label, primary: true, run: () => closeModal(true) },
    ]);
    modalResolve = resolve;
  });
}
$("#modalClose").onclick = () => closeModal(false);
$("#modal").addEventListener("cancel", (e) => {
  e.preventDefault();
  closeModal(false);
});
let P = null,
  projects = [],
  jobs = [],
  catalog = null,
  area = "setup",
  subtab = "content",
  settingsTab = "connections",
  selected = new Set(),
  page = 0,
  query = "",
  voiceQuery = "",
  voicePage = 0,
  setupController = null;
let undo = [],
  redo = [],
  dirty = false,
  editVersion = 0,
  saveTimer = null,
  saving = null,
  conflict = false,
  currentCue = -1,
  previewJob = null,
  previewVoice = null,
  quality = null,
  lastDetail = null;
let importAsTarget = false,
  mixEnabled = false,
  drawMode = false;
let styleScope = "all",
  voiceScope = "all";
let captionPreviewEnabled = false,
  viewerLoadRevision = 0;
const finalResolutions = new Map(), finalResultRequests = new Map();
let followVideo = false;
const standaloneSpeech = {
  text: (localStorage.getItem("npk-tts-text") || "").slice(0, 10000),
  voiceId: localStorage.getItem("npk-tts-voice") || "",
  speed: Math.max(.5, Math.min(2, Number(localStorage.getItem("npk-tts-speed")) || 1)),
  jobId: localStorage.getItem("npk-tts-job") || "",
  expressive: localStorage.getItem("npk-tts-expressive") === "true",
};
let standaloneSpeechSubmitting = false,
  standaloneSpeechPollBusy = false,
  standaloneSpeechDetail = null,
  standaloneSpeechOutputKey = "";
const styleKeys = new Set([
  "font",
  "font_size",
  "font_color",
  "outline_color",
  "outline",
  "subtitle_background",
  "subtitle_alignment",
  "subtitle_shadow",
  "subtitle_margin_x",
  "margin",
  "subtitle_line_chars",
  "subtitle_max_lines",
  "subtitle_bold",
  "subtitle_preset",
  "subtitle_karaoke",
  "subtitle_flow",
  "subtitle_chunk_words",
]);
function applyParam(p, key, value) {
  if (
    area === "subtitles" &&
    subtab === "style" &&
    styleScope === "selected" &&
    styleKeys.has(key) &&
    selected.size
  ) {
    selected.forEach((i) => {
      if (p.segments[i])
        p.segments[i].subtitle_style = {
          ...(p.segments[i].subtitle_style || {}),
          [key]: value,
        };
    });
  } else p.params[key] = value;
  if (area === "subtitles" && subtab === "style" &&
      (styleKeys.has(key) || key === "burn_subtitles")) previewStyle();
}
function readingRegions() {
  return P.extraction_regions ?? P.regions;
}
const panel = $("#panel"),
  video = $("#video");
const cleanLiveKeys = new Set(["method", "blur_radius", "cover_opacity", "edge_feather", "cover_color", "dilate",
  "stretch_height", "stretch_direction", "stretch_offset", "inpaint_radius", "context", "max_side", "mask_mode", "mask_source"]);
let cleanSliderHeld = false;
function cleanDraftRegions() {
  const regions = structuredClone(P?.regions || []);
  if (area === "clean") $$(".region-input").forEach(el => {
    const value = Number(el.value);
    const row=regions[+el.dataset.i];
    if (!row || el.value===el.dataset.originalDisplay || el.value==="" || !Number.isFinite(value)) return;
    const next={...row,[el.dataset.key]:value};
    try { validateRegionEdit(next); regions[+el.dataset.i]=regionGeometry.clamp(next,P.media); } catch (_) { /* Keep the last valid region while an input is incomplete. */ }
  });
  return regions;
}
function cleanLiveSnapshot() {
  if (!P || area !== "clean") return null;
  const params = Object.fromEntries([...cleanLiveKeys].map(k => [k, P.params[k]]));
  $$("#panel .param").forEach(el => {
    if (!cleanLiveKeys.has(el.dataset.key) || el.value === "") return;
    const value = ["number", "range"].includes(el.type) ? Number(el.value) : el.value;
    if (typeof value !== "number" || Number.isFinite(value)) params[el.dataset.key] = value;
  });
  const regions = cleanDraftRegions();
  if (regionDragIsCurrent()) {
    if (drag.kind === 'create') {
      const r = regionDragBounds(drag);
      regions.push({ x: r.x * P.media.width, y: r.y * P.media.height, w: r.w * P.media.width,
        h: r.h * P.media.height, start: 0, end: P.media.duration });
    } else drag.indices.forEach((index, n) => regions[index] = {...drag.nextRows[n]});
  }
  return { id: P.id, revision: P.source_revision ?? null, width: P.media.width, height: P.media.height,
    params, regions, source: $("#viewSource").value === "source", interacting: !!drag || cleanSliderHeld };
}
const cleanLive = new NpkCleanLive({ video, canvas: $("#cleanLiveCanvas"), frame: $("#cleanLiveFrame"),
  bar: $("#cleanLiveBar"), status: $("#cleanLiveStatus"), button: $("#cleanLiveToggle"), snapshot: cleanLiveSnapshot, token });
function beginCleanEdit() {
  if (!P || area !== "clean") return;
  cleanLive.enabled = true;
  if ($("#viewSource").value !== "source") {
    $("#viewSource").value = "source";
    $("#viewSource").onchange();
  }
  cleanLive.refresh();
}
panel.addEventListener("input", e => {
  if (area !== "clean" || !e.target.matches(".param,.region-input")) return;
  beginCleanEdit();
  if (e.target.matches(".region-input")) drawRegions();
});
panel.addEventListener("pointerdown", e => {
  if (area === "clean" && e.target.matches('.param[type="range"]')) { cleanSliderHeld = true; beginCleanEdit(); }
});
for (const name of ["pointerup", "pointercancel", "blur"]) window.addEventListener(name, () => {
  cleanSliderHeld = false; cleanLive.refresh();
});
document.addEventListener("visibilitychange", () => cleanLive.refresh());
function remember() {
  undo.push(JSON.stringify(P));
  if (undo.length > 25) undo.shift();
  redo = [];
}
function activeProjectPipeline() {
  return P && jobs.find(job => job.project_id === P.id && ["pipeline", "render", "auto_subtitles"].includes(job.kind) && ["queued", "running"].includes(job.state));
}
function refreshProjectLock() {
  const locked = Boolean(activeProjectPipeline());
  const editingPanel = ["setup", "subtitles", "clean", "voice", "export"].includes(area);
  const existing = panel.querySelector("#pipelineEditLock");
  if (locked && editingPanel && !existing) {
    const status = document.createElement("div");
    status.id = "pipelineEditLock"; status.className = "info"; status.setAttribute("role", "status");
    status.textContent = "Đang xử lý video. Cấu hình được giữ nguyên; bạn vẫn có thể xem video, chuyển dự án hoặc hủy trong Tiến trình.";
    panel.prepend(status);
  } else if ((!locked || !editingPanel) && existing) existing.remove();
  panel.querySelectorAll("input,textarea,select,button").forEach(control => {
    // Tabs and audition/navigation controls only change the view.
    const readonly = control.matches("[data-tab],[data-settings-tab],[data-audition],#voicesPrev,#voicesNext,#cuePrev,#cueNext,#recognitionDetails");
    if (locked && editingPanel && !readonly) {
      if (control._npkPipelineDisabled === undefined) control._npkPipelineDisabled = control.disabled;
      control.disabled = true;
    } else if (control._npkPipelineDisabled !== undefined) {
      control.disabled = control._npkPipelineDisabled; delete control._npkPipelineDisabled;
    }
  });
  updateUndo();
}
new MutationObserver(() => refreshProjectLock()).observe(panel, {childList: true, subtree: true});
function change(fn, { render = false, history = true } = {}) {
  if (!P) return;
  if (activeProjectPipeline()) {
    notice("Chờ quy trình hoàn tất hoặc hủy trong Tiến trình trước khi sửa cấu hình video.", true);
    return false;
  }
  if (history) remember();
  fn(P);
  dirty = true;
  editVersion++;
  quality = null;
  clearTimeout(saveTimer);
  $("#saved").textContent = "Đang chỉnh sửa";
  saveTimer = setTimeout(guard(save), 650);
  drawCaption();
  // Keep the paused viewer in sync too: deletion and undo/redo may never
  // trigger a video timeupdate or resize event to refresh the region overlay.
  drawRegions();
  updateSpeechSourceHints();
  if (area === "voice") guard(updateVoiceSpeechState)();
  if (area === "clean") markCleanPreviewStale();
  updateSubtitleSourceStatus();
  if (render) renderPanel();
  updateUndo();
}
function updateUndo() {
  $("#undo").disabled = !!activeProjectPipeline() || !undo.length;
  $("#redo").disabled = !!activeProjectPipeline() || !redo.length;
}
async function save() {
  clearTimeout(saveTimer);
  if (saving) {
    await saving;
    if (dirty) return save();
    return;
  }
  if (!dirty || !P) return;
  if (conflict)
    throw new Error(
      "Dự án đã đổi ở nơi khác. Mở Dự án và tải lại; chưa ghi đè bản mới.",
    );
  const id = P.id,
    version = editVersion,
    payload = {
      expected_revision: P.updated,
      name: P.name,
      params: P.params,
      segments: P.segments,
      regions: P.regions,
      ...(P.extraction_regions
        ? { extraction_regions: P.extraction_regions }
        : {}),
    };
  $("#saved").textContent = "Đang lưu…";
  saving = (async () => {
    try {
      const result = await api("/api/projects/" + id, payload, "PUT");
      if (P?.id === id) {
        P.updated = result.updated;
        if (version === editVersion) {
          P.params = result.params;
          P.segments = result.segments;
          dirty = false;
          drawCaption();
        }
        $("#saved").textContent = dirty ? "Có thay đổi mới" : "Đã lưu";
      }
    } catch (e) {
      if (e.status === 409) conflict = true;
      $("#saved").textContent = "Chưa lưu được";
      throw e;
    } finally {
      saving = null;
    }
  })();
  await saving;
  if (area === "voice") guard(updateVoiceSpeechState)();
  if (dirty) saveTimer = setTimeout(guard(save), 500);
}
async function loadProject(id, force = false) {
  captionPreviewEnabled = false;
  viewerLoadRevision++;
  cleanLive.reset();
  cancelRegionDrag();
  regionSelection.clear(); closeRegionMenu();
  drawMode = false;
  drawRegions();
  if (dirty && !force) await save();
  stopMix();
  P = await api("/api/studio/projects/" + id);
  dirty = false;
  conflict = false;
  editVersion = 0;
  undo = [];
  redo = [];
  selected.clear();
  quality = null;
  page = 0;
  query = "";
  currentCue = -1;
  $("#saved").textContent = "Đã lưu";
  storePreference("npk-current", id);
  setMedia();
  renderPanel();
  updateUndo();
}
function finalArtifactCandidates() {
  return Object.values(P?.artifacts || {}).filter(artifact=>artifact?.job_id&&artifact.primary_file==='final.mp4');
}
function rememberFinalJob(job) {
  // A new final can exist without being auto-applied after concurrent edits.
  // Ask the shared server resolver rather than deciding from its timestamp.
  const known=finalResolutions.get(job.project_id);
  if(known?.current?.job_id!==job.id&&known?.historical?.job_id!==job.id)finalResolutions.delete(job.project_id);
}
function latestFinalArtifact() {
  const resolved=finalResolutions.get(P?.id);
  if(!resolved||(resolved.source_revision??null)!==(P?.source_revision??null))return null;
  const item=resolved.current||resolved.historical;
  if(!item)return null;
  const current=item.current&&resolved.expected_revision===P.updated&&!dirty;
  return {...item.artifact,job_id:item.job_id,primary_file:'final.mp4',_final_current:current,
    _final_reason:current?'':item.reason||'Đây là bản xuất trước khi cập nhật cấu hình hiện tại.'};
}
function requestFinalArtifacts(preferred, loadRevision) {
  const projectId=P?.id,sourceRevision=P?.source_revision??null,revision=P?.updated;
  const cached=finalResolutions.get(projectId);
  if(cached?.expected_revision===revision&&(cached.source_revision??null)===sourceRevision)return;
  const key=projectId+':'+revision;
  if(!finalResultRequests.has(key))finalResultRequests.set(key,api('/api/studio/projects/'+projectId+'/final-result')
    .then(result=>{
      if(P?.id===projectId&&P.updated===result.expected_revision&&(P.source_revision??null)===sourceRevision)
        finalResolutions.set(projectId,{...result,source_revision:sourceRevision});
    }).catch(()=>{}).finally(()=>finalResultRequests.delete(key)));
  finalResultRequests.get(key).then(()=>{
    if(P?.id!==projectId||P.updated!==revision||(P?.source_revision??null)!==sourceRevision)return;
    const artifact=latestFinalArtifact(),select=$('#viewSource');
    if(artifact&&!select.querySelector('option[value="final"]'))select.insertAdjacentHTML('beforeend','<option value="final"></option>');
    if(artifact)select.querySelector('option[value="final"]').textContent=artifact._final_current?'Video thành phẩm':'Video thành phẩm · lượt trước';
    if(!artifact){
      const wasFinal=select.value==='final';select.querySelector('option[value="final"]')?.remove();
      if(wasFinal){select.value='source';select.onchange();}
    }else if((preferred==='final'&&viewerLoadRevision===loadRevision)||(select.value==='final'&&!video.src.includes('/media/job/'+artifact.job_id+'/'))){
      select.value='final';select.onchange();
    }
    updateCaptionPreviewControls();
  });
}
function setMedia(preferred = "source") {
  if (!P) return;
  viewerLoadRevision++;
  cleanLive.reset();
  $("#replaceSource").disabled = false;
  $("#projectName").textContent = P.name;
  $("#projectsTop").textContent = P.name;
  $("#sourceMeta").textContent =
    `${clock(P.media.duration)} · ${P.media.width} × ${P.media.height}`;
  $("#empty").hidden = true;
  $("#scrub").max = P.media.duration;
  $("#viewSource").innerHTML = '<option value="source">Video nguồn</option>';
  const a = P.artifacts || {};
  const finalArtifact=latestFinalArtifact();
  if (a.clean)
    $("#viewSource").insertAdjacentHTML(
      "beforeend",
      '<option value="clean">Video đã xử lý chữ</option>',
    );
  if (finalArtifact)
    $("#viewSource").insertAdjacentHTML(
      "beforeend",
      `<option value="final">${finalArtifact._final_current?'Video thành phẩm':'Video thành phẩm · lượt trước'}</option>`,
    );
  const choice = preferred === "clean" && a.clean ? "clean" : preferred === "final" && finalArtifact ? "final" : "source";
  $("#viewSource").value = choice;
  if (choice === "final") captionPreviewEnabled = false;
  const artifact = choice === "clean" ? a.clean : finalArtifact;
  video.src = choice === "source" ? sourceMediaUrl() : mediaUrl('/media/job/' + artifact.job_id + '/' + (choice === "clean" ? 'clean.mp4' : 'final.mp4'));
  video.load();
  updateCaptionPreviewControls();
  requestFinalArtifacts(preferred,viewerLoadRevision);
  setTimeout(sizePicture, 0);
}
function sourceMediaUrl() {
  // The project ID stays the same after replacement, but the media cache must not.
  return mediaUrl('/media/source/' + P.id) + '&source=' + encodeURIComponent(P.source_revision || 'original');
}
function sizePicture() {
  const s = $("#stage"),
    ratio = (P?.media.width || 16) / (P?.media.height || 9);
  const width = Math.min(s.clientWidth - 2, (s.clientHeight - 2) * ratio);
  $("#picture").style.width = Math.max(0, width) + "px";
  $("#picture").style.height = Math.max(0, width / ratio) + "px";
  drawCaption();
  drawRegions();
}
function captionNormalizedWord(text) {
  // Match the exporter's NFKC/casefold/alphanumeric comparison. Vietnamese
  // punctuation stays visible but does not invalidate an aligned word.
  return String(text).normalize("NFKC").toLowerCase().replace(/ß/g, "ss")
    .replace(/ς/g, "σ").replace(/[^\p{L}\p{N}]/gu, "");
}
function captionWordTimings(row, text) {
  const tokens = String(text).match(/\S+/g) || [], start = Number(row.start), end = Number(row.end);
  if (!tokens.length || !Number.isFinite(start + end) || end <= start) return null;
  const words = Object.hasOwn(row, "caption_words") ? row.caption_words : row.words;
  if (Array.isArray(words) && words.length === tokens.length) {
    let previousStart = -Infinity;
    const matches = words.every((word, index) => {
      if (!word || typeof word !== "object" ||
          captionNormalizedWord(word.word || "") !== captionNormalizedWord(tokens[index])) return false;
      const first = Number(word.start), last = Number(word.end);
      if (!Number.isFinite(first + last) || last <= first || first < previousStart) return false;
      previousStart = first;
      return true;
    });
    if (matches) {
      const timed = words.map((word, index) => ({word: tokens[index],
        start: Math.max(start, Number(word.start)),
        end: Math.min(end, Number(word.end), index + 1 < words.length ? Number(words[index + 1].start) : end)}));
      if (timed.every(word => word.end > word.start)) return {
        words: timed, source: row.word_timing_source === "estimated" ? "estimated" : "aligned",
      };
    }
  }
  // A complete fallback is marked as an estimate, never as audio alignment.
  // The millisecond allocation and stable largest-remainder ties match ASS.
  const total = Math.round((end - start) * 1000);
  if (total < tokens.length) return null;
  const weights = tokens.map(word => Math.max(1, Array.from(captionNormalizedWord(word)).length));
  const weightSum = weights.reduce((sum, value) => sum + value, 0), remaining = total - tokens.length;
  const numerators = weights.map(weight => remaining * weight);
  const durations = numerators.map(value => 1 + Math.floor(value / weightSum));
  const order = durations.map((_, index) => index).sort((a, b) =>
    (numerators[b] % weightSum) - (numerators[a] % weightSum) || a - b);
  const leftover = total - durations.reduce((sum, value) => sum + value, 0);
  for (const index of order.slice(0, leftover)) durations[index]++;
  let cursor = start;
  return {source: "estimated", words: tokens.map((word, index) => {
    const first = cursor; cursor += durations[index] / 1000;
    return {word, start: first, end: index === tokens.length - 1 ? end : cursor};
  })};
}
const captionLayoutCache = {key: "", serial: 0, plan: null, timer: null, pending: false, failedRevision: null, fallbacks: new Map()};
function captionLayoutFingerprint(project = P) {
  if (!project) return "";
  return JSON.stringify([project.id, project.source_revision, project.media.width, project.media.height,
    project.params, project.segments]);
}
function requestCaptionLayout(project = P) {
  if (!project || !project.segments.some(row =>
    (row.subtitle_style?.subtitle_flow || project.params.subtitle_flow || "wrap") === "single")) return null;
  const key = captionLayoutFingerprint(project);
  if (captionLayoutCache.key === key &&
      (captionLayoutCache.plan || captionLayoutCache.timer || captionLayoutCache.pending || captionLayoutCache.failedRevision === project.updated))
    return captionLayoutCache.plan;
  clearTimeout(captionLayoutCache.timer);
  const serial = ++captionLayoutCache.serial, revision = project.updated, id = project.id;
  const payload = {expected_revision: revision, params: structuredClone(project.params), segments: structuredClone(project.segments)};
  captionLayoutCache.key = key; captionLayoutCache.plan = null; captionLayoutCache.pending = false;
  captionLayoutCache.failedRevision = null; captionLayoutCache.fallbacks.clear();
  captionLayoutCache.timer = setTimeout(async () => {
    captionLayoutCache.timer = null;
    captionLayoutCache.pending = true;
    try {
      const plan = await api(`/api/studio/projects/${id}/caption-layout`, payload, "POST", {timeout: 20000});
      if (serial !== captionLayoutCache.serial || P?.id !== id || captionLayoutFingerprint() !== key) return;
      if (plan.project_id !== id || !Array.isArray(plan.cues)) throw new Error("Mẫu bố cục không hợp lệ.");
      captionLayoutCache.plan = plan; drawCaption();
    } catch (error) {
      if (serial !== captionLayoutCache.serial || P?.id !== id || captionLayoutFingerprint() !== key) return;
      captionLayoutCache.failedRevision = revision;
      if (error.status === 409 && P.updated !== revision) { captionLayoutCache.key = ""; drawCaption(); }
    } finally { if (serial === captionLayoutCache.serial) captionLayoutCache.pending = false; }
  }, 120);
  return null;
}
const captionMeasureCanvas = document.createElement("canvas");
function estimatedCaptionChunks(row, text, params, media) {
  const timing = captionWordTimings(row, text), tokens = String(text).match(/\S+/g) || [];
  if (!tokens.length) return [];
  let fontSize = Number(params.font_size) || 48;
  const minimumSize = Math.min(fontSize, 8);
  const width = Math.max(1, Number(media.width) - 2 * (Number(params.subtitle_margin_x) || 0)
    - 2 * ((Number(params.outline) || 0) + (Number(params.subtitle_shadow) || 0)) - Math.max(8, fontSize * .2))
    / (params.subtitle_preset === "pop" ? 1.06 : 1);
  const context = captionMeasureCanvas.getContext("2d");
  const font = size => `${params.subtitle_bold ? "700" : "400"} ${size}px "${String(params.font || "Arial").replace(/["\\]/g, "")}"`;
  const literal = text => text.replaceAll("\\", "＼").replaceAll("{", "｛").replaceAll("}", "｝");
  const measure = (words, size = fontSize) => { context.font = font(size); return context.measureText(literal(words.join(" "))).width; };
  const widest = Math.max(...tokens.map(word => measure([word])));
  if (widest > width) fontSize = Math.max(minimumSize, fontSize * width / widest * .98);
  const limit = Math.max(2, Math.min(12, Math.round(Number(params.subtitle_chunk_words) || 6))), pages = [];
  for (let first = 0; first < tokens.length;) {
    let end = first + 1;
    while (end < tokens.length && end - first < limit && measure(tokens.slice(first, end + 1)) <= width) end++;
    for (let boundary = end; boundary >= first + 2; boundary--) {
      if (/[.!?;:,…]$/.test(tokens[boundary - 1]) && tokens.length - boundary >= 2) { end = boundary; break; }
    }
    pages.push({word_start: first, word_end: end}); first = end;
  }
  if (pages.length > 1 && pages.at(-1).word_end - pages.at(-1).word_start === 1) {
    const previous = pages.at(-2), last = pages.at(-1);
    if (previous.word_end - previous.word_start >= 3 && measure(tokens.slice(last.word_start - 1, last.word_end)) <= width) {
      previous.word_end--; last.word_start--;
    }
  }
  return pages.map((page, index) => ({...page, text: tokens.slice(page.word_start, page.word_end).join(" "),
    display_text: literal(tokens.slice(page.word_start, page.word_end).join(" ")),
    font_size: fontSize, start: index === 0 ? Number(row.start) : timing?.words[page.word_start]?.start ?? Number(row.start),
    end: index + 1 < pages.length ? timing?.words[pages[index + 1].word_start]?.start ?? Number(row.end) : Number(row.end),
    caption_words: timing?.words.slice(page.word_start, page.word_end) || [],
    word_timing_source: timing?.source || "estimated", estimated_layout: true,
    layout_overflow: measure(tokens.slice(page.word_start, page.word_end)) > width}));
}
function captionPage(row, text, params, time, rowIndex, request = true) {
  if ((params.subtitle_flow || "wrap") !== "single") return null;
  const plan = request ? requestCaptionLayout() : captionLayoutCache.plan, exact = plan?.cues.find(cue => cue.index === rowIndex);
  if (!exact && !captionLayoutCache.fallbacks.has(rowIndex))
    captionLayoutCache.fallbacks.set(rowIndex, estimatedCaptionChunks(row, text, params, P.media));
  const pages = exact?.chunks || captionLayoutCache.fallbacks.get(rowIndex) || [];
  return pages.find(page => page.start <= time && page.end > time) || pages[0] || null;
}
window.NPKCaptionTiming = {timeline: captionWordTimings, estimatedChunks: estimatedCaptionChunks};
let captionAnimationFrame = 0, captionAnimationMarker = "";
function animateCaptionWords() {
  captionAnimationFrame = 0;
  if (video.paused || video.ended) return;
  const time = video.currentTime || 0, source = $("#viewSource").value;
  if (P && area !== "clean" && source !== "final" &&
      (source !== "clean" || captionPreviewEnabled) && P.params.burn_subtitles) {
    const rowIndex = P.segments.findIndex(row => row.start <= time && row.end > time), row = P.segments[rowIndex];
    const style = {...P.params, ...(row?.subtitle_style || {})};
    if (row && (style.subtitle_preset === "highlight" || style.subtitle_preset === "karaoke" || style.subtitle_karaoke ||
        (style.subtitle_flow || "wrap") === "single")) {
      const text = captionPreviewEnabled || source === "clean" ? row.translated?.trim() : row.translated || row.text;
      const timing = text && captionWordTimings(row, text);
      const active = timing?.words.findIndex(word => time >= word.start && time < word.end) ?? -1;
      const word = timing?.words[active];
      const progress = style.subtitle_preset === "highlight" || !word ? 0
        : Math.round(100 * (time - word.start) / (word.end - word.start));
      const page = text && captionPage(row, text, style, time, rowIndex, false);
      const marker = `${P.id}:${source}:${rowIndex}:${active}:${progress}:${text}:${style.subtitle_preset}:${page?.word_start}:${page?.word_end}`;
      if (marker !== captionAnimationMarker) { captionAnimationMarker = marker; drawCaption(); }
    }
  }
  captionAnimationFrame = requestAnimationFrame(animateCaptionWords);
}
function drawCaption() {
  const el = $("#caption");
  const source = $("#viewSource").value;
  updateCaptionPreviewControls();
  // Final media already contains rendered pixels. Clean media only gains an
  // editable translation overlay after an explicit preview action.
  if (!P || area === "clean" || source === "final" ||
      (source === "clean" && !captionPreviewEnabled) || !P.params.burn_subtitles) {
    el.hidden = true;
    return;
  }
  const time = video.currentTime || 0, rowIndex = P.segments.findIndex(r => r.start <= time && r.end > time),
    row = P.segments[rowIndex];
  let text = row && (captionPreviewEnabled || source === "clean"
    ? row.translated?.trim() : row.translated || row.text);
  el.hidden = !text;
  if (!text) return;
  const p = { ...P.params, ...(row.subtitle_style || {}) },
    k = $("#picture").clientWidth / P.media.width, page = captionPage(row, text, p, time, rowIndex);
  const displayRow = page ? {...row, start: page.start, end: page.end, caption_words: page.caption_words,
    word_timing_source: page.word_timing_source} : row;
  if (page) text = page.display_text || page.text;
  el.textContent = text; delete el.dataset.wordTiming;
  el.dataset.flow = p.subtitle_flow || "wrap";
  if (page) { el.dataset.pageStart = page.word_start; el.dataset.pageEnd = page.word_end; }
  else { delete el.dataset.pageStart; delete el.dataset.pageEnd; }
  el.style.fontFamily = `"${String(p.font).replace(/["\\]/g, "")}", sans-serif`;
  el.style.fontWeight = p.subtitle_bold ? "700" : "400";
  el.style.fontSize = (page?.font_size || p.font_size) * k + "px";
  el.style.color = p.font_color;
  el.style.paddingLeft = el.style.paddingRight = p.subtitle_margin_x * k + "px";
  el.style.textShadow = p.subtitle_background
    ? "none"
    : `0 0 ${Math.max(0.2, p.outline * k)}px ${p.outline_color}, 1px 0 ${p.outline * k}px ${p.outline_color}, -1px 0 ${p.outline * k}px ${p.outline_color}, 0 1px ${p.outline * k}px ${p.outline_color}`;
  el.style.background = p.subtitle_background ? p.outline_color : "transparent";
  el.style.top =
    p.subtitle_alignment === "top"
      ? p.margin * k + "px"
      : p.subtitle_alignment === "middle"
        ? "50%"
        : "auto";
  el.style.bottom =
    p.subtitle_alignment === "bottom" ? p.margin * k + "px" : "auto";
  el.style.transform =
    p.subtitle_alignment === "middle" ? "translateY(-50%)" : "none";
  if (p.subtitle_preset === "highlight" || p.subtitle_preset === "karaoke" || p.subtitle_karaoke) {
    const timing = captionWordTimings(displayRow, text);
    if (timing) {
      el.dataset.wordTiming = timing.source;
      const parts = String(text).match(/\S+|\s+/g) || [], nodes = [];
      let index = 0;
      for (const part of parts) {
        if (/^\s+$/.test(part)) { nodes.push(document.createTextNode(part)); continue; }
        const word = timing.words[index++], span = document.createElement("span");
        span.className = "caption-preview-word"; span.textContent = part;
        if (p.subtitle_preset === "highlight") {
          if (time >= word.start && time < word.end) span.classList.add("caption-preview-highlight");
        } else {
          const progress = Math.max(0, Math.min(1, (time - word.start) / (word.end - word.start)));
          span.classList.add("caption-preview-karaoke");
          span.style.setProperty("--caption-word-progress", `${progress * 100}%`);
          span.style.setProperty("--caption-word-color", p.font_color);
        }
        nodes.push(span);
      }
      el.replaceChildren(...nodes);
      const badge = $("#captionPreviewStatus");
      if (captionPreviewEnabled && badge) {
        badge.textContent = `Xem trước · ${timing.source === "aligned" ? "Căn theo âm thanh" : "Nhịp từ ước lượng"}${page?.estimated_layout ? " · Bố cục tạm" : ""}`;
        badge.title = timing.source === "aligned"
          ? "Mốc từ khớp nội dung đang hiển thị. Xem mẫu như khi xuất để kiểm tra bản dựng chính xác."
          : "Mốc từ chia theo độ dài chữ, chưa xác định từ âm thanh. Cần căn theo lời đọc khi xử lý video.";
      }
    }
  }
  if (page && captionPreviewEnabled && !["highlight", "karaoke"].includes(p.subtitle_preset) && !p.subtitle_karaoke) {
    $("#captionPreviewStatus").textContent = `Xem trước · Một dòng · ${page.word_timing_source === "aligned" ? "Căn theo âm thanh" : "Nhịp từ ước lượng"}${page.estimated_layout ? " · Bố cục tạm" : ""}`;
  }
}
function updateCaptionPreviewControls() {
  const bar = $("#captionPreviewBar"), toggle = $("#captionPreviewToggle"), badge = $("#captionPreviewStatus");
  if (!bar || !toggle || !badge) return;
  bar.hidden = !P || area === "clean";
  const source = $("#viewSource").value, preview = captionPreviewEnabled && source !== "final";
  toggle.setAttribute("aria-pressed", String(preview));
  toggle.disabled = !P || !P.params.burn_subtitles;
  toggle.textContent = preview ? "Ẩn xem trước phụ đề" : "Xem trước phụ đề";
  toggle.title = "Xem bản dịch với kiểu đang chỉnh trên video chưa gắn chữ. Hiệu ứng xuất chính xác: Xem mẫu như khi xuất.";
  badge.dataset.mode = preview ? "preview" : source;
  badge.textContent = preview ? "Xem trước · Bản dịch" : source === "final"
    ? "Thành phẩm đã xuất" : source === "clean" ? "Video đã xử lý chữ" : "Video nguồn";
  badge.title = preview ? "Đang xem bản dịch chỉnh sửa. Video thành phẩm chỉ cập nhật sau khi xuất lại; nền sạch là lần xử lý đã lưu."
    : source === "final" ? "Chữ đã gắn vào video. Bấm Xem trước phụ đề để chỉnh trên nền chưa gắn chữ, rồi xuất lại."
      : "Chọn Xem trước phụ đề để hiển thị bản dịch đang chỉnh.";
}
function previewStyle() {
  if (!P || area === "clean") return;
  captionPreviewEnabled = true;
  const target = P.artifacts?.clean ? "clean" : "source";
  if ($("#viewSource").value !== target) {
    $("#viewSource").value = target;
    $("#viewSource").onchange();
  }
  drawCaption();
}
$("#captionPreviewToggle").onclick = () => {
  if (!P) return;
  if (captionPreviewEnabled) { captionPreviewEnabled = false; drawCaption(); }
  else previewStyle();
};
function drawRegions() {
  if (drag && !regionDragIsCurrent()) cancelRegionDrag(false);
  syncRegionSelection();
  cleanLive.refresh();
  const el = $("#regions");
  el.classList.toggle("draw", drawMode);
  el.classList.toggle("editable", !!P && ["clean", "subtitles"].includes(area));
  el.innerHTML = "";
  if (!P || !["clean", "subtitles"].includes(area)) return;
  if (area === 'subtitles' && !drawMode) {
    const task = recognitionWork();
    const method = pendingRecognition.get(P.id) || (task ? recognitionMethod(task) : P.coverage?.method);
    if (method === 'asr') { el.classList.remove('editable'); return; }
  }
  const { width, height } = P.media;
  const rows = displayedRegionRows();
  rows.forEach((r, i) => {
    if (r.start <= video.currentTime && r.end >= video.currentTime) {
      const b = document.createElement("div");
      const chosen = regionSelection.has(i);
      b.className = "region" + (chosen ? " selected" : "") + (drag?.indices?.includes(i) ? ' editing' : '');
      b.dataset.regionIndex = String(i);
      b.title=`Vùng ${i+1} · Kéo để di chuyển; Ctrl hoặc Shift chọn nhiều; chuột phải để thao tác`;
      b.setAttribute('aria-label', `Vùng ${i+1}, ${Math.round(r.w)} × ${Math.round(r.h)}`);
      b.setAttribute('aria-selected', String(chosen));
      Object.assign(b.style, {
        left: (r.x / width) * 100 + "%",
        top: (r.y / height) * 100 + "%",
        width: (r.w / width) * 100 + "%",
        height: (r.h / height) * 100 + "%",
      });
      b.innerHTML = `<span class="region-label">${i + 1}</span>`;
      if (chosen && !drawMode) for (const handle of ['nw','n','ne','e','se','s','sw','w']) {
        const names={nw:'góc trên trái',n:'cạnh trên',ne:'góc trên phải',e:'cạnh phải',se:'góc dưới phải',s:'cạnh dưới',sw:'góc dưới trái',w:'cạnh trái'};
        const knob = document.createElement('button');
        knob.type = 'button'; knob.className = 'region-handle'; knob.dataset.handle = handle;
        knob.tabIndex = -1;
        knob.title=`Kéo ${names[handle]}${handle.length===2?' · Giữ Shift để giữ tỷ lệ':''}`;
        knob.setAttribute('aria-label', `Đổi kích thước ${names[handle]} vùng ${i+1}`);
        b.append(knob);
      }
      el.append(b);
    }
  });
  updateRegionDraft();
}
let drag = null, regionSelection = new Set(), regionSelectionKey = '';
const regionGeometry = NpkRegionGeometry;
function regionRows(project = P, mode = area) {
  return project ? (mode === 'subtitles' ? project.extraction_regions ?? project.regions : project.regions) : [];
}
function editableRegionRows(project, mode = area) {
  return mode === 'subtitles' ? (project.extraction_regions ??= structuredClone(project.regions)) : project.regions;
}
function syncRegionSelection() {
  const key = `${P?.id || ''}:${P?.source_revision || ''}:${area}`;
  if (regionSelectionKey !== key) { regionSelection.clear(); regionSelectionKey = key; closeRegionMenu(); }
  regionSelection = new Set([...regionSelection].filter(i => Number.isInteger(i) && i >= 0 && i < regionRows().length));
}
function selectedRegionIndices() {
  syncRegionSelection();
  return [...regionSelection].sort((a,b) => a-b);
}
function displayedRegionRows() {
  const rows = area==='clean' ? cleanDraftRegions() : structuredClone(regionRows());
  if (regionDragIsCurrent() && drag.kind !== 'create') drag.indices.forEach((i,n) => rows[i] = {...drag.nextRows[n]});
  return rows;
}
function selectRegion(index, additive = false, {seek: seekRegion = false} = {}) {
  cancelRegionDrag(false); closeRegionMenu(); syncRegionSelection();
  if (!regionRows()[index]) return;
  if (additive) regionSelection.has(index) ? regionSelection.delete(index) : regionSelection.add(index);
  else regionSelection = new Set([index]);
  if (seekRegion && (video.currentTime < regionRows()[index].start || video.currentTime > regionRows()[index].end)) {
    video.pause(); seek(regionRows()[index].start);
  }
  drawRegions(); updateRegionInspector();
}
function startRegionDrawing() {
  if (activeProjectPipeline()) return notice("Chờ quy trình hoàn tất hoặc hủy trước khi chỉnh vùng.", true);
  if (!P || !['clean','subtitles'].includes(area)) return;
  cancelRegionDrag(false); closeRegionMenu();
  if (area === 'clean') beginCleanEdit();
  video.pause(); drawMode = !drawMode;
  drawRegions(); updateRegionInspector(); $('#regions').focus({preventScroll:true});
}
function regionDragIsCurrent() {
  return drag && P === drag.project && P?.id === drag.projectId &&
    P.source_revision === drag.sourceRevision && area === drag.area &&
    editVersion === drag.editVersion && JSON.stringify(regionRows()) === drag.rowsFingerprint &&
    (drag.kind !== 'create' || drawMode);
}
function regionPoint(e) {
  const rect = $("#regions").getBoundingClientRect();
  if (rect.width <= 0 || rect.height <= 0) return null;
  return {
    x: Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width)),
    y: Math.min(1, Math.max(0, (e.clientY - rect.top) / rect.height)),
    rect,
  };
}
function regionDragBounds(d) {
  return {
    x: Math.min(d.start.x, d.end.x), y: Math.min(d.start.y, d.end.y),
    w: Math.abs(d.end.x - d.start.x), h: Math.abs(d.end.y - d.start.y),
  };
}
function updateRegionDraft() {
  if (!regionDragIsCurrent()) return;
  cleanLive.refresh();
  if (drag.kind !== 'create') return;
  const r = regionDragBounds(drag);
  Object.assign(drag.preview.style, {
    left: r.x * 100 + "%", top: r.y * 100 + "%",
    width: r.w * 100 + "%", height: r.h * 100 + "%",
  });
  drag.preview.firstChild.textContent =
    `${Math.round(r.w * P.media.width)} × ${Math.round(r.h * P.media.height)}`;
  // timeupdate/resize redraw saved regions too; keep the unsaved draft visible.
  if (!drag.preview.isConnected) $("#regions").append(drag.preview);
}
function cancelRegionDrag(redraw = true) {
  if (!drag) return;
  const d = drag;
  drag = null;
  cleanLive.refresh();
  d.preview?.remove();
  const el = $("#regions");
  if (el.hasPointerCapture(d.pointerId)) el.releasePointerCapture(d.pointerId);
  if (redraw) { drawRegions(); updateRegionInspector(); }
}
$("#regions").onpointerdown = (e) => {
  if (!P || activeProjectPipeline() || drag || !["clean", "subtitles"].includes(area) ||
      e.button !== 0 || !e.isPrimary) return;
  const point = regionPoint(e);
  if (!point) return;
  const target = e.target.closest('.region[data-region-index]'), handle = e.target.closest('[data-handle]')?.dataset.handle;
  // Commit or reject a focused numeric draft before clearing selection or taking a gesture snapshot.
  e.currentTarget.focus({preventScroll:true});
  if (!drawMode && !target) {
    regionSelection.clear(); closeRegionMenu(); drawRegions(); updateRegionInspector();
    e.currentTarget.focus({preventScroll:true}); return;
  }
  e.preventDefault();
  closeRegionMenu(); video.pause();
  if (area === "clean") beginCleanEdit();
  let preview = null, kind = 'create', indices = [];
  if (!drawMode) {
    const index = Number(target.dataset.regionIndex);
    if(!regionRows()[index])return;
    if (!handle && (e.ctrlKey || e.metaKey || e.shiftKey)) {
      selectRegion(index,true); $('#regions').focus({preventScroll:true}); return;
    }
    if (!regionSelection.has(index)) regionSelection = new Set([index]);
    if (handle) { kind = 'resize'; indices = [index]; regionSelection = new Set([index]); }
    else { kind = 'move'; indices = selectedRegionIndices(); }
  } else {
    if (regionRows().length>=500) return notice('Tối đa 500 vùng.',true);
    preview = document.createElement('div'); preview.id = 'regionDraft'; preview.className = 'region region-draft';
    preview.setAttribute('aria-hidden','true'); preview.append(document.createElement('span'));
  }
  drag = {
    start: point, end: point, preview, pointerId: e.pointerId, kind, handle, indices,
    originalRows: indices.map(i => structuredClone(regionRows()[i])),
    nextRows: indices.map(i => structuredClone(regionRows()[i])),
    project: P, projectId: P.id, sourceRevision: P.source_revision, area, editVersion,
    rowsFingerprint: JSON.stringify(regionRows()),
  };
  e.currentTarget.focus({preventScroll:true});
  e.currentTarget.setPointerCapture(e.pointerId);
  drawRegions(); updateRegionInspector();
};
function updateRegionGesture(point, event) {
  drag.end = point;
  if (drag.kind === 'move') drag.nextRows = regionGeometry.move(drag.originalRows,
    (point.x-drag.start.x)*P.media.width,(point.y-drag.start.y)*P.media.height,P.media);
  else if (drag.kind === 'resize') drag.nextRows = [regionGeometry.resize(drag.originalRows[0],drag.handle,
    (point.x-drag.start.x)*P.media.width,(point.y-drag.start.y)*P.media.height,P.media,event.shiftKey)];
}
$("#regions").onpointermove = (e) => {
  if (!drag || e.pointerId !== drag.pointerId) return;
  if (!regionDragIsCurrent() || !(e.buttons & 1)) return cancelRegionDrag();
  const point = regionPoint(e);
  if (!point) return cancelRegionDrag();
  e.preventDefault();
  updateRegionGesture(point,e);
  // Update the transient region and effect, without project/history/save writes.
  if (drag.kind === 'create') updateRegionDraft(); else drawRegions();
  updateRegionInspector();
};
$("#regions").onpointerup = (e) => {
  if (!drag || e.pointerId !== drag.pointerId) return;
  if (!regionDragIsCurrent()) return cancelRegionDrag();
  const point = regionPoint(e);
  if (!point) return cancelRegionDrag();
  e.preventDefault();
  const d = drag;
  updateRegionGesture(point,e);
  const r = regionDragBounds(d);
  cancelRegionDrag(false);
  if (d.kind !== 'create') {
    if (JSON.stringify(d.nextRows) !== JSON.stringify(d.originalRows)) {
      change(p => { const rows = editableRegionRows(p,d.area); d.indices.forEach((i,n) => rows[i] = d.nextRows[n]); }, {render:true});
    } else { drawRegions(); updateRegionInspector(); }
    return;
  }
  if (r.w * point.rect.width < 3 || r.h * point.rect.height < 3) { drawRegions(); return; }
  drawMode = false;
  regionSelection = new Set([regionRows().length]);
  change(
    (p) =>
      (d.area === "subtitles"
        ? (p.extraction_regions ??= structuredClone(readingRegions()))
        : p.regions
      ).push(regionGeometry.clamp({
        x: r.x * p.media.width, y: r.y * p.media.height,
        w: r.w * p.media.width, h: r.h * p.media.height,
        start: 0,
        end: p.media.duration,
      },p.media)),
    { render: true },
  );
  drawRegions();
  updateRegionInspector();
  notice(
    d.area === "subtitles"
      ? "Đã thêm vùng đọc phụ đề riêng; không thay vùng che chữ."
      : "Đã thêm vùng xử lý chữ.",
  );
};
$("#regions").onpointercancel = $("#regions").onlostpointercapture = (e) => {
  if (drag?.pointerId === e.pointerId) cancelRegionDrag();
};
window.addEventListener("blur", cancelRegionDrag);
function closeRegionMenu() { const menu = $('#regionContextMenu'); if (menu) menu.hidden = true; }
function openRegionMenu(event) {
  if (!P || !['clean','subtitles'].includes(area)) return;
  event.preventDefault(); cancelRegionDrag(false);
  const target = event.target.closest('.region[data-region-index]');
  $('#regions').focus({preventScroll:true});
  if (target && !regionSelection.has(+target.dataset.regionIndex)) selectRegion(+target.dataset.regionIndex);
  const count = selectedRegionIndices().length;
  let menu = $('#regionContextMenu');
  if (!menu) { menu = document.createElement('div'); menu.id = 'regionContextMenu'; menu.setAttribute('role','menu'); document.body.append(menu); }
  const item = (command,label,key='',disabled=false) => `<button type="button" role="menuitem" data-region-command="${command}" ${disabled?'disabled':''} ${command==='delete'?'class="danger"':''}><span>${label}</span><small>${key}</small></button>`;
  menu.innerHTML = `<span class="menu-heading">${count ? `${count} vùng được chọn` : 'Vùng trong video'}</span>`+
    item('add','Thêm vùng','N')+item('edit','Hiệu chỉnh','',count!==1)+item('duplicate','Nhân bản','Ctrl+D',!count)+
    item('merge','Gộp vùng','',count<2)+item('selectAll','Chọn tất cả','Ctrl+A',!regionRows().length)+
    '<hr>'+item('startHere','Bắt đầu tại vị trí này','',!count)+item('endHere','Kết thúc tại vị trí này','',!count)+
    item('fullTime','Áp dụng toàn video','',!count)+'<hr>'+item('delete','Xóa vùng','Delete',!count);
  menu.hidden = false;
  menu.style.left = Math.max(8,Math.min(event.clientX,innerWidth-menu.offsetWidth-8))+'px';
  menu.style.top = Math.max(8,Math.min(event.clientY,innerHeight-menu.offsetHeight-8))+'px';
  const project=P, mode=area, revision=P.source_revision, fingerprint=JSON.stringify(regionRows());
  menu.querySelectorAll('button').forEach(button => button.onclick = guard(() => {
    if(P!==project||area!==mode||P.source_revision!==revision||JSON.stringify(regionRows())!==fingerprint) { closeRegionMenu(); return notice('Các vùng đã thay đổi; chọn lại vùng cần chỉnh.',true); }
    return regionCommand(button.dataset.regionCommand);
  }));
  menu.onkeydown=event=>{
    const buttons=[...menu.querySelectorAll('button:not(:disabled)')], index=buttons.indexOf(document.activeElement);
    if(['ArrowDown','ArrowUp','ArrowLeft','ArrowRight','Home','End','Escape','Tab'].includes(event.key)){
      event.stopPropagation();
      event.preventDefault();
      if(event.key==='Escape'||event.key==='Tab'){closeRegionMenu();$('#regions').focus({preventScroll:true});return;}
      if(['ArrowLeft','ArrowRight'].includes(event.key))return;
      const next=event.key==='Home'?0:event.key==='End'?buttons.length-1:(index+(event.key==='ArrowDown'?1:-1)+buttons.length)%buttons.length;
      buttons[next]?.focus({preventScroll:true});
    }
  };
  drawRegions(); updateRegionInspector(); menu.querySelector('button:not(:disabled)')?.focus({preventScroll:true});
}
$('#regions').oncontextmenu = openRegionMenu;
document.addEventListener('pointerdown',event => { if (!event.target.closest('#regionContextMenu')) closeRegionMenu(); });
document.addEventListener('scroll',closeRegionMenu,true);
window.addEventListener('resize',closeRegionMenu);
function regionCommand(command) {
  if (!P || !['clean','subtitles'].includes(area)) return;
  cancelRegionDrag(false); closeRegionMenu();
  const indices = selectedRegionIndices(), mode = area;
  if (command==='add') return startRegionDrawing();
  if (command==='selectAll') { regionSelection=new Set(regionRows().map((_,i)=>i)); drawRegions(); updateRegionInspector(); return; }
  if (!indices.length) return;
  if (command==='edit') {
    if (area==='clean') { updateRegionInspector(); const details=$('#regionCoordinates'); if(details)details.open=true; $('#regionInspector').scrollIntoView({block:'nearest'}); $('#regionCoordinates input')?.focus(); }
    else {
      const row=regionRows()[indices[0]], project=P, revision=P.source_revision, fingerprint=JSON.stringify(regionRows());
      openModal('Hiệu chỉnh vùng đọc',`<div class="two">${[['x','X'],['y','Y'],['w','Rộng'],['h','Cao'],['start','Bắt đầu (giây)'],['end','Kết thúc (giây)']].map(([key,label])=>`<label>${label}<input type="number" data-edit-reading="${key}" step="${['start','end'].includes(key)?'.01':'.1'}" value="${Number(row[key].toFixed(2))}"></label>`).join('')}</div>`,[{label:'Hủy',run:()=>closeModal()},{label:'Lưu',primary:true,run:()=>{
        if(P!==project||area!==mode||P.source_revision!==revision||JSON.stringify(regionRows())!==fingerprint)throw new Error('Các vùng đã thay đổi; mở lại Hiệu chỉnh để tiếp tục.');
        const value={...row}; $$('[data-edit-reading]').forEach(input=>{if(input.value==='')throw new Error('Nhập đủ thông số vùng.');if(input.value!==input.defaultValue)value[input.dataset.editReading]=Number(input.value);});
        validateRegionEdit(value); if(JSON.stringify(value)!==JSON.stringify(row))change(p=>editableRegionRows(p,mode)[indices[0]]=regionGeometry.clamp(value,p.media),{render:true});closeModal();
      }}]);
    }
    return;
  }
  if (area==='clean') beginCleanEdit();
  if (command==='delete') {
    const selectedSet=new Set(indices); change(p=>{const rows=editableRegionRows(p,mode);for(let i=rows.length-1;i>=0;i--)if(selectedSet.has(i))rows.splice(i,1);regionSelection.clear();},{render:true});
  } else if (command==='duplicate') {
    if(regionRows().length+indices.length>500)return notice('Tối đa 500 vùng.',true);
    const copies=regionGeometry.move(indices.map(i=>structuredClone(regionRows()[i])),16,16,P.media), start=regionRows().length;
    regionSelection=new Set(copies.map((_,n)=>start+n));change(p=>editableRegionRows(p,mode).push(...copies),{render:true});
  } else if (command==='merge' && indices.length>1) {
    const merged=regionGeometry.union(indices.map(i=>regionRows()[i])), fingerprint=JSON.stringify(regionRows()),project=P;
    const apply=()=>{
      if(P!==project||area!==mode||JSON.stringify(regionRows())!==fingerprint)throw new Error('Các vùng đã thay đổi; chọn lại trước khi gộp.');
      const index=indices[0]; regionSelection=new Set([index]); change(p=>{const rows=editableRegionRows(p,mode);rows[index]=merged;indices.slice(1).reverse().forEach(i=>rows.splice(i,1));},{render:true});
    };
    if (indices.some(i=>regionRows()[i].start!==merged.start||regionRows()[i].end!==merged.end))
      return ask('Gộp các vùng khác thời gian?',`Khung mới bao toàn bộ các vùng đã chọn và hoạt động từ ${clock(merged.start)} đến ${clock(merged.end)}, kể cả khoảng trống ở giữa. Bạn có thể hoàn tác.`,'Gộp vùng').then(ok=>{if(ok)apply();});
    apply();
  } else if (['fullTime','startHere','endHere'].includes(command)) {
    const rows=indices.map(i=>({...regionRows()[i],...(command==='fullTime'?{start:0,end:P.media.duration}:command==='startHere'?{start:video.currentTime}:{end:video.currentTime})}));
    for(const row of rows)validateRegionEdit(row);
    change(p=>{const all=editableRegionRows(p,mode);indices.forEach((i,n)=>all[i]=rows[n]);},{render:true});
  }
  drawRegions(); updateRegionInspector(); $('#regions').focus({preventScroll:true});
}
function validateRegionEdit(row) {
  if (['x','y','w','h','start','end'].some(key=>!Number.isFinite(row[key])))throw new Error('Thông số vùng phải là số hợp lệ.');
  if(row.w<1||row.h<1)throw new Error('Rộng và cao tối thiểu 1 điểm ảnh.');
  if(row.start<0||row.end>P.media.duration||row.end<=row.start)throw new Error('Thời gian vùng phải nằm trong video; kết thúc sau bắt đầu.');
}
function updateRegionInspector() {
  const inspector=$('#regionInspector'), indices=selectedRegionIndices();
  $('#drawRegion')?.classList.toggle('active',drawMode);
  $('#drawRegion')?.setAttribute('aria-pressed',String(drawMode));
  $$('#regionList [data-select-region]').forEach(button=>{
    const index=Number(button.dataset.selectRegion), chosen=regionSelection.has(index),r=displayedRegionRows()[index];
    button.classList.toggle('selected',chosen);button.setAttribute('aria-pressed',String(chosen));
    if(r){const size=button.querySelector('.clean-region-name small');if(size)size.textContent=`${Math.round(r.w)} × ${Math.round(r.h)}`;const time=button.querySelector('.clean-region-time');if(time)time.textContent=`${clock(r.start)}–${clock(r.end)}`;}
  });
  if($('#regionSelectionCount'))$('#regionSelectionCount').textContent=indices.length?`${indices.length} / ${regionRows().length}`:String(regionRows().length);
  $$('#panel [data-region-command]').forEach(button=>{const command=button.dataset.regionCommand;button.disabled=command==='merge'?indices.length<2:command==='selectAll'?!regionRows().length:!indices.length;});
  if(!inspector)return;
  if(!indices.length){inspector.innerHTML='';inspector.hidden=true;return;}
  inspector.hidden=false;
  if(indices.length!==1){inspector.innerHTML=`<div class="region-inspector-heading"><strong>${indices.length} vùng đang chọn</strong><small>Di chuyển cùng nhau</small></div>`;return;}
  const index=indices[0], row=displayedRegionRows()[index], key=`${P.id}:${index}`;
  const focused=inspector.contains(document.activeElement), same=inspector.dataset.selection===key;
  if(focused&&same&&!drag)return;
  const opened=same&&$('#regionCoordinates')?.open;
  inspector.dataset.selection=key;
  const input=(property,label)=>{const display=Number(row[property].toFixed(['start','end'].includes(property)?2:1));return `<label>${label}<input class="region-input" aria-label="${label} vùng ${index+1}" type="number" data-i="${index}" data-key="${property}" data-original-display="${display}" step="${['start','end'].includes(property)?'.01':'.1'}" value="${display}"></label>`;};
  inspector.innerHTML=`<div class="region-inspector-heading"><strong>Vùng ${index+1}</strong><small>${Math.round(row.w)} × ${Math.round(row.h)} px</small></div><div class="region-timing">${input('start','Bắt đầu (giây)')}${input('end','Kết thúc (giây)')}</div><div class="row region-time-actions"><button data-region-command="startHere" title="Đặt mốc bắt đầu bằng vị trí phát hiện tại">Từ đây</button><button data-region-command="endHere" title="Đặt mốc kết thúc bằng vị trí phát hiện tại">Đến đây</button><button data-region-command="fullTime">Toàn video</button></div><details id="regionCoordinates" ${opened?'open':''}><summary>Tọa độ chính xác</summary><div class="region-coordinates">${input('x','X')}${input('y','Y')}${input('w','Rộng')}${input('h','Cao')}</div></details>`;
  inspector.querySelectorAll('[data-region-command]').forEach(button=>button.onclick=guard(()=>regionCommand(button.dataset.regionCommand)));
  const project=P, mode=area, revision=P.source_revision, fingerprint=JSON.stringify(regionRows());
  inspector.querySelectorAll('.region-input').forEach(input=>input.onchange=()=>{
    try {
      if(P!==project||area!==mode||P.source_revision!==revision||JSON.stringify(regionRows())!==fingerprint)throw new Error('Các vùng đã thay đổi; chọn lại vùng cần chỉnh.');
      if(input.value===input.dataset.originalDisplay)return;
      const value=Number(input.value);if(input.value===''||!Number.isFinite(value))throw new Error('Nhập số hợp lệ; vùng chưa được thay đổi.');
      const current=regionRows()[index], next={...current,[input.dataset.key]:value}; validateRegionEdit(next);
      change(p=>editableRegionRows(p,mode)[index]=regionGeometry.clamp(next,p.media));input.blur();updateRegionInspector();
    } catch(error) { input.value=input.dataset.originalDisplay;input.blur();updateRegionInspector();cleanLive.refresh();notice(error.message,true); }
  });
}
function handleRegionKey(event) {
  if (activeProjectPipeline()) return false;
  if(!P||!['clean','subtitles'].includes(area)||$('#modal').open||/INPUT|TEXTAREA|SELECT/.test(event.target.tagName)||event.target.isContentEditable)return false;
  if(event.key==='Escape') {
    if(drag||drawMode){cancelRegionDrag(false);drawMode=false;drawRegions();updateRegionInspector();closeRegionMenu();return true;}
    if($('#regionContextMenu')&&!$('#regionContextMenu').hidden){closeRegionMenu();$('#regions').focus({preventScroll:true});return true;}
  }
  if(event.key.toLowerCase()==='n'&&!event.ctrlKey&&!event.metaKey&&area==='clean'){startRegionDrawing();return true;}
  const regionFocused=event.target===$('#regions')||event.target.closest('#regionInspector,#regionList,#regionContextMenu,[data-region-command]');
  if(!regionFocused)return false;
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a'){regionCommand('selectAll');return true;}
  if(!selectedRegionIndices().length)return false;
  if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='d'){regionCommand('duplicate');return true;}
  if(event.key==='Delete'||event.key==='Backspace'){regionCommand('delete');return true;}
  const step=event.shiftKey?10:1, vectors={ArrowLeft:[-step,0],ArrowRight:[step,0],ArrowUp:[0,-step],ArrowDown:[0,step]};
  if(vectors[event.key]){
    cancelRegionDrag(false);const indices=selectedRegionIndices(),before=indices.map(i=>regionRows()[i]);
    const rows=regionGeometry.move(before,...vectors[event.key],P.media);
    if(JSON.stringify(before)!==JSON.stringify(rows))change(p=>{const all=editableRegionRows(p);indices.forEach((i,n)=>all[i]=rows[n]);},{render:true});
    updateRegionInspector();$('#regions').focus({preventScroll:true});return true;
  }
  return false;
}
new ResizeObserver(sizePicture).observe($("#stage"));
video.addEventListener("timeupdate", () => {
  if (P && area === "subtitles" && subtab === "content") {
    const active = P.segments.findIndex(
      (r) => r.start <= video.currentTime && video.currentTime < r.end,
    );
    if (active !== currentCue) {
      currentCue = active;
      $$(".cue").forEach((el) =>
        el.classList.toggle("current", +el.dataset.cue === active),
      );
      const editing = document.activeElement?.matches(
        "input,textarea,select,[contenteditable]",
      );
      if (followVideo && active >= 0 && !editing && !dirty && !query) {
        if (page !== Math.floor(active / 40)) {
          page = Math.floor(active / 40);
          renderCueRows();
        }
        const target = $(`[data-cue="${active}"]`);
        if (target) {
          const top =
            target.offsetTop -
            panel.offsetTop -
            ($(".sticky-tools")?.offsetHeight || 0) -
            20;
          panel.scrollTop = Math.max(0, top);
        }
      }
    }
  }
  $("#scrub").value = video.currentTime;
  $("#timecode").textContent =
    `${clock(video.currentTime)} / ${clock(P?.media.duration)}`;
  drawCaption();
  drawRegions();
  syncMix();
});
video.addEventListener("loadedmetadata", () => {
  sizePicture();
  $("#timecode").textContent =
    `${clock(video.currentTime)} / ${clock(P?.media.duration)}`;
});
video.addEventListener("play", () => {
  $("#play").innerHTML = icon("pause");
  $("#play").setAttribute("aria-label", "Tạm dừng video");
  syncMix(true);
  captionAnimationMarker = "";
  if (!captionAnimationFrame) captionAnimationFrame = requestAnimationFrame(animateCaptionWords);
});
video.addEventListener("pause", () => {
  $("#play").innerHTML = icon("play");
  $("#play").setAttribute("aria-label", "Phát video");
  $("#mixVoice").pause();
  $("#mixBgm").pause();
  if (captionAnimationFrame) cancelAnimationFrame(captionAnimationFrame);
  captionAnimationFrame = 0;
  drawCaption();
});
video.addEventListener("error", () => {
  if (P)
    notice(
      "Chưa phát được video này. Nguồn có thể đã chuyển chỗ hoặc định dạng không được khung xem hỗ trợ.",
      true,
    );
});
function seek(value) {
  video.currentTime = Math.max(0, Math.min(P?.media.duration || 0, value));
  drawCaption();
}
$("#play").onclick = guard(async () => {
  if (!P) return;
  if (video.paused) await video.play();
  else video.pause();
});
$("#prev").onclick = () => seek(video.currentTime - 1 / (P?.media.fps || 30));
$("#next").onclick = () => seek(video.currentTime + 1 / (P?.media.fps || 30));
$("#scrub").oninput = (e) => seek(+e.target.value);
$("#mute").onclick = () => {
  video.muted = !video.muted;
  $("#mute").innerHTML = icon(video.muted ? "muted" : "volume");
  $("#mute").setAttribute(
    "aria-label",
    video.muted ? "Bật âm video" : "Tắt âm video",
  );
};
$("#fit").onclick = () => {
  $("#workbench").classList.remove("viewer-expanded");
  sizePicture();
};
$("#fullscreen").onclick = guard(async () => {
  if (document.fullscreenElement) await document.exitFullscreen();
  else await $("#viewer").requestFullscreen();
});
$("#viewerRatio").value = localStorage.getItem("npk-viewer-ratio") || 54;
document.documentElement.style.setProperty(
  "--viewer",
  $("#viewerRatio").value + "%",
);
$("#viewerRatio").oninput = (e) => {
  document.documentElement.style.setProperty("--viewer", e.target.value + "%");
  storePreference("npk-viewer-ratio", e.target.value);
};
$("#undo").onclick = () => {
  if (!undo.length || activeProjectPipeline()) return;
  cancelRegionDrag(false); closeRegionMenu();
  const revision = P.updated;
  redo.push(JSON.stringify(P));
  P = JSON.parse(undo.pop());
  P.updated = revision;
  change(() => {}, { history: false, render: true });
};
$("#redo").onclick = () => {
  if (!redo.length || activeProjectPipeline()) return;
  cancelRegionDrag(false); closeRegionMenu();
  const revision = P.updated;
  undo.push(JSON.stringify(P));
  P = JSON.parse(redo.pop());
  P.updated = revision;
  change(() => {}, { history: false, render: true });
};
function field(key, label, type = "number", options = null) {
  const own =
    area === "subtitles" &&
    subtab === "style" &&
    styleScope === "selected" &&
    selected.size
      ? P.segments[[...selected][0]]?.subtitle_style
      : null;
  const value = own?.[key] ?? P?.params[key] ?? "";
  if (key === "font")
    return `<div class="font-control"><label>${label}<select class="param" data-key="font" id="fontPick" aria-describedby="fontStatus"><option value="${esc(value)}">${esc(value || "Chưa chọn phông chữ")}</option></select></label><small id="fontStatus" role="status">Đang đọc danh sách phông đã cài…</small><button id="retryFonts" class="link" hidden>Thử tải danh sách lại</button></div>`;
  if (type === "check")
    return `<label class="check"><input class="param" data-key="${key}" type="checkbox" ${value ? "checked" : ""}>${label}</label>`;
  const input =
    type === "select"
      ? `<select class="param" data-key="${key}">${options.map(([v, n]) => `<option value="${esc(v)}" ${String(v) === String(value) ? "selected" : ""}>${esc(n)}</option>`).join("")}</select>`
      : type === "textarea"
        ? `<textarea class="param" data-key="${key}">${esc(value)}</textarea>`
        : `<input class="param" data-key="${key}" type="${type}" value="${esc(value)}" ${type === "number" ? 'step="any"' : ""}>`;
  return `<label>${label}${input}</label>`;
}
function bindParams() {
  panel.querySelectorAll(".param").forEach(
    (el) =>
      (el.onchange = () => {
        const key = el.dataset.key,
          value =
            el.type === "checkbox"
              ? el.checked
              : ["number", "range"].includes(el.type)
                ? Number(el.value)
                : el.value;
        if (["number", "range"].includes(el.type) &&
            (el.value.trim() === "" || !Number.isFinite(value))) {
          el.value = P.params[key] ?? "";
          return notice("Nhập một giá trị số hợp lệ.", true);
        }
        if (key === "subtitle_chunk_words" && (!Number.isInteger(value) || value < 2 || value > 12)) {
          el.value = P.params.subtitle_chunk_words || 6;
          return notice("Nhập số nguyên từ 2 đến 12 từ mỗi cụm.", true);
        }
        if (area === "clean" && cleanLiveKeys.has(key)) beginCleanEdit();
        change((p) => applyParam(p, key, value));
        if (key === "subtitle_flow") updateCaptionFlowControls();
        if (key === "tts_text_source") updateSpeechSourceHints();
        if (["tts_timing_mode", "tts_auto_schedule"].includes(key)) updateVoiceTimingHint();
        if (key === "font") updateFontStatus(el);
        if (["voice_id", "burn_subtitles", "use_clean", "method"].includes(key))
          renderPanel();
      }),
  );
}
let fontCatalog = null,
  fontCatalogRequest = null;
async function loadFontPicker() {
  const picker = $("#fontPick");
  if (!picker) return;
  picker.disabled = true;
  try {
    if (!fontCatalog) {
      if (!fontCatalogRequest)
        fontCatalogRequest = api("/api/studio/fonts")
          .then((names) => {
            if (!Array.isArray(names) || !names.length)
              throw new Error("Chưa tìm thấy phông chữ đã cài.");
            fontCatalog = names;
          })
          .finally(() => {
            fontCatalogRequest = null;
          });
      await fontCatalogRequest;
    }
    if (!picker.isConnected) return;
    const value = picker.value;
    const options = fontCatalog.map((name) => [name, name]);
    if (!fontCatalog.includes(value))
      options.unshift([
        value,
        value ? `${value} · chưa tìm thấy trên máy` : "Chọn phông chữ đã cài…",
      ]);
    picker.innerHTML = options
      .map(
        ([v, label]) =>
          `<option value="${esc(v)}" ${v === value ? "selected" : ""} ${fontCatalog.includes(v) ? "" : "disabled"}>${esc(label)}</option>`,
      )
      .join("");
    picker.disabled = false;
    $("#retryFonts").hidden = true;
    updateFontStatus(picker);
  } catch (error) {
    if (!picker.isConnected) return;
    $("#fontStatus").textContent =
      "Không tải được danh sách phông. Phông đang lưu được giữ nguyên.";
    $("#retryFonts").hidden = false;
  }
}
function updateFontStatus(picker) {
  const status = $("#fontStatus");
  if (!status) return;
  const valid = fontCatalog?.includes(picker.value);
  status.textContent = valid
    ? `${fontCatalog.length} phông đã cài · chọn để xem ngay trên video.`
    : picker.value
      ? "Phông này chưa có trên máy. Hãy chọn phông đã cài trước khi xuất."
      : "Chưa có phông được chọn. Chọn trong danh sách bên trên.";
  status.className = valid ? "muted" : "font-warning";
}
function tabs(items, active, attr = "data-tab") {
  return `<div class="tabs">${items.map(([id, name]) => `<button ${attr}="${id}" class="${active === id ? "active" : ""}">${name}</button>`).join("")}</div>`;
}
function setArea(next) {
  closeRegionMenu();
  if (next === "text_to_speech") {
    video.pause();
    stopMix();
    $("#audition").pause();
    previewJob = null;
    previewVoice = null;
    $("#workbench").classList.remove("viewer-expanded");
  } else $("#ttsAudio")?.pause();
  cleanLive.reset();
  cancelRegionDrag();
  drawMode = false;
  area = next;
  if (area === "clean") captionPreviewEnabled = false;
  syncRegionSelection();
  $('#viewerHint').hidden = area === 'clean';
  renderPanel();
  drawCaption();
  drawRegions();
  $$("#nav [data-area]").forEach((b) =>
    b.setAttribute("aria-pressed", b.dataset.area === area),
  );
  $("#panel").scrollTop = 0;
}
$$("#nav [data-area]").forEach(
  (b) => (b.onclick = () => setArea(b.dataset.area)),
);
$("#exportTop").onclick = () => setArea("export");
$("#setupTop").onclick = () => setArea("setup");
$("#projectsTop").onclick = () => setArea("projects");
function renderPanel() {
  closeProjectMenu();
  closeRegionMenu();
  const standalone = area === "text_to_speech";
  $("#workbench").classList.toggle("tts-workbench", standalone);
  $("#viewer").hidden = standalone;
  $("#exportTop").disabled = standalone || !P;
  if (!P && !["setup", "projects", "settings", "text_to_speech"].includes(area)) {
    panel.innerHTML =
      '<div class="empty-panel"><h2>Không gian làm việc</h2><p class="muted">Nhập video, sau đó tạo phụ đề, chọn giọng và xem trước kết quả.</p><button id="importPanel" class="primary">Nhập video</button></div>';
    $("#importPanel").onclick = importVideo;
    return;
  }
  const functions = {
    setup: renderSetup,
    subtitles: renderSubtitles,
    clean: renderClean,
    voice: renderVoice,
    text_to_speech: renderTextToSpeech,
    export: renderExport,
    projects: renderProjects,
    settings: renderSettings,
  };
  functions[area]();
  bindParams();
  icons(panel);
  cleanLive.refresh();
  window.NPKUpgrade?.panel();
  refreshProjectLock();
}
function renderSetup() {
  setupController = window.NPKSingleVideoSetup.render({panel, escape: esc, icon, api, change, save, queue, notice, guard, clock,
    project: () => P, catalog: () => catalog, jobs: () => jobs, mediaUrl, time: () => video.currentTime || 0, seek, previewStyle, continuePipeline: continuePausedPipeline,
    openArea: (next, tab) => { if (next === "subtitles" && tab) subtab = tab; if (next === "settings" && tab) settingsTab = tab; setArea(next); },
    externalVoice: () => { setArea("voice"); $("#voiceSource").value = "external"; renderExternalVoice(); },
    importSubtitles: () => $("#subtitleFile").click(), importVideo,
    nativePicker: () => Boolean(window.pywebview?.api?.pick_video),
    pickDirectory: async () => { if (window.pywebview?.api?.pick_directory) return window.pywebview.api.pick_directory(); notice("Nhập đường dẫn thư mục hiện có vào ô Thư mục xuất."); return null; },
    upload: async (file) => {
      if (!file || !/\.(mp4|mkv|mov|avi|webm|m4v)$/i.test(file.name)) throw new Error("Chọn tệp video MP4, MKV, MOV, AVI, WebM hoặc M4V.");
      if (file.size > 8 * 1024 ** 3) throw new Error("Video cần nhỏ hơn 8 GiB. Với tệp lớn, dùng Chọn tệp trong ứng dụng.");
      await save(); const body = new FormData(); body.append("file", file); body.append("name", file.name.replace(/\.[^.]+$/, ""));
      notice("Đang nhập video vào dữ liệu cục bộ của ứng dụng…");
      const project = await api("/api/studio/import-video", body, "POST", {timeout: 300000});
      await loadProject(project.id); setArea("setup"); await poll(); notice("Đã nhập video. Chọn cấu hình rồi bấm Bắt đầu xử lý video.");
    },
  });
}
function renderSubtitles() {
  panel.innerHTML = `<div class="panel-title"><h2>Phụ đề</h2><span class="grow"></span><span class="pill">${P.segments.length} câu</span></div>${tabs(
    [
      ["content", "Nội dung"],
      ["style", "Kiểu chữ"],
      ["quality", "Kiểm tra"],
    ],
    subtab,
  )}<div id="subBody"></div>`;
  $$("[data-tab]").forEach(
    (b) =>
      (b.onclick = () => {
        subtab = b.dataset.tab;
        renderPanel();
      }),
  );
  if (subtab === "style") renderStyle();
  else if (subtab === "quality") guard(renderQuality)();
  else renderCues();
}
// Cue edits transform authored source words together with the cue. Generated
// dub timings are invalidated; a moved cue no longer points to the same audio.
function editorInvalidateCaption(row) {
  if ("caption_words" in row || "caption_timing_key" in row) {
    row.caption_words = [];
    row.word_timing_source = "estimated";
    delete row.caption_timing_key;
  }
  return row;
}
function editorBoundCue(row, start, end) {
  const next = { ...row, start, end };
  if (Array.isArray(row.words))
    next.words = row.words.filter(word => word.end > start && word.start < end)
      .map(word => ({ ...word, start: Math.max(start, word.start), end: Math.min(end, word.end) }));
  return editorInvalidateCaption(next);
}
function editorShiftCue(row, offset) {
  const next = { ...row, start: row.start + offset, end: row.end + offset };
  if (Array.isArray(row.words)) next.words = row.words.map(word => ({ ...word,
    start: word.start + offset, end: word.end + offset }));
  return editorInvalidateCaption(next);
}
function editorSplitText(text, fraction, words, cut) {
  text = String(text || "");
  const normalized = value => String(value).replace(/[^\p{L}\p{N}]/gu, "").toLowerCase();
  if (Array.isArray(words) && words.length &&
      normalized(words.map(word => word.word).join("")) === normalized(text)) {
    const left = words.filter(word => (word.start + word.end) / 2 < cut);
    const count = normalized(left.map(word => word.word).join("")).length;
    if (!count) return ["", text];
    const letters = [...text.matchAll(/[\p{L}\p{N}]/gu)];
    if (count >= letters.length) return [text, ""];
    const boundary = letters[count].index;
    return [text.slice(0, boundary).trimEnd(), text.slice(boundary).trimStart()];
  }
  // No acoustic transcript for this column: split at a token boundary and
  // retain every character. It is an estimate, never a rewrite or shortening.
  const matches = [...text.matchAll(/\S+/g)];
  if (matches.length < 2) return [text, ""];
  const count = Math.max(1, Math.min(matches.length - 1, Math.round(matches.length * fraction)));
  const boundary = matches[count].index;
  return [text.slice(0, boundary).trimEnd(), text.slice(boundary).trimStart()];
}
function editorSplitCue(row, cut) {
  const first = editorBoundCue(row, row.start, cut), second = editorBoundCue(row, cut, row.end);
  const fraction = (cut - row.start) / (row.end - row.start);
  for (const key of ["text", "translated"]) {
    const words = key === "text" ? row.words : row.caption_words;
    [first[key], second[key]] = editorSplitText(row[key], fraction, words, cut);
  }
  if (Array.isArray(row.words)) {
    first.words = row.words.filter(word => (word.start + word.end) / 2 < cut)
      .map(word => ({ ...word, end: Math.min(cut, word.end) }));
    second.words = row.words.filter(word => (word.start + word.end) / 2 >= cut)
      .map(word => ({ ...word, start: Math.max(cut, word.start) }));
  }
  delete first.pause_after_ms;
  delete second.pause_before_ms;
  return [first, second];
}
function editorMergeCues(rows, params = {}) {
  if (rows.some(row => (row.speech_enabled !== false) !== (rows[0].speech_enabled !== false)))
    throw new Error("Các câu có vai khác nhau: có câu lồng tiếng, có câu chỉ hiển thị phụ đề. Chọn cùng vai trước khi gộp; NPK không tự đổi vai của câu.");
  if (rows.some(row => (row.voice_id || params.voice_id) !== (rows[0].voice_id || params.voice_id) ||
      (row.speech_rate ?? params.voice_speed) !== (rows[0].speech_rate ?? params.voice_speed)))
    throw new Error("Các câu đang dùng giọng hoặc tốc độ khác nhau. Chọn cùng giọng và tốc độ trước khi gộp để giữ đúng lời đọc.");
  const merged = {start: rows[0].start, end: rows.at(-1).end,
    text: rows.map(row => row.text).join(" "), translated: rows.map(row => row.translated).join(" ")};
  if (rows.some(row => "speech_enabled" in row)) merged.speech_enabled = rows[0].speech_enabled !== false;
  if (rows.every(row => Array.isArray(row.words)))
    merged.words = rows.flatMap(row => row.words.map(word => ({ ...word }))).sort((a, b) => a.start - b.start);
  if (rows.some(row => "caption_words" in row || "caption_timing_key" in row))
    editorInvalidateCaption(Object.assign(merged, { caption_words: [] }));
  if (rows.some(row => row.spoken_text?.trim()))
    merged.spoken_text = rows.map(row => row.spoken_text?.trim() || row.translated || row.text).join(" ");
  for (const key of ["voice_id", "speech_rate", "character_name"])
    if (rows[0][key] != null && rows.every(row => row[key] === rows[0][key])) merged[key] = rows[0][key];
  if (rows[0].pause_before_ms != null) merged.pause_before_ms = rows[0].pause_before_ms;
  if (rows.at(-1).pause_after_ms != null) merged.pause_after_ms = rows.at(-1).pause_after_ms;
  return merged;
}
function renderCues() {
  const body = $("#subBody");
  body.innerHTML = `<div class="sticky-tools"><div class="row"><button id="createSubs">Tạo phụ đề tự động</button><button id="translateSubs">Dịch phụ đề</button><button id="importSubs">Nhập tệp</button></div><div id="recognitionStatus" class="recognition-status info" hidden><div><strong id="recognitionTitle"></strong><p id="recognitionHint"></p><div id="recognitionRecovery" class="row recognition-recovery" hidden><button id="trySpeechRecognition">Thử nghe lời thoại</button><button id="chooseSubtitleRegion">Chọn vùng chữ thủ công</button></div></div><button id="recognitionDetails">Chi tiết</button></div><div class="toolbar"><button id="addCue">Thêm câu</button><button id="splitCue">Chia câu</button><button id="mergeCue">Gộp câu</button><button id="shiftCues">Dịch thời gian</button><button id="replaceText">Tìm / thay</button><button id="deleteCues">Xóa câu chọn</button></div><input id="cueSearch" type="search" placeholder="Tìm trong lời gốc hoặc bản dịch" aria-label="Tìm phụ đề" value="${esc(query)}"><div class="row"><label class="check"><input id="selectPage" type="checkbox">Chọn trang này</label><span class="grow"></span><button id="downloadSubs" class="link">Xuất tệp phụ đề</button></div></div><div id="cueRows"></div><div id="cuePages"></div>`;
  renderCueRows();
  $("#createSubs").textContent = "Tạo phụ đề tự động";
  $("#createSubs").title =
    "Tự đọc chữ phụ đề in sẵn trên hình; có thể mở Nâng cao để chọn nghe lời thoại";
  $("#createSubs").insertAdjacentHTML(
    "afterend",
    '<button id="listenSubs">Nhận dạng lời thoại</button>',
  );
  $("#listenSubs").onclick = () => extractDialog("asr");
  $("#trySpeechRecognition").onclick = () => extractDialog("asr");
  $("#chooseSubtitleRegion").onclick = () => extractDialog("ocr", true);
  body
    .querySelector(".sticky-tools")
    .insertAdjacentHTML("afterend", '<div id="subtitleSourceStatus"></div>');
  updateSubtitleSourceStatus();
  updateRecognitionUI();
  const orderButton = document.createElement("button");
  orderButton.textContent = "Sắp theo thời gian";
  orderButton.id = "sortCues";
  $("#subBody .toolbar").append(orderButton);
  orderButton.onclick = () => {
    change((p) => p.segments.sort((a, b) => a.start - b.start));
    selected.clear();
    renderPanel();
  };
  $("#cueSearch").oninput = (e) => {
    query = e.target.value;
    page = 0;
    renderCueRows();
  };
  $("#createSubs").onclick = () => extractDialog("ocr");
  $("#translateSubs").onclick = translationDialog;
  $("#translateSubs").insertAdjacentHTML('afterend', '<button id="reviewDialogue">Kiểm tra lời đọc</button>');
  $("#reviewDialogue").onclick = guard(dialogueReviewDialog);
  $("#importSubs").onclick = () => {
    importAsTarget = false;
    $("#subtitleFile").click();
  };
  $("#downloadSubs").onclick = subtitleDownload;
  $("#downloadSubs").insertAdjacentHTML(
    "beforebegin",
    `<label class="check"><input id="followVideo" type="checkbox" ${followVideo ? "checked" : ""}>Bám theo video</label>`,
  );
  $("#followVideo").onchange = (e) => (followVideo = e.target.checked);
  $("#selectPage").onchange = (e) => {
    filteredCues()
      .slice(page * 40, (page + 1) * 40)
      .forEach(({ i }) =>
        e.target.checked ? selected.add(i) : selected.delete(i),
      );
    renderCueRows();
  };
  $("#addCue").onclick = () => {
    const start = Math.min(
      Math.max(0, P.media.duration - 0.05),
      video.currentTime,
    );
    change(
      (p) =>
        p.segments.push({
          start,
          end: Math.min(start + 2, p.media.duration),
          text: "",
          translated: "",
        }),
      { render: true },
    );
  };
  $("#deleteCues").onclick = guard(async () => {
    if (!selected.size) return notice("Chọn câu cần xóa trước.");
    if (
      await ask(
        "Xóa câu phụ đề?",
        `Xóa ${selected.size} câu đã chọn. Có thể hoàn tác.`,
        "Xóa câu",
      )
    ) {
      change(
        (p) => (p.segments = p.segments.filter((_, i) => !selected.has(i))),
        { render: false },
      );
      selected.clear();
      renderPanel();
    }
  });
  $("#splitCue").onclick = () => {
    const i = [...selected][0],
      r = P.segments[i];
    if (selected.size !== 1 || !r) return notice("Chọn đúng một câu để chia.");
    if (r.spoken_text?.trim())
      return notice("Câu có lời đọc riêng. Sao chép lời này để giữ lại, xóa ô Lời để đọc rồi chia câu; sau đó nhập lời phù hợp cho từng câu mới.", true);
    const t = video.currentTime;
    if (t <= r.start + 0.02 || t >= r.end - 0.02)
      return notice("Tua đến vị trí bên trong câu rồi bấm Chia.");
    if ((r.pause_before_ms || 0) >= (t - r.start) * 1000 ||
        (r.pause_after_ms || 0) >= (r.end - t) * 1000)
      return notice("Giảm khoảng nghỉ trước hoặc sau để hai câu sau khi chia còn đủ thời gian đọc.", true);
    const [first, second] = editorSplitCue(r, t);
    change(
      (p) => p.segments.splice(i, 1, first, second),
      { render: true },
    );
    notice("Đã chia câu và mốc từ. Bản dịch chưa có mốc âm thanh được chia theo số từ; kiểm tra nội dung hai câu trước khi tạo giọng lại.");
  };
  $("#mergeCue").onclick = () => {
    const ids = [...selected].sort((a, b) => a - b);
    if (ids.length < 2 || ids.some((v, i) => i && v !== ids[i - 1] + 1))
      return notice("Chọn ít nhất hai câu liền nhau để gộp.");
    const rows = ids.map((i) => P.segments[i]);
    let merged;
    try { merged = editorMergeCues(rows, P.params); }
    catch (error) { return notice(error.message, true); }
    change(
      (p) =>
        p.segments.splice(ids[0], ids.length, merged),
      { render: false },
    );
    selected.clear();
    renderPanel();
    if (rows.some((r) => r.spoken_text?.trim() || r.speech_rate != null || r.voice_id || r.pause_before_ms || r.pause_after_ms))
      notice("Đã gộp lời đọc; giữ nghỉ đầu/cuối và giọng, tốc độ riêng nếu các câu dùng giống nhau. Kiểm tra nhịp đọc của câu mới.");
  };
  $("#shiftCues").onclick = shiftDialog;
  $("#replaceText").onclick = replaceDialog;
}
let latestRecognition = null;
const pendingRecognition = new Map();
function recognitionMethod(job) {
  return ['ocr', 'asr'].includes(job.kind) ? job.kind
    : job.kind === 'pipeline' ? job.extraction_method || job.configuration?.pipeline_extract : null;
}
function recognitionWork() {
  if (!P) return null;
  const candidates = jobs.filter(j => j.project_id === P.id &&
    (j.source_revision || null) === (P.source_revision || null) &&
    ['ocr', 'asr'].includes(recognitionMethod(j)));
  if (latestRecognition?.project_id === P.id &&
      (latestRecognition.source_revision || null) === (P.source_revision || null) &&
      !candidates.some(j => j.id === latestRecognition.id)) candidates.push(latestRecognition);
  const rank = j => j.state === 'running' ? 0 : j.state === 'queued' ? 1 : 2;
  candidates.sort((a, b) => rank(a) - rank(b) || (b.created || 0) - (a.created || 0));
  return candidates[0] || null;
}
function updateRecognitionUI() {
  if (!P || !$('#recognitionStatus')) return;
  const task = recognitionWork(), pending = pendingRecognition.get(P.id);
  const method = pending || (task ? recognitionMethod(task) : P.coverage?.method);
  if ($('#regions').dataset.readingMethod !== (method || '')) {
    $('#regions').dataset.readingMethod = method || '';
    drawRegions();
  }
  const busy = Boolean(pending || task && ['queued', 'running'].includes(task.state));
  for (const [id, value] of [['createSubs', 'ocr'], ['listenSubs', 'asr']]) {
    const button = $('#' + id);
    if (!button) continue;
    button.classList.toggle('primary', method === value);
    button.setAttribute('aria-pressed', String(method === value));
    button.disabled = busy;
    button.title = busy ? 'Đang có lượt nhận dạng. Chờ hoàn tất hoặc mở Chi tiết để hủy.'
      : value === 'ocr' ? 'Đọc phụ đề in trên hình, kể cả khi không có lời nói'
      : 'Tạo phụ đề từ lời nói; không lấy chữ chỉ xuất hiện trên hình';
  }
  const status = $('#recognitionStatus');
  status.hidden = !task && !pending;
  if (!status.hidden) {
    const name = method === 'asr' ? 'Nghe lời thoại' : 'Đọc chữ trên hình';
    const percent = Math.max(0, Math.min(100, Math.round((Number(task?.progress) || 0) * 100)));
    const recognized = task?.kind === 'pipeline' && (task.stage_journal?.completed_steps || []).includes(method);
    const failure = task?.state === 'error' ? jobFailureStage(task) : null;
    $('#recognitionTitle').textContent = pending ? `${name} · Đang gửi yêu cầu…`
      : recognized && ['error','cancelled'].includes(task.state) ? `${name} · Đã nhận dạng xong · Quy trình đã dừng`
      : `${name}${task.kind === 'pipeline' ? ' · Trong quy trình xuất' : ''} · ${stateNames[task.state] || 'Chưa xác định'}${task.state === 'running' ? ` · ${percent}%` : ''}${jobTimingLabel(task) ? ' · '+jobTimingLabel(task) : ''}`;
    $('#recognitionHint').textContent = busy
      ? (P.segments.length ? 'Bảng hiện tại vẫn được giữ trong lúc xử lý.' : 'Bảng phụ đề sẽ xuất hiện khi có kết quả. Bạn vẫn có thể xem video.')
      : recognized && ['error','cancelled'].includes(task.state) ? `Kết quả nhận dạng đã được lưu. ${failure ? 'Quy trình dừng ở bước '+failure.name+'. ' : ''}Mở Chi tiết để tiếp tục các bước còn lại.`
      : task.state === 'error' ? `Lượt này đã dừng do lỗi, không còn chạy. ${jobEndedLabel(task)} Mở Chi tiết để xem lỗi và phần có thể khôi phục; không tự chạy lại.`
      : task.state === 'cancelled' ? 'Lượt nhận dạng đã dừng. Bạn có thể chọn cách nhận dạng để chạy lại.'
      : task.state === 'done' && method === 'ocr' && !P.segments.length ? 'NPK chưa tìm thấy câu phụ đề. Thử nghe lời thoại, hoặc chọn vùng chữ nếu phụ đề nằm ngoài phần dưới video.'
      : P.segments.length ? 'Kiểm tra nội dung và thời gian trong bảng trước khi dịch hoặc xuất.'
      : 'Bảng hiện tại chưa có câu. Mở Chi tiết để kiểm tra kết quả trước khi chạy lại.';
    status.classList.toggle('warning', !pending && task?.state === 'error');
    status.classList.toggle('info', pending || task?.state !== 'error');
    $('#recognitionDetails').hidden = !task || Boolean(pending);
    $('#recognitionRecovery').hidden = busy || task?.state !== 'done' || method !== 'ocr' || Boolean(P.segments.length);
    $('#recognitionDetails').onclick = guard(async () => {
      setProgressExpanded(true);
      await showJob(task.id);
    });
  }
  const empty = $('#emptyCueRows');
  if (empty) empty.hidden = !query && !status.hidden;
  if ($('#cuePages')) $('#cuePages').hidden = !P.segments.length && !status.hidden;
  if ($('#subtitleSourceStatus')) $('#subtitleSourceStatus').hidden = !P.segments.length && !status.hidden;
}
function subtitleSourceSummary(project) {
  const rows = project.segments,
    duration = +project.media.duration,
    coverage = project.coverage || {};
  let last = 0,
    gap = null,
    cursor = 0;
  for (const row of [...rows].sort((a, b) => a.start - b.start)) {
    if (!gap && row.start - cursor > Math.max(12, duration * 0.15))
      gap = [cursor, +row.start];
    cursor = Math.max(cursor, +row.end);
    last = Math.max(last, +row.end);
  }
  if (rows.length && duration - last > Math.max(10, duration * 0.15))
    gap = [last, duration];
  const source =
    { ocr: "Đọc chữ trên hình", asr: "Nghe lời thoại", import: "Tệp phụ đề" }[
      coverage.method
    ] || "Bảng phụ đề đang có · chưa có thông tin lượt nhận dạng";
  const summary = rows.length
    ? `${rows.length} câu · câu cuối đến ${clock(last)} / ${clock(duration)}`
    : "Chưa có phụ đề. Chọn đọc chữ trên hình hoặc nghe lời thoại để bắt đầu.";
  return { summary, source, gap };
}
function updateSubtitleSourceStatus() {
  const target = $("#subtitleSourceStatus");
  if (!target || !P) return;
  const { summary, source, gap } = subtitleSourceSummary(P);
  target.className = gap ? "warning subtitle-source" : "subtitle-source muted";
  target.innerHTML = `<div>${esc(summary)}</div>${P.segments.length ? `<small>${esc(source)}</small>` : ""}${gap ? `<div>Đoạn ${clock(gap[0])}–${clock(gap[1])} chưa có câu. Nếu trên hình còn chữ, hãy đọc chữ trên hình trước khi dịch.</div><button id="inspectSubtitleGap" class="link">Xem đoạn này</button>` : ""}`;
  if (gap)
    $("#inspectSubtitleGap").onclick = () =>
      seek(Math.min(gap[0] + 0.5, gap[1]));
}
function filteredCues() {
  const q = query.toLocaleLowerCase();
  return P.segments
    .map((r, i) => ({ r, i }))
    .filter(
      ({ r }) =>
        !q || (r.text + " " + r.translated).toLocaleLowerCase().includes(q),
    );
}
function selectedSpeechText(row, params = P?.params || {}) {
  if (row.speech_enabled === false) return "";
  const keys = ({translated: ["translated"], edited: ["spoken_text", "translated"], source: ["text"]})[params.tts_text_source]
    || ["spoken_text", "translated", "text"];
  for (const key of keys) { const text = String(row[key] || "").trim(); if (text) return text; }
  return "";
}
async function dialogueReviewDialog() {
  await save();
  if (!P) return;
  const id = P.id, version = editVersion, revision = P.updated;
  const report = await api('/api/studio/projects/' + id + '/dialogue-review');
  if (P?.id !== id || dirty || saving || editVersion !== version || P.updated !== revision ||
      report.expected_revision !== revision)
    throw new Error('Nội dung đã đổi; mở lại Kiểm tra lời đọc để lấy đúng bản đang dùng.');
  const proposals = (report.proposals || []).slice(0, 80);
  const choices = proposals.map(proposal => {
    const indices = proposal.indices || [], first = indices[0], last = indices.at(-1);
    const samples = indices.map(index => {
      const row = P.segments[index];
      const disabled = (proposal.changes || []).some(change => change.index === index && change.speech_enabled === false);
      return row ? `<div><strong>Câu ${index + 1} · ${disabled ? 'đề xuất tắt đọc' : 'giữ đọc'}</strong><p>Lời gốc: ${esc(row.text || '')}</p><p>Đang đọc: ${esc(selectedSpeechText(row))}</p></div>` : '';
    }).join('');
    return `<article class="info"><label class="check"><input type="checkbox" data-dialogue-proposal="${esc(proposal.id)}">${esc(proposal.message)}</label><small>Câu ${first + 1}${last !== first ? '–' + (last + 1) : ''}</small><details><summary>Xem đủ lời gốc và lời đang đọc</summary>${samples}</details></article>`;
  }).join('');
  const issues = (report.issues || []).slice(0, 30).map(issue => `<p>${esc(issue.message || '')}</p>`).join('');
  openModal('Kiểm tra lời đọc', `<p>Đây là gợi ý từ nội dung đã có trong máy. Mở chi tiết, xem lời gốc và chủ động chọn những gợi ý bạn đồng ý. Câu lặp có thể là lời nhấn mạnh có chủ ý.</p><p class="muted">Áp dụng chỉ tắt lồng tiếng ở các câu đã chọn; phụ đề, bản dịch, mốc thời gian và vùng xóa chữ được giữ. Có thể hoàn tác hoặc khôi phục phiên bản. Không gửi nội dung tới dịch vụ AI.</p>${choices || '<div class="info">Chưa thấy nhóm lặp hoặc dòng ghi nguồn đủ rõ để đề xuất. Bạn vẫn có thể chỉnh Lời đọc và nhịp nghỉ của từng câu.</div>'}${(report.proposals || []).length > proposals.length ? '<p class="warning">Đang hiển thị 80 gợi ý đầu. Sau khi xử lý, mở kiểm tra lại để xem phần còn lại.</p>' : ''}${issues ? `<details><summary>Những chỗ cần xem riêng</summary>${issues}</details>` : ''}`, [
    {label: 'Đóng', run: () => closeModal()},
    ...(proposals.length ? [{label: 'Áp dụng các gợi ý đã chọn', primary: true, run: async () => {
      const ids = $$('[data-dialogue-proposal]:checked').map(input => input.dataset.dialogueProposal);
      if (!ids.length) throw new Error('Chọn ít nhất một gợi ý hoặc bấm Đóng để giữ nguyên.');
      if (P?.id !== id || dirty || saving || editVersion !== version || P.updated !== revision ||
          report.expected_revision !== revision)
        throw new Error('Nội dung đã đổi; mở lại Kiểm tra lời đọc trước khi áp dụng.');
      const result = await api('/api/studio/projects/' + id + '/dialogue-review/apply', {
        expected_revision: report.expected_revision, signature: report.signature, proposal_ids: ids});
      if (P?.id !== id) { closeModal(); return; }
      if (dirty || saving || editVersion !== version) {
        conflict = true;
        closeModal();
        notice('Đã lưu lựa chọn lời đọc. Bản đang nhập vẫn được giữ; tải lại dự án để đối chiếu.', true);
        return;
      }
      remember();
      P = result; quality = null; editVersion++; selected.clear();
      closeModal(); renderPanel(); drawCaption(); updateUndo();
      $('#saved').textContent = 'Đã lưu';
      notice('Đã lưu lựa chọn lời đọc. Bản giọng và video trước vẫn được giữ; tạo lại phần lồng tiếng khi sẵn sàng.');
    }}] : []),
  ]);
}
function speechSourceName(params = P?.params || {}) {
  return ({translated: "Bản dịch", edited: "Lời đã biên tập", source: "Phụ đề gốc"})[params.tts_text_source] || "Tương thích dự án cũ";
}
function cueCharacterLabel(row) {
  if (row.speech_enabled === false) return "Chỉ hiển thị phụ đề";
  const id = row.voice_id || P.params.voice_id;
  const voice = catalog?.voices?.find(item => item.id === id);
  return [row.character_name?.trim(), voice?.name || id].filter(Boolean).join(" · ");
}
function updateSpeechSourceHints() {
  if (!P) return;
  const source = P.params.tts_text_source, rows = P.segments;
  const different = rows.filter(row => row.spoken_text?.trim() && row.spoken_text.trim() !== String(row.translated || "").trim()).length;
  const hint = $("#voiceSpeechSourceHint");
  if (hint) hint.textContent = source === "translated"
    ? `Đọc đầy đủ ô Bản dịch; bỏ qua lời đọc riêng đã lưu.${different ? ` ${different} câu có lời riêng khác bản dịch và không được sử dụng.` : ""}`
    : source === "edited" ? `Ưu tiên Lời đọc riêng, để trống mới dùng Bản dịch.${different ? ` ${different} câu sẽ đọc khác phụ đề hiển thị.` : ""} Không tự chuyển sang lời gốc.`
      : source === "source" ? "Đọc nguyên ô Lời gốc; không sử dụng bản dịch hay lời đọc riêng."
        : "Dự án cũ ưu tiên lời đọc riêng, sau đó bản dịch rồi lời gốc. Chọn Bản dịch để chỉ đọc bản dịch đầy đủ.";
  $$("[data-cue-speech-effective]").forEach(el => {
    const row = rows[+el.dataset.cueSpeechEffective]; if (!row) return;
    const text = selectedSpeechText(row);
    el.textContent = row.speech_enabled === false ? "Chỉ hiển thị phụ đề · Không tạo lồng tiếng cho câu này."
      : `${speechSourceName()}: ${text || "Chưa có nội dung để đọc; cần hoàn thiện trước khi tạo giọng."}`;
    el.classList.toggle("warning", row.speech_enabled !== false && !text);
  });
  $$("[data-cue-character-summary]").forEach(el => {
    const row = rows[+el.dataset.cueCharacterSummary]; if (row) el.textContent = cueCharacterLabel(row);
  });
}
async function updateVoiceSpeechState() {
  const target = $("#voiceSpeechArtifactStatus");
  if (!P || !target || area !== "voice") return;
  if (P.params.voice_path) { target.textContent = "Đang dùng tệp âm thanh ngoài; lựa chọn nội dung chỉ áp dụng khi tạo giọng AI mới."; target.className = "muted"; return; }
  if (dirty || saving) { target.textContent = "Có chỉnh sửa đang lưu. NPK sẽ kiểm tra lại bản giọng sau khi lưu xong."; target.className = "muted"; return; }
  const id = P.id, version = editVersion;
  const status = await api('/api/studio/projects/' + id + '/artifacts-status');
  if (P?.id !== id || area !== "voice" || editVersion !== version || dirty || saving || !$("#voiceSpeechArtifactStatus")) return;
  target.className = status.speech_ready ? "muted" : "warning";
  target.textContent = status.speech_ready ? "Lồng tiếng đã tạo khớp nội dung và giọng đang chọn."
    : P.artifacts?.tts ? "Bản giọng đã tạo không còn khớp lời/giọng hiện tại. Tạo lại lồng tiếng trước khi xuất."
      : "Chưa có lồng tiếng khớp cấu hình. Chọn giọng và bấm Tạo lồng tiếng.";
}
function cueSpeechControls(row, index) {
  const custom = row.speech_enabled === false || !!row.spoken_text?.trim() || row.speech_rate != null ||
    (row.pause_before_ms || 0) > 0 || (row.pause_after_ms || 0) > 0;
  return `<details class="cue-speech" ${custom ? "open" : ""}><summary>Lời đọc và nhịp nghỉ${custom ? " · đã chỉnh riêng" : ""}<small class="cue-character-summary" data-cue-character-summary="${index}">${esc(cueCharacterLabel(row))}</small></summary><div>
    <label class="check"><input id="cueSpeechEnabled${index}" class="cue-speech-enabled" type="checkbox" data-i="${index}" aria-label="Lồng tiếng câu ${index + 1}" ${row.speech_enabled !== false ? "checked" : ""}>Lồng tiếng câu này</label>
    <label for="cueCharacter${index}">Nhân vật<input id="cueCharacter${index}" class="cue-text cue-character-name" data-i="${index}" data-part="character_name" maxlength="80" value="${esc(row.character_name || "")}" placeholder="Tên để kiểm tra giọng, không tự chọn giọng" aria-label="Nhân vật câu ${index + 1}"></label>
    <p class="cue-effective-speech muted" data-cue-speech-effective="${index}" role="status">${row.speech_enabled === false ? "Chỉ hiển thị phụ đề · Không tạo lồng tiếng cho câu này." : `${esc(speechSourceName())}: ${esc(selectedSpeechText(row) || "Chưa có nội dung để đọc; cần hoàn thiện trước khi tạo giọng.")}`}</p>
    <label for="cueSpoken${index}">Lời đọc riêng · chỉ dùng khi chọn Lời đã biên tập<textarea id="cueSpoken${index}" class="cue-text cue-spoken-text" data-i="${index}" data-part="spoken_text" aria-label="Lời để đọc câu ${index + 1}" aria-describedby="cueSpeechHint${index}" maxlength="10000" placeholder="Để trống để dùng Bản dịch khi chọn Lời đã biên tập">${esc(row.spoken_text || "")}</textarea></label>
    <p id="cueSpeechHint${index}" class="muted cue-speech-hint">Chỉnh cách nói hoặc rút gọn lời đọc ở đây; nội dung phụ đề trên hình được giữ riêng. Dấu câu giúp ngắt ý, chưa có điều khiển cảm xúc tự động.</p>
    <div class="three cue-speech-numbers"><label>Tốc độ riêng<input class="cue-speech-number" data-i="${index}" data-part="speech_rate" type="number" min="0.5" max="2" step="0.05" value="${esc(row.speech_rate ?? "")}" placeholder="Theo video (${esc(P.params.voice_speed)})" aria-label="Tốc độ đọc câu ${index + 1}" aria-describedby="cuePauseHint${index}"></label>
    <label>Nghỉ trước (ms)<input class="cue-speech-number" data-i="${index}" data-part="pause_before_ms" type="number" min="0" max="2000" step="10" value="${esc(row.pause_before_ms ?? "")}" placeholder="0" aria-label="Nghỉ trước câu ${index + 1}" aria-describedby="cuePauseHint${index}"></label>
    <label>Nghỉ sau (ms)<input class="cue-speech-number" data-i="${index}" data-part="pause_after_ms" type="number" min="0" max="2000" step="10" value="${esc(row.pause_after_ms ?? "")}" placeholder="0" aria-label="Nghỉ sau câu ${index + 1}" aria-describedby="cuePauseHint${index}"></label></div>
    <small id="cuePauseHint${index}" class="muted">Tốc độ 0,5–2 lần; để trống để theo tốc độ video. Hai khoảng nghỉ nằm trong thời gian của câu, mỗi khoảng tối đa 2.000 ms.</small>
  </div></details>`;
}
function renderCueRows() {
  const filtered = filteredCues();
  page = Math.min(page, Math.max(0, Math.ceil(filtered.length / 40) - 1));
  $("#cueRows").innerHTML =
    filtered
      .slice(page * 40, (page + 1) * 40)
      .map(
        ({ r, i }) =>
          `<article class="cue ${currentCue === i ? "current" : ""}" data-cue="${i}"><div class="cue-head"><input type="checkbox" class="cue-select" data-i="${i}" aria-label="Chọn câu ${i + 1}" ${selected.has(i) ? "checked" : ""}><span class="cue-number">${i + 1}</span><button class="jump" data-i="${i}" title="Xem câu này">${icon("play")}</button><input class="time-input" data-i="${i}" data-part="start" type="number" step=".01" min="0" max="${P.media.duration}" value="${r.start.toFixed(2)}" aria-label="Bắt đầu câu ${i + 1}"><span>→</span><input class="time-input" data-i="${i}" data-part="end" type="number" step=".01" min="0" max="${P.media.duration}" value="${r.end.toFixed(2)}" aria-label="Kết thúc câu ${i + 1}"><small>giây</small></div><div class="two"><label>Lời gốc<textarea class="cue-text" data-i="${i}" data-part="text" aria-label="Lời gốc câu ${i + 1}">${esc(r.text)}</textarea></label><label>Bản dịch<textarea class="cue-text" data-i="${i}" data-part="translated" aria-label="Bản dịch câu ${i + 1}" placeholder="Chưa dịch">${esc(r.translated)}</textarea></label></div>${cueSpeechControls(r, i)}</article>`,
      )
      .join("") ||
    '<div class="info" id="emptyCueRows">Chưa có câu phù hợp. Bạn có thể tạo hoặc nhập phụ đề.</div>';
  $("#cuePages").innerHTML =
    `<div class="pagination"><button id="cuePrev" ${page === 0 ? "disabled" : ""}>Trang trước</button><small>${page + 1} / ${Math.max(1, Math.ceil(filtered.length / 40))} · ${filtered.length} câu</small><button id="cueNext" ${(page + 1) * 40 >= filtered.length ? "disabled" : ""}>Trang sau</button></div>`;
  updateRecognitionUI();
  $("#cuePrev").onclick = () => {
    page--;
    renderCueRows();
  };
  $("#cueNext").onclick = () => {
    page++;
    renderCueRows();
  };
  $$(".cue-select").forEach(
    (el) =>
      (el.onchange = () =>
        el.checked
          ? selected.add(+el.dataset.i)
          : selected.delete(+el.dataset.i)),
  );
  $$(".cue-speech-enabled").forEach(el => {
    el.onchange = () => change(p => {
      p.segments[+el.dataset.i].speech_enabled = el.checked;
    }, { render: true });
  });
  $$(".jump").forEach(
    (b) =>
      (b.onclick = () => {
        currentCue = +b.dataset.i;
        seek(P.segments[currentCue].start + 0.01);
        renderCueRows();
      }),
  );
  $$(".cue-text").forEach(
    (el) =>
      (el.oninput = () =>
        change((p) => {
          const row = p.segments[+el.dataset.i], key = el.dataset.part;
          if (key === "character_name" && !el.value.trim()) delete row[key];
          else row[key] = key === "character_name" ? el.value.slice(0, 80) : el.value;
        })),
  );
  $$(".time-input").forEach(
    (el) => {
      const commit = (report = false) => {
        const r = P.segments[+el.dataset.i],
          value = el.valueAsNumber;
        if (
          el.value.trim() === "" || !Number.isFinite(value) ||
          value < 0 ||
          value > P.media.duration ||
          (el.dataset.part === "start" ? value >= r.end : value <= r.start) ||
          ((r.pause_before_ms || 0) + (r.pause_after_ms || 0)) >=
            (el.dataset.part === "start" ? r.end - value : value - r.start) * 1000
        ) {
          if (report) {
            el.value = r[el.dataset.part];
            notice("Mốc kết thúc phải sau bắt đầu, nằm trong video và đủ chỗ cho khoảng nghỉ.", true);
          }
          return;
        }
        if (r[el.dataset.part] !== value)
          change((p) => {
            const row = p.segments[+el.dataset.i];
            p.segments[+el.dataset.i] = editorBoundCue(row,
              el.dataset.part === "start" ? value : row.start,
              el.dataset.part === "end" ? value : row.end);
          });
      };
      el.oninput = () => commit();
      el.onchange = () => commit(true);
    },
  );
  $$(".cue-speech-number").forEach(
    (el) => {
      const commit = (report = false) => {
        const row = P.segments[+el.dataset.i], key = el.dataset.part;
        const empty = el.value.trim() === "" && !el.validity.badInput;
        const value = empty ? null : el.valueAsNumber;
        const pause = key !== "speech_rate";
        const before = key === "pause_before_ms" ? value || 0 : row.pause_before_ms || 0;
        const after = key === "pause_after_ms" ? value || 0 : row.pause_after_ms || 0;
        const valid = empty || (Number.isFinite(value) &&
          (pause ? value >= 0 && value <= 2000 && Number.isInteger(value) &&
            before + after < (row.end - row.start) * 1000 : value >= .5 && value <= 2));
        el.setAttribute("aria-invalid", String(!valid));
        el.setCustomValidity(valid ? "" : pause ? "Nhập 0–2.000 ms và giữ tổng khoảng nghỉ ngắn hơn câu." : "Nhập tốc độ từ 0,5 đến 2 lần.");
        if (!valid) {
          if (report) notice(el.validationMessage, true);
          return;
        }
        if (empty ? row[key] != null : row[key] !== value)
          change((p) => {
            if (empty) delete p.segments[+el.dataset.i][key];
            else p.segments[+el.dataset.i][key] = value;
          });
      };
      el.oninput = () => commit();
      el.onchange = () => commit(true);
    },
  );
}
function renderStyle() {
  $("#subBody").innerHTML =
    `<p class="muted">Áp dụng cho toàn bộ phụ đề. Khung xem phản ánh cỡ chữ theo độ phân giải video.</p>${field("burn_subtitles", "Hiện và gắn phụ đề lên video", "check")}<div class="row"><button data-preset="simple">Tối giản</button><button data-preset="review">Review</button><button data-preset="box">Nền hộp</button></div><div class="two section-end">${field("font", "Phông chữ", "text")}${field("font_size", "Cỡ chữ trên video")}${field("font_color", "Màu chữ", "color")}${field("outline_color", "Màu viền / nền", "color")}${field("outline", "Độ dày viền")}${field(
      "subtitle_alignment",
      "Vị trí",
      "select",
      [
        ["bottom", "Phía dưới"],
        ["middle", "Giữa khung"],
        ["top", "Phía trên"],
      ],
    )}${field("margin", "Lề trên / dưới")}${field("subtitle_margin_x", "Lề trái / phải")}</div>${field("subtitle_background", "Nền hộp sau chữ", "check")}<details><summary>Định dạng nâng cao</summary><div>${field("subtitle_shadow", "Độ bóng")}<p class="muted">Tệp SRT chỉ giữ nội dung và thời gian. Chọn ASS để lưu kiểu chữ. Phông cần được cài trên máy xuất video.</p></div></details>`;
  $("#subBody").insertAdjacentHTML(
    "afterbegin",
    `<label>Phạm vi kiểu chữ<select id="styleScope"><option value="all">Kiểu mặc định toàn video</option><option value="selected" ${selected.size ? "" : "disabled"}>${selected.size} câu đã chọn</option></select></label><p class="muted">${P.segments.filter((r) => r.subtitle_style).length} câu có kiểu riêng. Chọn câu trong Nội dung để chỉnh một nhóm.</p><div class="row"><button id="clearCueStyle">Bỏ kiểu riêng của câu chọn</button><button id="dragCaption">Kéo vị trí chữ trên hình</button></div>`,
  );
  $("#subBody > p:last-of-type").insertAdjacentHTML("afterend", `<div class="two caption-flow-controls">${field("subtitle_flow", "Cách hiển thị chữ", "select", [["single", "Một dòng · tự chuyển cụm"], ["wrap", "Cả câu · xuống dòng"]])}${field("subtitle_chunk_words", "Tối đa từ mỗi cụm")}</div>`);
  const chunkWordsInput = $('#subBody .param[data-key="subtitle_chunk_words"]');
  chunkWordsInput.min = 2; chunkWordsInput.max = 12; chunkWordsInput.step = 1;
  updateCaptionFlowControls();
  $("#subBody > p:last-of-type").textContent =
    styleScope === "selected" && selected.size
      ? "Áp dụng cho các câu đã chọn. Khung xem phản ánh cỡ chữ theo độ phân giải video."
      : "Áp dụng cho kiểu mặc định toàn video; câu có kiểu riêng vẫn giữ định dạng riêng.";
  $("#styleScope").value =
    styleScope === "selected" && selected.size ? "selected" : "all";
  $("#styleScope").onchange = (e) => {
    styleScope = e.target.value;
    renderPanel();
  };
  const exactPreview = document.createElement("button");
  exactPreview.id = "exactSubtitlePreview";
  exactPreview.textContent = "Xem mẫu chữ như khi xuất";
  $("#subBody").append(exactPreview);
  $("#subBody details > div").insertAdjacentHTML(
    "afterbegin",
    `<div class="two caption-wrap-controls">${field("subtitle_line_chars", "Ký tự tối đa mỗi dòng")}${field("subtitle_max_lines", "Số dòng mong muốn")}</div><p class="muted caption-wrap-controls">Xuống dòng theo số ký tự; không cắt mất nội dung. Câu vượt số dòng sẽ được nhắc trong Kiểm tra. Bấm xem mẫu để kiểm cách hiển thị thực tế.</p>`,
  );
  updateCaptionFlowControls();
  exactPreview.onclick = guard(() => queue("subtitle_preview"));
  if (P.artifacts?.subtitle_preview) {
    const a = P.artifacts.subtitle_preview;
    $("#subBody").insertAdjacentHTML(
      "beforeend",
      `<div class="result-card"><h3>Mẫu chữ ở ${clock(a.time)}</h3><img style="width:100%" alt="Mẫu phụ đề được vẽ bằng bộ xuất video" src="${mediaUrl("/media/job/" + a.job_id + "/subtitle-preview.png")}"><p class="muted">Mẫu dùng bộ vẽ chữ của bản xuất. Sau khi đổi kiểu, bấm xem mẫu lại.</p></div>`,
    );
  }
  $("#clearCueStyle").onclick = () => {
    if (!selected.size) return notice("Chọn câu cần bỏ kiểu riêng.");
    change(
      (p) => selected.forEach((i) => delete p.segments[i].subtitle_style),
      { render: true },
    );
  };
  $("#dragCaption").onclick = () => {
    captionDragMode = !captionDragMode;
    if (captionDragMode) previewStyle();
    $("#caption").style.pointerEvents = captionDragMode ? "auto" : "none";
    $("#caption").style.cursor = captionDragMode ? "ns-resize" : "default";
    notice(
      captionDragMode
        ? "Kéo phụ đề lên / xuống. Thay đổi vị trí cũng có thể nhập bằng ô lề."
        : "Đã tắt kéo vị trí.",
    );
  };
  $$("[data-preset]").forEach(
    (b) =>
      (b.onclick = () =>
        change(
          (p) =>
            Object.entries({
              font: "Arial",
              font_size: b.dataset.preset === "review" ? 54 : 46,
              font_color: "#FFFFFF",
              outline_color: "#000000",
              outline: 3,
              subtitle_alignment: "bottom",
              subtitle_background: b.dataset.preset === "box",
              burn_subtitles: true,
            }).forEach(([k, v]) => applyParam(p, k, v)),
          { render: true },
        )),
  );
  $("#retryFonts").onclick = () => loadFontPicker();
  loadFontPicker();
}
function updateCaptionFlowControls() {
  const flow = panel.querySelector('.param[data-key="subtitle_flow"]');
  const words = panel.querySelector('.param[data-key="subtitle_chunk_words"]');
  if (flow && words) words.closest('label').hidden = flow.value !== "single";
  if (flow) panel.querySelectorAll('.caption-wrap-controls').forEach(control => {control.hidden = flow.value === "single";});
}
let captionDragMode = false,
  captionDrag = null;
$("#caption").onpointerdown = (e) => {
  if (!captionDragMode) return;
  e.preventDefault();
  captionDrag = { y: e.clientY };
  e.currentTarget.setPointerCapture(e.pointerId);
};
$("#caption").onpointerup = (e) => {
  if (!captionDrag) return;
  const rect = $("#picture").getBoundingClientRect(),
    y = Math.max(0, Math.min(rect.height, e.clientY - rect.top));
  captionDrag = null;
  change(
    (p) => {
      applyParam(p, "subtitle_alignment", "bottom");
      applyParam(
        p,
        "margin",
        Math.round(((rect.height - y) / rect.height) * p.media.height),
      );
    },
    { render: true },
  );
};
let qualityRenderSerial=0;
function groupedSubtitleIssues(issues) {
  const names={reading:"Nhịp đọc",line_budget:"Số dòng",line_length:"Độ dài phụ đề",overlap:"Mốc chồng nhau",duplicate:"Câu lặp",brief_fragment:"Chữ xuất hiện ngắn",cropped_text:"Mép vùng đọc",internal_gap:"Khoảng trống",trailing_gap:"Đoạn cuối",coverage_unknown:"Phạm vi chưa xác minh",coverage_partial:"Phạm vi nhận dạng",timing:"Mốc thời gian",caption_fit:"Chữ chưa vừa khung",unsafe_position:"Vị trí chữ",font_missing:"Phông chữ",untranslated:"Bản dịch còn thiếu",blank:"Nội dung còn thiếu",empty:"Chưa có phụ đề"};
  const groups=new Map();
  for(const issue of issues || []) {
    const category=String(issue.code || "other").startsWith("ocr_")?"ocr_tracks":issue.code || "other";
    const key=(issue.severity || "warning")+":"+category;
    if(!groups.has(key))groups.set(key,{severity:issue.severity || "warning",name:category==="ocr_tracks"?"Lớp chữ cần đối chiếu":names[category] || "Chi tiết kiểm tra",issues:[]});
    groups.get(key).issues.push(issue);
  }
  const priority={error:0,review:1,warning:2};
  return [...groups.values()].sort((a,b)=>(priority[a.severity]??2)-(priority[b.severity]??2));
}
async function renderQuality() {
  const id = P.id,renderId=++qualityRenderSerial;
  await save();
  if(P?.id!==id||renderId!==qualityRenderSerial||area!=="subtitles"||subtab!=="quality")return;
  const q = await api("/api/studio/projects/" + id + "/quality");
  if (P?.id !== id || renderId!==qualityRenderSerial || area !== "subtitles" || subtab !== "quality") return;
  quality = q;
  const delivery = q.delivery;
  const deliverySummary = delivery ? `<div class="quality-delivery ${delivery.constraint_count ? "error" : "info"}"><strong>Nhịp giọng · ${delivery.measured_count}/${delivery.cue_count} câu đã đo</strong><p>${delivery.constraint_count ? `${delivery.constraint_count} câu chưa vừa thời gian. Kiểm tra các mốc trước khi lồng tiếng.` : delivery.unknown_count ? `${delivery.unknown_count} câu sẽ được đo khi tạo giọng.` : "Các clip đã đo vừa thời gian hiện tại."}</p></div>` : q.delivery_error ? `<p class="warning">${esc(q.delivery_error)}</p>` : "";
  const issueGroups=groupedSubtitleIssues(q.issues),errors=(q.issues || []).filter(x=>x.severity==="error").length;
  const detailRows=issueGroups.map(group=>`<details class="quality-issue-group ${group.severity==="error"?"error":group.severity==="review"?"warning":""}" ${group.severity==="error"?"open":""}><summary>${esc(group.name)} <span class="pill">${group.issues.length}</span>${group.severity==="error"?" · Cần sửa":group.severity==="review"?" · Chưa xác minh":""}</summary><div class="quality-issue-list">${group.issues.map(x=>`<div class="quality-issue"><p>${esc(x.message)}</p><div class="row">${x.start!=null?`<button class="issue-jump" data-time="${x.start}">Xem ${clock(x.start)}</button>`:""}${x.index!=null?`<button class="issue-edit" data-i="${x.index}">Sửa câu ${x.index+1}</button>`:""}</div></div>`).join("")}</div></details>`).join("");
  const mode=P.params.subtitle_automation || "manual";
  const automationDisclosure = mode === "router9"
    ? "9Router nhận bằng chứng OCR để chọn phụ đề qua model đã chọn. Khả năng dùng ảnh phụ thuộc model và được kiểm tra riêng; có thể tính phí."
    : "Gemini nhận nội dung OCR và một số ảnh mẫu để chọn phụ đề. Cần khóa Gemini; có thể tính phí. Chọn chế độ này để bật hỗ trợ Gemini.";
  $("#subBody").innerHTML =
    `<section class="quality-overview"><div class="quality-overview-head"><strong>${q.requires_review?"Phụ đề cần xử lý thêm":errors?"Có lỗi cần sửa":"Đã kiểm tra phụ đề"}</strong><span class="pill">${q.count ?? P.segments.length} câu</span></div><p>${q.requires_review?"NPK có thể tự đối chiếu các lớp chữ và kiểm tra phần còn thiếu.":errors?`${errors} lỗi cần sửa trước khi xuất.`:q.reviewed?"Phạm vi của phiên bản hiện tại đã được xác nhận.":"Cấu trúc phụ đề hợp lệ. Xem mẫu để đánh giá cách hiển thị."}</p><label>Cách xử lý<select id="qualitySubtitleAutomation">${[["local","Tự động trên máy"],["gemini","Tự động với Gemini"],["router9","Tự động với 9Router"],["manual","Thủ công · nâng cao"]].map(([value,label])=>`<option value="${value}" ${mode===value?"selected":""}>${label}</option>`).join("")}</select></label><p id="qualityGeminiDisclosure" class="quality-disclosure" ${!["gemini","router9"].includes(mode)?"hidden":""}>${automationDisclosure}</p><div class="row"><button id="autoSubtitles" class="primary" hidden>Tự xử lý phụ đề</button><button id="refreshQuality">Kiểm tra lại</button></div><small id="qualityAutomationStatus" role="status"></small></section>${deliverySummary}<div class="quality-groups">${detailRows || "<p class=\"muted\">Không thấy lỗi cấu trúc trong bảng hiện tại.</p>"}</div><div id="qualityAdvanced"></div>`;
  $("#qualityAdvanced").innerHTML=`<details class="quality-advanced"><summary>Nâng cao · Nhận dạng và xác nhận thủ công</summary><div class="row"><button id="redoExtraction">Nhận dạng lại</button></div><div id="qualityTrackControls"></div>${q.requires_review?'<details class="quality-manual-review"><summary>Xác nhận phạm vi sau khi xem video</summary><label>Lý do xác nhận<textarea id="reviewReason" placeholder="Ví dụ: Đoạn này chỉ có nhạc, không có lời."></textarea></label><button id="acceptReview">Tôi đã xem và xác nhận</button><p class="muted">Xem các đoạn cần đối chiếu trước khi xác nhận. Xác nhận không tạo thêm phụ đề.</p></details>':""}</details>`;
  $("#qualitySubtitleAutomation").onchange=guard(async event=>{
    change(project=>{project.params.subtitle_automation=event.target.value;});
    await save();await renderQuality();
  });
  $("#redoExtraction").onclick = () => extractDialog("ocr");
  // Track choice is an explicit editor action. Loading quality never replaces
  // the current subtitle table or its translations.
  const trackEvidence = await api("/api/studio/projects/" + id + "/ocr-tracks");
  if (P?.id !== id || renderId!==qualityRenderSerial || area !== "subtitles" || subtab !== "quality") return;
  const automatic=mode!=="manual",available=q.automation?.available ?? trackEvidence.available;
  const autoButton=$("#autoSubtitles");autoButton.hidden=!available;
  const active=jobs.some(job=>job.project_id===id&&["queued","running"].includes(job.state));
  autoButton.disabled=!automatic||active;
  $("#qualitySubtitleAutomation").disabled=active;
  $("#qualityAutomationStatus").textContent=active?"Đang có tác vụ xử lý; chờ kết quả trước khi tiếp tục.":q.automation?.summary || (automatic?available?"Tự chọn lớp phụ đề và lưu kết quả. Phần chưa đủ bằng chứng sẽ được báo riêng.":"Nhận dạng OCR trước để NPK có bằng chứng chọn phụ đề.":"Chuyển sang chế độ tự động để NPK xử lý; lựa chọn thủ công nằm trong Nâng cao.");
  autoButton.onclick=guard(async()=>{
    autoButton.disabled=true;
    try {
      await save();
      if(P?.id!==id)throw new Error("Dự án đang mở đã thay đổi.");
      await queue("auto_subtitles",{replace_segments:true});notice("Đang tự chọn phụ đề và kiểm tra phần còn thiếu.");
    }catch(error){autoButton.disabled=false;throw error;}
  });
  if (trackEvidence.available) {
    const trackChoices = trackEvidence.tracks.map((track, index) =>
      `<div class="card"><label class="check"><input type="checkbox" data-ocr-track="${esc(track.id)}" ${track.selected ? "checked" : ""}>Lớp ${index + 1} · ${track.run_count} câu · ${track.lines} dòng</label><p class="muted">${clock(track.start)}–${clock(track.end)}${track.lines > 3 ? " · nhiều dòng, có thể là tài liệu" : ""}</p><details><summary>Xem nội dung nhận dạng</summary>${track.preview.map(run => `<p style="white-space:pre-wrap">${esc(run.text)}</p><button data-ocr-track-time="${run.start}">Xem ${clock(run.start)}</button>`).join("")}</details></div>`).join("");
    $("#qualityTrackControls").innerHTML=`<details id="ocrTrackPicker"><summary>Chọn lớp chữ thủ công · ${trackEvidence.tracks.length} lớp</summary><div><p class="muted">Chọn phụ đề cần dịch hoặc đọc. Các lớp khác được lưu để đối chiếu.</p>${trackChoices}<button id="applyOCRTracks" class="primary">Dùng các lớp đã chọn</button>${trackEvidence.legacy_prefix_rows ? '<p class="warning">Lượt này có phần nhận dạng cũ chưa lưu lớp chữ. Chọn lớp mới chỉ lấy phần có bằng chứng; nhận dạng lại nếu cần đủ toàn video.</p>' : ""}</div></details>`;
    $$('[data-ocr-track-time]').forEach(button => button.onclick = () => seek(+button.dataset.ocrTrackTime));
    const chosenTracks = () => $$('[data-ocr-track]:checked').map(input => input.dataset.ocrTrack);
    const apply = $("#applyOCRTracks");
    $$('[data-ocr-track]').forEach(input => input.onchange = () => apply.disabled = !chosenTracks().length);
    apply.disabled = !chosenTracks().length;
    apply.onclick = guard(() => {
      const trackIds = chosenTracks();
      if (!trackIds.length) throw new Error("Chọn ít nhất một lớp chữ.");
      openModal("Đổi lớp nội dung phụ đề", `<p>Đưa ${trackIds.length} lớp đã chọn vào bảng hiện có ${P.segments.length} câu. Bản trước được lưu trong Khôi phục phiên bản.</p><p class="muted">Bản dịch, giọng và kiểu riêng được giữ cho câu có lời gốc và mốc thời gian giống hệt. Giọng hoặc video cũ chỉ được dùng lại khi còn khớp nội dung mới.</p>`, [
        {label:"Hủy",run:() => closeModal()},
        {label:"Áp dụng lớp đã chọn",primary:true,run:async () => {
          await save();
          if (P?.id !== id) throw new Error("Dự án đang mở đã thay đổi.");
          const version = editVersion;
          const result = await api("/api/studio/projects/" + id + "/ocr-tracks", {
            expected_revision:trackEvidence.expected_revision,source_revision:trackEvidence.source_revision,
            source_signature:trackEvidence.source_signature,job_id:trackEvidence.job_id,
            manifest_key:trackEvidence.manifest_key,track_ids:trackIds});
          if (P?.id !== id) { closeModal(); return; }
          if (dirty || saving || editVersion !== version) {
            conflict = true;
            notice("Đã lưu lựa chọn lớp chữ. Nội dung đang nhập vẫn được giữ; tải lại dự án để đối chiếu.", true);
            closeModal(); return;
          }
          P = result; quality = null; undo = []; redo = []; selected.clear(); page = 0; currentCue = -1;
          $("#saved").textContent = "Đã lưu";
          closeModal(); drawCaption(); renderPanel(); updateUndo();
          notice(`Đã chọn lớp chữ · ${P.segments.length} câu. Bản trước đã lưu trong lịch sử.`);
        }}
      ]);
    });
  } else if (P.coverage?.method === "ocr") {
    $("#qualityTrackControls").innerHTML=`<p class="muted">${esc(trackEvidence.reason)}</p>`;
  }
  $("#refreshQuality").onclick = guard(renderQuality);
  $$(".issue-jump").forEach((b) => (b.onclick = () => seek(+b.dataset.time)));
  $$(".issue-edit").forEach(
    (b) =>
      (b.onclick = () => {
        selected = new Set([+b.dataset.i]);
        currentCue = +b.dataset.i;
        page = Math.floor(currentCue / 40);
        subtab = "content";
        renderPanel();
        seek(P.segments[currentCue].start);
      }),
  );
  if ($("#acceptReview"))
    $("#acceptReview").onclick = guard(async () => {
      P = await api("/api/studio/projects/" + P.id + "/review", {
        signature: q.signature,
        reason: $("#reviewReason").value,
      });
      notice("Đã lưu xác nhận của bạn cho phiên bản phụ đề này.");
      renderPanel();
    });
}
function subtitleDownload() {
  openModal(
    "Xuất tệp phụ đề",
    '<p>SRT giữ nội dung và mốc. ASS giữ thêm kiểu chữ.</p><label class="check"><input id="downloadTarget" type="checkbox" checked>Dùng bản dịch (câu chưa dịch giữ lời gốc)</label>',
    [
      { label: "Đóng", run: () => closeModal() },
      ...["srt", "ass"].map((format) => ({
        label: "Tải " + format.toUpperCase(),
        run: async () => {
          await save();
          const a = document.createElement("a");
          a.href =
            mediaUrl(`/api/projects/${P.id}/subtitles`) +
            `&format=${format}&translated=${$("#downloadTarget").checked}`;
          a.download = "Phu-de." + format;
          a.click();
          closeModal();
        },
      })),
    ],
  );
}
function shiftDialog() {
  openModal(
    "Dịch thời gian phụ đề",
    `<p>Áp dụng ${selected.size ? "cho " + selected.size + " câu chọn" : "toàn bộ bảng"}.</p><label>Dịch bao nhiêu giây? (âm: sớm hơn)<input id="shiftValue" type="number" step=".1" value="0"></label>`,
    [
      { label: "Hủy", run: () => closeModal() },
      {
        label: "Áp dụng",
        primary: true,
        run: () => {
          const offset = Number($("#shiftValue").value),
            ids = selected.size ? [...selected] : P.segments.map((_, i) => i);
          if (
            ids.some(
              (i) =>
                P.segments[i].start + offset < 0 ||
                P.segments[i].end + offset > P.media.duration,
            )
          )
            throw new Error(
              "Dịch mốc sẽ đưa câu ra ngoài video. Giảm độ dịch.",
            );
          change(
            (p) =>
              ids.forEach((i) => {
                p.segments[i] = editorShiftCue(p.segments[i], offset);
              }),
            { render: true },
          );
          closeModal();
        },
      },
    ],
  );
}
function replaceDialog() {
  openModal(
    "Tìm và thay nội dung",
    '<label>Tìm chính xác<input id="findText"></label><label>Thay bằng<input id="replaceValue"></label><label>Phần cần sửa<select id="replaceColumn"><option value="translated">Bản dịch</option><option value="text">Lời gốc</option></select></label><p id="replaceCount" class="info">Chỉ thay trong câu chọn, hoặc toàn bảng nếu chưa chọn câu.</p>',
    [
      { label: "Hủy", run: () => closeModal() },
      {
        label: "Xem số lần khớp",
        run: () => {
          const f = $("#findText").value,
            k = $("#replaceColumn").value;
          let count = 0;
          if (f)
            P.segments.forEach((r, i) => {
              if (!selected.size || selected.has(i))
                count += r[k].split(f).length - 1;
            });
          $("#replaceCount").textContent =
            `Sẽ thay ${count} vị trí. Có thể hoàn tác.`;
        },
      },
      {
        label: "Thay nội dung",
        primary: true,
        run: () => {
          const f = $("#findText").value,
            t = $("#replaceValue").value,
            k = $("#replaceColumn").value;
          if (!f) throw new Error("Nhập nội dung cần tìm.");
          change(
            (p) =>
              p.segments.forEach((r, i) => {
                if (!selected.size || selected.has(i))
                  r[k] = r[k].split(f).join(t);
              }),
            { render: true },
          );
          closeModal();
        },
      },
    ],
  );
}

function extractDialog(method = "ocr", showRegionEditor = false) {
  if (pendingRecognition.has(P.id) || ['queued', 'running'].includes(recognitionWork()?.state)) {
    notice('Đang có lượt nhận dạng. Chờ hoàn tất hoặc mở Chi tiết để hủy.');
    return;
  }
  openModal(
    "Tạo phụ đề tự động",
    `<section class="recognition-start-card"><strong id="recognitionStartTitle">Quét video để tìm phụ đề trên hình</strong><p id="recognitionStartCopy">NPK quét chữ trên toàn khung hình, tự đề xuất lớp phụ đề theo vị trí và thời gian, rồi ghép thành câu có mốc. Nếu nhiều lớp chữ cạnh tranh, NPK giữ bằng chứng và yêu cầu kiểm tra.</p><p class="muted">Nếu video không có phụ đề in sẵn, bạn có thể chọn nghe lời thoại trong phần Nâng cao.</p></section><details id="recognitionAdvanced" ${method === "asr" || showRegionEditor ? "open" : ""}><summary>Nâng cao · cách nhận dạng, ngôn ngữ và phạm vi</summary><div class="recognition-advanced-body"><label>Cách nhận dạng<select id="extractMethod"><option value="ocr">Tự đọc phụ đề trên hình</option><option value="asr">Nghe lời thoại</option></select></label><label data-extract-for="asr">Ngôn ngữ lời thoại<select id="extractLanguage">${[
      ["auto", "Tự nhận biết"],
      ["zh", "Tiếng Trung"],
      ["vi", "Tiếng Việt"],
      ["en", "Tiếng Anh"],
      ["ja", "Tiếng Nhật"],
      ["ko", "Tiếng Hàn"],
    ]
      .map(
        ([v, n]) =>
          `<option value="${v}" ${P.params.source_language === v ? "selected" : ""}>${n}</option>`,
      )
      .join(
        "",
      )}</select></label><div class="two"><label>Từ giây<input id="extractStart" type="number" value="0" min="0" step=".1"></label><label>Đến giây<input id="extractEnd" type="number" value="${P.media.duration}" min="0" step=".1"></label></div><button id="readWholeVideo" class="link">Đặt lại toàn bộ video</button><div id="extractionRangeHint" class="info"></div><div data-extract-for="asr"><label>Thiết bị nghe lời thoại<select id="extractDevice"><option value="cpu">Bộ xử lý · giảm tải</option><option value="cuda" ${P.params.asr_device === "cuda" ? "selected" : ""}>Card NVIDIA tương thích</option></select></label><label>Độ lớn mô hình nhận giọng<select id="extractModel">${["tiny", "base", "small", "medium", "large-v3"].map((v) => `<option ${v === P.params.asr_model ? "selected" : ""}>${v}</option>`).join("")}</select></label><p class="muted">Mô hình nhận giọng còn thiếu có thể được tải về kho trên D.</p></div><div data-extract-for="ocr"><label>Số ảnh đọc chữ mỗi giây<input id="extractFps" type="number" min=".2" max="8" step=".2" value="${P.params.ocr_fps}"></label><label>Xử lý vùng đọc chữ<select id="extractScale"><option value="native">Giữ kích thước vùng · giảm tải</option><option value="legacy">Phóng lớn vùng · thử khi chữ rất nhỏ</option></select></label><p class="muted">Quét toàn khung có thể chậm hơn. Chỉ tăng mật độ lấy ảnh khi chữ xuất hiện rất ngắn.</p></div><details id="readingRegionAdvanced" data-extract-for="ocr"><summary>Giới hạn vùng đọc (tùy chọn)</summary><div><p class="muted">Mặc định NPK tìm chữ trên toàn khung hình. Chỉ khoanh vùng nếu video có nhiều chữ nền hoặc muốn quét nhanh một khu vực cụ thể.</p><div id="readRegionList"></div><div class="row"><button id="drawReadingRegion">Vẽ vùng trên video</button><button id="copyReadRegion">Sao chép vùng từ Chữ gốc</button><button id="clearReadRegion">Bỏ vùng riêng</button></div></div></details></div></details><div class="warning">${P.segments.length ? `Kết quả nhận dạng sẽ thay bảng ${P.segments.length} câu hiện tại, kể cả bản dịch. Phiên bản cũ được giữ để khôi phục. ` : ""}Sau khi đọc xong, xem lại kết quả rồi mới dịch hoặc xuất.</div>`,
    [
      { label: "Hủy", run: () => closeModal() },
      {
        label: method === "asr" ? "Nghe lời thoại và tạo phụ đề" : "Quét video và tạo phụ đề",
        primary: true,
        run: async () => {
          const kind = $("#extractMethod").value,
            start = +$("#extractStart").value,
            end = +$("#extractEnd").value;
          if (
            !Number.isFinite(start) ||
            !Number.isFinite(end) ||
            !$("#extractStart").value ||
            !$("#extractEnd").value ||
            start < 0 ||
            end <= start ||
            end > P.media.duration
          )
            throw new Error("Khoảng nhận dạng phải nằm trong video.");
          if (
            kind === "ocr" &&
            (!Number.isFinite(+$("#extractFps").value) ||
              +$("#extractFps").value < 0.2 ||
              +$("#extractFps").value > 8)
          )
            throw new Error("Số ảnh đọc chữ mỗi giây phải từ 0,2 đến 8.");
          change((p) =>
            Object.assign(p.params, {
              source_language: $("#extractLanguage").value,
              ...(kind === "asr"
                ? {
                    asr_device: $("#extractDevice").value,
                    asr_model: $("#extractModel").value,
                  }
                : {
                    ocr_fps: +$("#extractFps").value,
                    ocr_scale_mode: $("#extractScale").value,
                  }),
              extraction_start: start,
              extraction_end: end,
            }),
          );
          await save();
          closeModal();
          await queue(kind, { replace_segments: true });
        },
      },
    ],
  );
  $("#drawReadingRegion").onclick = () => {
    closeModal();
    subtab = "content";
    setArea("subtitles");
    drawMode = true;
    drawRegions();
    notice("Giữ chuột và kéo để khoanh vùng phụ đề trên video. Thả để lưu, Esc để hủy.");
  };
  const renderReading = () => {
    $("#readRegionList").innerHTML =
      readingRegions()
        .map(
          (r, i) =>
            `<fieldset class="card"><legend>Vùng đọc ${i + 1}</legend><div class="two">${[
              ["x", "Trái", 0, P.media.width, 1],
              ["y", "Trên", 0, P.media.height, 1],
              ["w", "Rộng", 1, P.media.width, 1],
              ["h", "Cao", 1, P.media.height, 1],
              ["start", "Bắt đầu (giây)", 0, P.media.duration, 0.1],
              ["end", "Kết thúc (giây)", 0, P.media.duration, 0.1],
            ]
              .map(
                ([key, label, min, max, step]) =>
                  `<label>${label}<input type="number" data-reading="${i}" data-region-key="${key}" aria-label="${label} vùng đọc ${i + 1}" min="${min}" max="${max}" step="${step}" value="${Number(r[key] ?? (key === "end" ? P.media.duration : 0))}"></label>`,
              )
              .join(
                "",
              )}</div><button data-remove-reading="${i}">Xóa vùng ${i + 1}</button></fieldset>`,
        )
        .join("") || '<p class="muted">Chưa có vùng riêng.</p>';
    updateExtractionHint();
    $$("[data-reading]").forEach(
      (input) =>
        (input.onchange = () => {
          const i = +input.dataset.reading,
            key = input.dataset.regionKey;
          const r = { ...readingRegions()[i], [key]: +input.value };
          if (
            !input.value ||
            !Number.isFinite(r[key]) ||
            r.x < 0 ||
            r.y < 0 ||
            r.w < 1 ||
            r.h < 1 ||
            r.x + r.w > P.media.width ||
            r.y + r.h > P.media.height ||
            r.start < 0 ||
            r.end <= r.start ||
            r.end > P.media.duration
          ) {
            input.value = readingRegions()[i][key];
            notice(
              "Vùng phải nằm trong hình và có thời gian bắt đầu trước kết thúc.",
              true,
            );
            return;
          }
          change((p) => {
            p.extraction_regions = structuredClone(readingRegions());
            p.extraction_regions[i] = r;
          });
          drawRegions();
          updateExtractionHint();
        }),
    );
    $$("[data-remove-reading]").forEach(
      (button) =>
        (button.onclick = () => {
          change((p) => {
            p.extraction_regions = structuredClone(readingRegions());
            p.extraction_regions.splice(+button.dataset.removeReading, 1);
          });
          renderReading();
          drawRegions();
        }),
    );
  };
  renderReading();
  $("#copyReadRegion").onclick = () => {
    change((p) => (p.extraction_regions = structuredClone(p.regions)));
    renderReading();
    notice("Đã sao chép vùng che sang vùng đọc riêng.");
    drawRegions();
  };
  $("#extractScale").value = P.params.ocr_scale_mode || "native";
  $("#clearReadRegion").onclick = () => {
    change((p) => (p.extraction_regions = []));
    renderReading();
    notice("Đã bỏ vùng giới hạn; lần nhận dạng tới sẽ tìm chữ trên toàn khung hình.");
    drawRegions();
  };
  function updateExtractionHint() {
    const ocr = $("#extractMethod").value === "ocr";
    const start = +$("#extractStart").value,
      end = +$("#extractEnd").value;
    const limited =
      ocr &&
      readingRegions().some(
        (r) => (+r.start || 0) > start || +(r.end ?? P.media.duration) < end,
      );
    $("#extractionRangeHint").textContent = ocr
      ? `Đọc chữ trong hình từ ${clock(start)} đến ${clock(end)}. ${readingRegions().length ? `${readingRegions().length} vùng riêng đang được chọn.` : "NPK tìm chữ trên toàn khung hình và xếp lớp theo vị trí, thời gian."}${limited ? " Một vùng chỉ hoạt động trong một phần thời gian đã chọn." : ""}`
      : `Nghe lời thoại từ ${clock(start)} đến ${clock(end)}. Chữ chỉ xuất hiện trên hình sẽ không được lấy bằng cách này.`;
    $("#extractionRangeHint").className = limited ? "warning" : "info";
  }
  const updateMethod = () => {
    const kind = $("#extractMethod").value;
    $$("[data-extract-for]").forEach((el) => {
      el.hidden = el.dataset.extractFor !== kind;
    });
    $("#modalTitle").textContent =
      "Tạo phụ đề tự động";
    $("#recognitionStartTitle").textContent = kind === "ocr"
      ? "Quét video để tìm phụ đề trên hình"
      : "Nghe lời thoại để tạo phụ đề";
    $("#recognitionStartCopy").textContent = kind === "ocr"
      ? "NPK quét chữ trên toàn khung hình, tự đề xuất lớp phụ đề theo vị trí và thời gian rồi ghép thành câu có mốc. Nếu nhiều lớp chữ cạnh tranh, NPK giữ bằng chứng và yêu cầu kiểm tra."
      : "NPK nhận dạng lời nói bằng bộ Whisper trên máy, tự nhận biết ngôn ngữ khi để mặc định và giữ mốc thời gian. Không gửi âm thanh lên dịch vụ dịch.";
    $("#modalActions button.primary").textContent = kind === "asr"
      ? "Nghe lời thoại và tạo phụ đề" : "Quét video và tạo phụ đề";
    updateExtractionHint();
  };
  $("#extractMethod").value = method === "asr" ? "asr" : "ocr";
  $("#extractLanguage").value = P.params.source_language || "auto";
  $("#extractMethod").onchange = updateMethod;
  $("#extractStart").oninput = updateExtractionHint;
  $("#extractEnd").oninput = updateExtractionHint;
  $("#readWholeVideo").onclick = () => {
    $("#extractStart").value = 0;
    $("#extractEnd").value = P.media.duration;
    updateExtractionHint();
  };
  $("#recognitionAdvanced").addEventListener("toggle", () => {
    if (showRegionEditor && $("#recognitionAdvanced").open) $("#readingRegionAdvanced").open = true;
  });
  if (showRegionEditor) $("#readingRegionAdvanced").open = true;
  updateMethod();
}
function translationDialog() {
  if (!P.segments.length) {
    notice(
      "Chưa có câu để dịch. Đọc chữ trên hình, nghe lời thoại hoặc nhập tệp phụ đề trước.",
    );
    return;
  }
  openModal(
    "Dịch phụ đề",
    `<div class="info">Nội dung phụ đề và hướng dẫn sẽ gửi tới nhà cung cấp bạn chọn. Có thể tính phí; không tự đổi tài khoản hay mô hình khi hết hạn mức.</div><label>Phạm vi<select id="translateScope"><option value="missing">Câu chưa dịch</option><option value="all">Toàn bộ bảng</option><option value="selected" ${selected.size ? "" : "disabled"}>${selected.size} câu đã chọn</option></select></label><label>Nhà cung cấp<select id="translateProvider"><option value="gemini" ${P.params.provider === "gemini" ? "selected" : ""}>Gemini</option><option value="deepseek" ${P.params.provider === "deepseek" ? "selected" : ""}>DeepSeek</option><option value="router9" ${P.params.provider === "router9" ? "selected" : ""}>9Router</option></select></label><label id="translateRouter9Endpoint" ${P.params.provider === "router9" ? "" : "hidden"}>Địa chỉ API 9Router<input id="translateBaseUrl" value="${esc(P.params.router9_base_url || router9DefaultBaseUrl)}"></label><label>Mô hình đã chọn<input id="translateModel" value="${esc(P.params.translation_model)}"></label><label>Ngôn ngữ đích<input id="translateTarget" value="${esc(P.params.target_language)}"></label><label>Văn phong<textarea id="translateStyle">${esc(P.params.style)}</textarea></label><details><summary>Tên riêng và cách dịch</summary><div><label>Từ điển tên riêng<textarea id="translateGlossary">${esc(P.params.glossary)}</textarea></label></div></details>`,
    [
      {
        label: "Kết nối dịch vụ",
        run: () => {
          closeModal();
          settingsTab = "connections";
          setArea("settings");
        },
      },
      { label: "Hủy", run: () => closeModal() },
      {
        label: "Dịch phụ đề",
        primary: true,
        run: async () => {
          const scope = $("#translateScope").value;
          change((p) =>
            Object.assign(p.params, {
              provider: $("#translateProvider").value,
              translation_model: $("#translateModel").value,
              ...($("#translateProvider").value === "router9" ? {router9_base_url: $("#translateBaseUrl").value.trim()} : {}),
              translation_scope: scope,
              target_language: $("#translateTarget").value,
              style: $("#translateStyle").value,
              glossary: $("#translateGlossary").value,
            }),
          );
          await save();
          closeModal();
          await queue("translate", {
            replace_segments: true,
            ...(scope === "selected" ? { segment_indices: [...selected] } : {}),
          });
        },
      },
    ],
  );
  const { summary, gap } = subtitleSourceSummary(P);
  $("#modalBody").insertAdjacentHTML(
    "afterbegin",
    `<div id="translationSourceNotice" class="${gap ? "warning" : "info"}"><strong>${esc(summary)}</strong><p>Chỉ dịch nội dung trong bảng hiện tại; không tự đọc thêm chữ từ khung hình hay nghe thêm lời thoại.</p>${gap ? `<p>Đoạn ${clock(gap[0])}–${clock(gap[1])} chưa có câu. Nếu còn chữ trên hình, hãy đọc lại trước khi dịch. Khoảng trống cũng có thể là đoạn không có phụ đề.</p>` : ""}<button id="readFramesBeforeTranslate">Đọc chữ trên hình trước</button></div>`,
  );
  $("#translateProvider").onchange = () => { $("#translateRouter9Endpoint").hidden = $("#translateProvider").value !== "router9"; };
  $("#readFramesBeforeTranslate").onclick = () => extractDialog("ocr");
}
async function queue(kind, extra = {}) {
  if (activeProjectPipeline()) throw new Error("Video đang có quy trình xử lý. Chờ hoàn tất hoặc hủy trong Tiến trình trước khi chạy thêm.");
  const projectId = P.id;
  // Capture the click, not wherever playback has reached after save/preflight.
  const requestedTime = video.currentTime || 0;
  if (["preview", "clip"].includes(kind)) video.pause();
  const method = recognitionMethod({kind, configuration:{pipeline_extract:P.params.pipeline_extract}});
  const recognizing = ['ocr', 'asr'].includes(method);
  if (recognizing) {
    if (pendingRecognition.has(projectId) || ['queued', 'running'].includes(recognitionWork()?.state))
      throw new Error('Đang có lượt nhận dạng. Chờ hoàn tất hoặc mở Chi tiết để hủy.');
    pendingRecognition.set(projectId, method);
    updateRecognitionUI();
  }
  try {
    await save();
    if (P?.id !== projectId) throw new Error('Dự án đang mở đã đổi. Chọn lại tác vụ cho đúng dự án.');
    const payload = {
      project_id: projectId,
      kind,
      time: requestedTime,
      expected_revision: P.updated,
      request_id: crypto.randomUUID(),
      ...extra,
    };
    const check = await api("/api/preflight", { project_id: projectId, kind });
    if (!check.ok) throw new Error(check.errors.join("\n"));
    const job = await api("/api/jobs", payload);
    jobs = [job, ...jobs.filter(j => j.id !== job.id)];
    if (recognizing) pendingRecognition.delete(projectId);
    renderProgress();
    lastDetail = job.id;
    refreshProjectLock();
    notice(kind === "clip" ? "Đã xếp mẫu thử tối đa 3 giây. Gần cuối video, mẫu sẽ lùi lại để đủ thời lượng." : kind === "pipeline" ? "Đã bắt đầu quy trình. Cấu hình được giữ nguyên trong lúc xử lý." : "Đã xếp tác vụ. Bạn vẫn có thể xem video và chỉnh dự án.");
    await poll();
    // Keep the user's expanded/collapsed choice; loading a job must not open it.
    if (!$("#progressDetails").hidden) await showJob(job.id);
    return job;
  } finally {
    if (recognizing) pendingRecognition.delete(projectId);
    updateRecognitionUI();
  }
}
async function continuePausedPipeline(identifier) {
  if (activeProjectPipeline()) throw new Error("Video đang có quy trình xử lý. Chờ hoàn tất hoặc hủy trước khi chạy tiếp.");
  const projectId = P?.id;
  if (!projectId) throw new Error("Mở dự án của quy trình cần tiếp tục trước.");
  await save();
  const version = editVersion, revision = P?.updated;
  const plan = await api("/api/studio/jobs/" + identifier + "/continuation");
  if (P?.id !== projectId || dirty || saving || editVersion !== version || P.updated !== revision)
    throw new Error("Cấu hình vừa thay đổi. Lưu và kiểm tra lại trước khi tiếp tục.");
  if (plan.project_id !== projectId || plan.expected_revision !== revision)
    throw new Error("Quy trình không còn khớp dự án đang mở. Tải lại trước khi tiếp tục.");
  if (!plan.can_continue) {
    if (plan.requires_review) { subtab = "quality"; setArea("subtitles"); setProgressExpanded(false); }
    throw new Error((plan.errors || []).join("\n") || plan.reason || "Chưa thể tiếp tục các bước còn lại.");
  }
  const job = await api("/api/studio/jobs/" + identifier + "/continue-pipeline", {
    expected_revision: revision, request_id: crypto.randomUUID(),
  });
  jobs = [job, ...jobs.filter(row => row.id !== job.id)];
  lastDetail = job.id;
  renderProgress(); refreshProjectLock();
  await poll();
  if (!$("#progressDetails").hidden) await showJob(job.id);
  return job;
}
function cleanSlider(key, label, min, max, step = 1, unit = "px", scale = 1) {
  const value = Number(P.params[key]);
  return `<label class="clean-slider">${label}<output>${Math.round(value * scale)} ${unit}</output><input class="param" type="range" data-key="${key}" aria-label="${label}" min="${min}" max="${max}" step="${step}" value="${value}" data-unit="${unit}" data-scale="${scale}" title="${label}"></label>`;
}
function cleanControls() {
  const method = P.params.method;
  const strength = method === "blur" ? cleanSlider("blur_radius", "Làm mờ", 2, 64)
    : method === "stretch" ? cleanSlider("stretch_height", "Dải nền", 2, 100)
    : method === "telea" ? cleanSlider("inpaint_radius", "Bán kính", 1, 9)
    : method === "box" ? field("cover_color", "Màu che", "color")
    : cleanSlider("dilate", "Nới vùng", 0, 32);
  return `<div class="clean-controls">${strength}${cleanSlider("cover_opacity", "Độ đậm", 0, 1, .05, "%", 100)}${cleanSlider("edge_feather", "Mềm viền", 0, 32)}</div>
    ${method === "stretch" ? `<div class="clean-stretch-options">${field("stretch_direction", "Lấy nền từ", "select", [["above","Phía trên"],["below","Phía dưới"]])}${cleanSlider("stretch_offset", "Khoảng cách", 0, 200)}</div>` : ""}`;
}
function markCleanPreviewStale() {
  for (const id of ["#compareConfig", "#frameCompareConfig", "#cleanReadyStatus"]) {
    const el = $(id);
    if (el) {
      const noVideo = id === "#cleanReadyStatus" && !P.artifacts?.clean;
      el.textContent = noVideo
        ? "Chưa có video đã xử lý · Xem thử trước khi xuất."
        : "Thông số đã đổi · Kết quả thuộc lần trước.";
      el.className = id === "#cleanReadyStatus"
        ? "clean-ready-status " + (noVideo ? "info" : "warning")
        : "clean-artifact-status " + (noVideo ? "muted" : "warning");
      el.title = "Vùng hoặc phương pháp đã thay đổi. Xem lại ảnh hoặc mẫu 3 giây để kiểm tra thông số mới trước khi xử lý và xuất video.";
    }
  }
}
function renderCleanArtifacts() {
  const ready = $("#cleanReadyStatus"), art = P.artifacts || {};
  ready.textContent = art.clean ? "Đang kiểm tra video đã xử lý…" : "Chưa có video đã xử lý · Xem thử trước khi xuất.";
  ready.title = "Bản xem trực tiếp chỉ giúp điều chỉnh vùng. Ảnh và mẫu 3 giây dùng bộ xử lý thật; hãy kiểm tra trước khi xử lý toàn video.";
  if (art.clean) {
    ready.insertAdjacentHTML("afterend", '<div class="row clean-artifact-actions"><button id="showCleanVideo">Video đã xử lý</button><button id="showOriginalVideo">Video nguồn</button></div>');
    $("#showCleanVideo").onclick = () => { $("#viewSource").value = "clean"; $("#viewSource").onchange(); };
    $("#showOriginalVideo").onclick = () => { cleanLive.enabled = false; $("#viewSource").value = "source"; $("#viewSource").onchange(); };
  }
  const sample = art.preview;
  if (sample?.files?.includes('after.jpg')) {
    const src = file => mediaUrl('/media/job/' + sample.job_id + '/' + file);
    $("#frameCompare").innerHTML = `<details class="clean-preview-result" open><summary>Ảnh xem thử · ${clock(sample.time || 0)}</summary><div class="row"><button id="frameBefore" aria-pressed="false">Trước</button><button id="frameAfter" class="active" aria-pressed="true">Sau</button><button id="frameEnlarge" title="Phóng to ảnh để kiểm tra chi tiết vùng chữ">Phóng to</button></div><img id="frameComparisonImage" class="clean-sample-image" alt="Khung hình sau xử lý" src="${src('after.jpg')}"><p id="frameCompareConfig" class="clean-artifact-status muted" title="Mẫu một khung dùng bộ xử lý thật, chưa đánh giá được độ ổn định cả video.">Đang đối chiếu thông số…</p></details>`;
    const show = after => {
      const img = $("#frameComparisonImage");
      img.src = src(after ? 'after.jpg' : 'before.jpg');
      img.alt = after ? 'Khung hình sau xử lý' : 'Khung hình trước xử lý';
      for (const [id, on] of [["#frameBefore", !after],["#frameAfter", after]]) {
        $(id).classList.toggle('active',on); $(id).setAttribute('aria-pressed',on);
      }
    };
    $("#frameBefore").onclick = () => show(false);
    $("#frameAfter").onclick = () => show(true);
    $("#frameEnlarge").onclick = () => openModal('So sánh khung hình · ' + clock(sample.time || 0),
      `<img id="largeCleanFrame" class="clean-sample-image" alt="Khung hình so sánh" src="${src('after.jpg')}">`,[
        {label:'Trước xử lý',run:()=>$("#largeCleanFrame").src=src('before.jpg')},
        {label:'Sau xử lý',run:()=>$("#largeCleanFrame").src=src('after.jpg')},
        {label:'Đóng',run:()=>closeModal()},
      ]);
  }
  const id = P.id, version = editVersion;
  guard(async () => {
    const status = await api('/api/studio/projects/' + id + '/artifacts-status');
    if (P?.id !== id || area !== 'clean' || version !== editVersion || ready !== $("#cleanReadyStatus")) return;
    if (art.clean) {
      const current = status.clean_ready && !dirty;
      ready.textContent = current ? 'Video xử lý khớp thông số hiện tại.' : 'Video thuộc lần trước · Thông số đã đổi.';
      ready.className = 'clean-ready-status ' + (current ? 'info' : 'warning');
    }
    if ($("#frameCompareConfig")) $("#frameCompareConfig").textContent = status.preview_current && !dirty
      ? 'Ảnh khớp thông số đã lưu.'
      : 'Thông số đã đổi · Cần xem thử lại.';
  })();
}
function renderClean() {
  panel.innerHTML = `<div class="clean-workspace"><div class="panel-title clean-panel-heading"><h2>Xử lý chữ gốc</h2><span class="grow"></span><button id="cleanHelp" class="clean-help" title="Hướng dẫn thao tác và chọn phương pháp" aria-label="Hướng dẫn xử lý chữ gốc">?</button></div><section class="clean-method-section" aria-label="Phương pháp xử lý">${field(
    "method",
    "Phương pháp",
    "select",
    [
      ["blur", "Làm mờ · nhẹ"],
      ["box", "Hộp màu · nhẹ"],
      ["stretch", "Kéo nền · nhẹ"],
      ["telea", "Lấp nền · tùy cảnh"],
      ["lama", "Phục hồi AI · nặng"],
    ],
  )}${cleanControls()}<details class="clean-advanced"><summary>Tùy chỉnh nâng cao</summary><div class="two">${P.params.method !== "lama" ? field("dilate", "Nới vùng (px)") : ""}${P.params.method === "lama" ? field("max_side", "Giới hạn ảnh AI (px)") : ""}${["telea","lama"].includes(P.params.method) ? field("mask_mode", "Phạm vi phục hồi", "select", [["rectangle","Toàn bộ vùng"],["glyph","Nét chữ sáng"]]) : ""}</div></details></section><section class="clean-regions-section" aria-label="Vùng xử lý"><div class="clean-section-heading"><h3>Vùng xử lý</h3><span id="regionSelectionCount" class="clean-count">${P.regions.length} vùng</span><span class="grow"></span><button data-region-command="selectAll" class="clean-text-button" title="Chọn tất cả vùng · Ctrl+A">Chọn tất cả</button></div><div class="row clean-region-create"><button id="drawRegion" class="${drawMode ? "active" : ""}" aria-pressed="${drawMode}" title="Vẽ một vùng mới trên video · N">${icon("plus")}${drawMode ? "Đang vẽ" : "Thêm vùng"}</button><button id="guessRegion" title="Tìm các vùng chữ trên khung hình hiện tại">Gợi ý tự động</button><button id="copyRegionsForClean" title="Sao chép vùng nhận dạng phụ đề làm vùng xử lý chữ">Từ vùng đọc</button></div><div id="regionList" class="clean-region-list" aria-label="Danh sách vùng">${
    P.regions
      .map(
        (r, i) =>
          `<button type="button" data-select-region="${i}" class="clean-region-row${selectedRegionIndices().includes(i) ? " selected" : ""}" aria-pressed="${selectedRegionIndices().includes(i)}" title="Chọn vùng ${i + 1} · Ctrl hoặc Shift để chọn nhiều"><span class="clean-region-number">${i + 1}</span><span class="clean-region-name">Vùng ${i + 1}<small>${Math.round(r.w)} × ${Math.round(r.h)} px</small></span><span class="clean-region-time">${clock(r.start)}–${clock(r.end)}</span></button>`,
      )
      .join("") ||
    '<div class="clean-region-empty"><span>Chưa có vùng xử lý</span><button data-start-region-drawing>Vẽ vùng trên video</button></div>'
  }</div><div class="row clean-region-actions" aria-label="Thao tác với vùng đã chọn"><button data-region-command="duplicate" title="Nhân bản vùng đã chọn · Ctrl+D" disabled>Nhân bản</button><button data-region-command="merge" title="Gộp các vùng đã chọn thành một vùng bao quanh" disabled>Gộp vùng</button><button data-region-command="delete" title="Xóa các vùng đã chọn · Delete" disabled>${icon("close")}Xóa</button></div><div id="regionInspector"></div></section><section class="clean-preview-section" aria-label="Xem thử và xử lý"><div class="clean-section-heading"><h3>Xem thử</h3><span class="grow"></span><small title="Các tác vụ xem thử sử dụng vị trí đang dừng trên video">Tại vị trí đang xem</small></div><div class="row clean-preview-actions"><button id="previewFrame" title="Xử lý một khung hình bằng phương pháp và vùng hiện tại">Xem ảnh</button><button id="previewClean" title="Xử lý một mẫu video tối đa 3 giây để kiểm tra chuyển động">Thử 3 giây</button></div><div id="cleanReadyStatus" class="clean-ready-status info" role="status"></div><div id="frameCompare"></div><div id="cleanResult"></div><div class="clean-finish">${field("use_clean", "Dùng bản xử lý khi xuất", "check")}<button id="cleanAll" class="primary">Xử lý toàn video</button></div></section></div>`;
  $$(".clean-slider input").forEach(el => el.oninput = () => {
    el.parentElement.querySelector("output").textContent = Math.round(Number(el.value)*Number(el.dataset.scale)) + " " + el.dataset.unit;
    markCleanPreviewStale();
  });
  $("#previewFrame").onclick = guard(() => queue("preview"));
  renderCleanArtifacts();
  $("#drawRegion").onclick = startRegionDrawing;
  $$('[data-start-region-drawing]').forEach(button => button.onclick = startRegionDrawing);
  $$('[data-select-region]').forEach(button => button.onclick = event => selectRegion(
    +button.dataset.selectRegion, event.ctrlKey || event.metaKey || event.shiftKey, {seek: true},
  ));
  $$('[data-region-command]').forEach(button => button.onclick = guard(() => regionCommand(button.dataset.regionCommand)));
  updateRegionInspector();
  $("#cleanHelp").onclick = () => openModal("Vùng chữ và cách xử lý", `<div class="clean-help-content"><h3>Điều chỉnh trực tiếp trên video</h3><p>Bấm <strong>Thêm vùng</strong> rồi kéo để vẽ. Bấm một vùng để chọn, kéo phần giữa để di chuyển hoặc kéo các điểm ở cạnh và góc để đổi kích thước. Ctrl hoặc Shift chọn nhiều vùng; chuột phải mở các thao tác nhanh.</p><p>Vùng đang chọn có thời gian bắt đầu và kết thúc riêng. Mục <strong>Tọa độ</strong> cho phép nhập vị trí và kích thước chính xác. Hoàn tác bằng Ctrl+Z.</p><h3>Chọn phương pháp</h3><p><strong>Làm mờ, hộp màu và kéo nền</strong> nhẹ hơn. Với kéo nền, dải lấy mẫu phải không có chữ; sát mép video, hệ thống có thể thử phía còn lại.</p><p><strong>Lấp nền và phục hồi AI</strong> xử lý từng khung, có thể chậm hoặc bệt nền, nhấp nháy khi vùng lớn và chuyển cảnh. Chế độ nét chữ sáng có thể nhầm nền sáng hoặc sót viền chữ.</p><h3>Kiểm tra trước khi xuất</h3><p>Độ đậm 100% che rõ nhất. Giảm độ đậm hoặc tăng mềm viền có thể làm chữ lộ lại. Vẽ vùng rộng hơn chữ, kiểm tra <strong>Xem ảnh</strong> và <strong>Thử 3 giây</strong> trước khi xử lý toàn video.</p><p>Bản xem trực tiếp giúp chỉnh vùng. Ảnh và mẫu video dùng bộ xử lý thật; một khung chưa phản ánh độ ổn định của cả video. Khi thông số đổi, kết quả cũ được đánh dấu để bạn xem thử lại.</p></div>`, [{label: "Đã hiểu", primary: true, run: () => closeModal()}]);
  $("#guessRegion").onclick = guard(() => queue("detect"));
  $("#copyRegionsForClean").onclick = guard(async () => {
    if (!readingRegions().length)
      return notice(
        "Chưa có vùng đọc riêng. Vẽ vùng ở Phụ đề → Đọc chữ trên hình trước.",
      );
    if (
      await ask(
        "Sao chép vùng đọc để xử lý chữ?",
        "Thay các vùng che hiện tại bằng vùng đọc và khoảng thời gian tương ứng. Bản che cũ được giữ nhưng không còn khớp cấu hình mới; hãy xem thử trước khi xử lý toàn video.",
        "Sao chép",
      )
    ) {
      const regions = structuredClone(readingRegions());
      change((p) => (p.regions = regions), { render: true });
      drawRegions();
    }
  });
  $("#previewClean").onclick = guard(() => queue("clip"));
  $("#cleanAll").onclick = guard(async () => {
    if (
      await ask(
        "Xử lý chữ toàn video?",
        P.params.method === "lama"
          ? "Phục hồi nền bằng AI có thể chạy lâu và làm máy nóng. Nên xem thử trước; chế độ Êm giữ chất lượng nhưng chạy chậm hơn."
          : "Tác vụ tạo một bản video mới, giữ nguyên video gốc.",
        "Bắt đầu",
      )
    )
      await queue("clean");
  });
  if (P.artifacts?.clip) {
    const a = P.artifacts.clip;
    $("#cleanResult").innerHTML =
      `<details class="clean-preview-result" open><summary>Mẫu trước / sau</summary><div class="two"><div><small>Trước xử lý</small><video id="compareBefore" muted preload="metadata" style="width:100%" src="${sourceMediaUrl()}"></video></div><div><small>Sau xử lý</small><video id="compareAfter" muted controls preload="auto" style="width:100%" src="${mediaUrl("/media/job/" + a.job_id + "/clean.mp4")}"></video></div></div><p id="compareConfig" class="clean-artifact-status muted" title="Phát video bên phải để xem hai khung cùng thời điểm.">Đang đối chiếu thông số của mẫu…</p></details>`;
    guard(async () => {
      const id = P.id,
        [j, status] = await Promise.all([
          api("/api/studio/jobs/" + a.job_id),
          api("/api/studio/projects/" + id + "/artifacts-status"),
        ]);
      if (P?.id !== id || area !== "clean" || !$("#compareAfter")) return;
      const before = $("#compareBefore"),
        after = $("#compareAfter"),
        start = j.result?.start ?? a.start ?? j.time ?? 0,
        duration = j.result?.duration ?? a.duration ?? Math.min(3, P.media.duration - start);
      const sync = () => {
        if (Math.abs(before.currentTime - (start + after.currentTime)) > 0.08)
          before.currentTime = start + after.currentTime;
      };
      before.onloadedmetadata = () => (before.currentTime = start);
      if (before.readyState >= 1) before.currentTime = start;
      after.onplay = () => {
        sync();
        before.play().catch(() => {});
      };
      after.onpause = () => before.pause();
      after.ontimeupdate = sync;
      after.onseeking = sync;
      after.onended = () => before.pause();
      $("#compareConfig").textContent =
        `${clock(start)}–${clock(start + duration)} · ${status.clip_current ? "Khớp thông số." : "Thông số đã đổi."}${Math.abs(start - (j.time || 0)) > .1 ? " Mẫu lùi để đủ thời lượng." : ""}`;
    })();
  }
}
function renderTextToSpeech() {
  standaloneSpeechOutputKey = "";
  panel.innerHTML = `<div class="tts-studio"><div class="panel-title"><h2>Tạo giọng từ văn bản</h2><span class="grow"></span><span class="pill">Xử lý trên máy</span></div><p class="muted">Soạn lời đọc, chọn giọng và xuất âm thanh. Bạn có thể sử dụng mục này mà không cần nhập video.</p><div class="tts-layout"><section class="tts-compose card" aria-label="Soạn lời đọc"><div class="row"><h3>Nội dung lời đọc</h3><span class="grow"></span><small id="ttsTextCount"></small></div><label for="ttsText" class="sr-only">Văn bản cần tạo giọng</label><textarea id="ttsText" maxlength="10000" rows="12" placeholder="Nhập lời đọc tiếng Việt tại đây…">${esc(standaloneSpeech.text)}</textarea><p class="muted">Bản nháp được lưu trên máy này. Tối đa 10.000 ký tự mỗi lượt.</p><div id="ttsExpressionTools" hidden><p class="muted">Chèn thẻ biểu cảm hoặc khoảng nghỉ tại con trỏ. Thẻ được nhập thủ công; hệ thống không tự phân tích cảm xúc.</p><div class="row tts-tags">${[["[cười]", "Cười"], ["[thở dài]", "Thở dài"], ["[hắng giọng]", "Hắng giọng"], ["[nghỉ 1s]", "Nghỉ 1 giây"]].map(([tag, label]) => `<button type="button" data-tts-tag="${esc(tag)}">${esc(label)}</button>`).join("")}</div></div><div class="two"><label for="ttsVoice">Giọng đọc<select id="ttsVoice" aria-describedby="ttsVoiceDescription"></select></label><label for="ttsSpeed">Tốc độ đọc (0,5–2)<input id="ttsSpeed" type="number" min="0.5" max="2" step="0.05" value="${esc(standaloneSpeech.speed)}"></label></div><p id="ttsVoiceDescription" class="muted"></p><p id="ttsLocalStatus" class="info" role="status"></p><div class="row"><button id="ttsGenerate" class="primary" type="button">Tạo âm thanh</button><button id="ttsRefreshVoices" type="button">Làm mới giọng</button><small>Ctrl+Enter để tạo giọng</small></div></section><section class="tts-results" aria-label="Kết quả tạo giọng"><div class="card"><h3>Âm thanh của bạn</h3><p id="ttsJobStatus" role="status">Âm thanh sẽ xuất hiện tại đây sau khi tạo.</p><progress id="ttsJobProgress" max="1" value="0" aria-label="Tiến trình tạo giọng" hidden></progress><div id="ttsOutput"></div><div class="row"><button id="ttsCancel" type="button" hidden>Hủy tác vụ</button><button id="ttsJobDetails" type="button" hidden>Xem chi tiết</button></div></div><div class="card"><h3>Viết để nghe tự nhiên hơn</h3><p>Dùng câu rõ nghĩa và dấu câu để tạo nhịp. Với đoạn dài, chia thành các đoạn văn theo ý. Nghe lại trước khi đưa vào video.</p><p class="muted">VieNeu hỗ trợ thẻ biểu cảm và khoảng nghỉ. Piper đọc trên CPU và không xử lý các thẻ này. Tốc độ được áp dụng cho cả bài; mục tạo giọng riêng không ép lời theo mốc phụ đề.</p></div><div id="ttsRecent" class="card" hidden></div></section></div></div>`;
  $("#ttsExpressionTools").insertAdjacentHTML("afterbegin", `<label class="check"><input id="ttsExpressive" type="checkbox" ${standaloneSpeech.expressive ? "checked" : ""}>Đọc tiếng cười theo ngữ cảnh · VieNeu</label><small class="muted">Chuyển Hì hì / Ha ha thành tiếng cười; không tự suy đoán cảm xúc hoặc tạo tiếng hét.</small>`);
  $("#ttsExpressive").onchange = event => {
    standaloneSpeech.expressive = event.target.checked;
    storePreference("npk-tts-expressive", standaloneSpeech.expressive);
  };
  $("#ttsText").oninput = () => {
    standaloneSpeech.text = $("#ttsText").value;
    storePreference("npk-tts-text", standaloneSpeech.text);
    updateTextToSpeechControls();
  };
  $("#ttsText").onkeydown = (event) => {
    if (event.ctrlKey && event.key === "Enter") {
      event.preventDefault();
      if (!$("#ttsGenerate").disabled) $("#ttsGenerate").click();
    }
  };
  $("#ttsVoice").onchange = () => {
    standaloneSpeech.voiceId = $("#ttsVoice").value;
    storePreference("npk-tts-voice", standaloneSpeech.voiceId);
    updateTextToSpeechControls();
  };
  $("#ttsSpeed").oninput = () => {
    const speed = Number($("#ttsSpeed").value);
    if (Number.isFinite(speed) && speed >= .5 && speed <= 2) {
      standaloneSpeech.speed = speed;
      storePreference("npk-tts-speed", speed);
    }
    updateTextToSpeechControls();
  };
  $$('[data-tts-tag]').forEach((button) => {
    button.onclick = guard(() => {
      const editor = $("#ttsText"), tag = button.dataset.ttsTag + " ";
      if (editor.value.length - (editor.selectionEnd - editor.selectionStart) + tag.length > 10000)
        throw new Error("Nội dung đã đạt giới hạn 10.000 ký tự.");
      editor.setRangeText(tag, editor.selectionStart, editor.selectionEnd, "end");
      editor.dispatchEvent(new Event("input", { bubbles: true }));
      editor.focus();
    });
  });
  $("#ttsGenerate").onclick = guard(async () => {
    if (standaloneSpeechSubmitting) return;
    const text = $("#ttsText").value.trim(), speed = Number($("#ttsSpeed").value);
    const voice = (catalog?.voices || []).find((v) => v.id === standaloneSpeech.voiceId);
    if (!text || text.length > 10000) throw new Error("Nhập lời đọc từ 1 đến 10.000 ký tự.");
    if (!voice?.installed || voice.available === false) throw new Error("Giọng đã chọn chưa sẵn sàng trên máy. Làm mới giọng và kiểm tra bộ xử lý.");
    if (!Number.isFinite(speed) || speed < .5 || speed > 2) throw new Error("Tốc độ đọc phải nằm trong khoảng 0,5–2.");
    standaloneSpeechSubmitting = true;
    updateTextToSpeechControls();
    try {
      const job = await api("/api/jobs", {
        kind: "text_to_speech", sample_text: text, request_id: crypto.randomUUID(),
        params: { voice_id: voice.id, voice_speed: speed, ...window.NPKUpgrade?.ttsParams(), tts_expressive: standaloneSpeech.expressive },
        ...window.NPKUpgrade?.speechPayload(),
      });
      standaloneSpeech.jobId = job.id;
      standaloneSpeechDetail = job;
      storePreference("npk-tts-job", job.id);
      jobs = [job, ...jobs.filter((j) => j.id !== job.id)];
      lastDetail = job.id;
      renderProgress();
      updateTextToSpeechResult(job);
      notice("Đã xếp tác vụ tạo giọng. Bạn có thể tiếp tục soạn bản nháp.");
      await refreshStandaloneSpeechJob();
    } finally {
      standaloneSpeechSubmitting = false;
      updateTextToSpeechControls();
    }
  });
  $("#ttsRefreshVoices").onclick = guard(async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    button.textContent = "Đang làm mới…";
    try {
      catalog = await api("/api/catalog?refresh_voices=true");
      if (area === "text_to_speech") updateTextToSpeechVoices();
    } finally {
      button.disabled = false;
      button.textContent = "Làm mới giọng";
    }
  });
  $("#ttsCancel").onclick = guard(async () => {
    if (!standaloneSpeech.jobId) return;
    await api("/api/jobs/" + encodeURIComponent(standaloneSpeech.jobId) + "/cancel", {});
    await refreshStandaloneSpeechJob();
  });
  $("#ttsJobDetails").onclick = guard(async () => {
    if (!standaloneSpeech.jobId) return;
    setProgressExpanded(true);
    await showJob(standaloneSpeech.jobId);
  });
  updateTextToSpeechVoices();
  updateTextToSpeechResult(standaloneSpeechDetail);
  guard(refreshStandaloneSpeechJob)();
}
function updateTextToSpeechVoices() {
  if (area !== "text_to_speech" || !$("#ttsVoice")) return;
  const voices = (catalog?.voices || []).filter((v) => ["piper", "vieneu"].includes(v.provider) && v.installed);
  if (!standaloneSpeech.voiceId) {
    const first = voices.find((v) => v.provider === "vieneu" && v.available !== false) || voices.find((v) => v.available !== false);
    if (first) {
      standaloneSpeech.voiceId = first.id;
      storePreference("npk-tts-voice", first.id);
    }
  }
  $("#ttsVoice").innerHTML = voices.map((v) => `<option value="${esc(v.id)}" ${v.id === standaloneSpeech.voiceId ? "selected" : ""} ${v.available === false ? "disabled" : ""}>${esc(v.name)} · ${v.provider === "vieneu" ? "VieNeu" : "Piper"}${v.available === false ? " · chưa sẵn sàng" : ""}</option>`).join("");
  if (!voices.some((v) => v.id === standaloneSpeech.voiceId))
    $("#ttsVoice").insertAdjacentHTML("afterbegin", `<option value="${esc(standaloneSpeech.voiceId)}" selected disabled>${catalog ? "Chọn một giọng đã cài trên máy" : "Đang đọc thư viện giọng…"}</option>`);
  updateTextToSpeechControls();
}
function updateTextToSpeechControls() {
  if (area !== "text_to_speech" || !$("#ttsGenerate")) return;
  const voice = (catalog?.voices || []).find((v) => v.id === standaloneSpeech.voiceId);
  const isVieNeu = voice?.provider === "vieneu", available = !!voice?.installed && voice.available !== false;
  $("#ttsExpressionTools").hidden = !isVieNeu;
  $("#ttsTextCount").textContent = $("#ttsText").value.length.toLocaleString("vi-VN") + " / 10.000 ký tự";
  $("#ttsVoiceDescription").textContent = voice ? (isVieNeu ? "VieNeu · tích hợp trong NPK" + voiceSampleRateLabel(voice.sample_rate) : "Piper · CPU") + (voiceDescription(voice) ? " · " + voiceDescription(voice) : "") : "";
  const status = $("#ttsLocalStatus");
  status.className = available ? "info" : "warning";
  status.textContent = available
    ? "Tạo giọng hoàn toàn trên máy, không gửi văn bản ra Internet. NPK tự vận hành bộ xử lý; không cần mở ứng dụng giọng khác."
    : voice?.unavailable_reason || catalog?.voice_providers?.vieneu?.message || (catalog ? "Chưa có giọng sẵn sàng. Kiểm tra bộ xử lý trong Cài đặt, rồi làm mới giọng." : "Đang đọc thư viện giọng trên máy…");
  const speed = Number($("#ttsSpeed").value);
  $("#ttsGenerate").disabled = standaloneSpeechSubmitting || !available || !$("#ttsText").value.trim() || !Number.isFinite(speed) || speed < .5 || speed > 2;
  $("#ttsGenerate").textContent = standaloneSpeechSubmitting ? "Đang xếp tác vụ…" : "Tạo âm thanh";
}
function updateTextToSpeechResult(job) {
  if (area !== "text_to_speech" || !$("#ttsJobStatus")) return;
  const active = job && ["queued", "running"].includes(job.state);
  const stateLabel = job ? stateNames[job.state] || job.state : "";
  const message = safeMessage(job?.message);
  $("#ttsJobStatus").textContent = job ? stateLabel + (message && message !== stateLabel ? " · " + message : "") : "Âm thanh sẽ xuất hiện tại đây sau khi tạo.";
  $("#ttsJobStatus").className = job?.state === "error" ? "error" : "";
  $("#ttsJobProgress").hidden = !active;
  $("#ttsJobProgress").value = Math.max(0, Math.min(1, Number(job?.progress) || 0));
  $("#ttsCancel").hidden = !active;
  $("#ttsJobDetails").hidden = !job;
  const outputKey = job ? job.id + ":" + job.state + ":" + Boolean(job.storage_removed) : "";
  if (outputKey !== standaloneSpeechOutputKey) {
    standaloneSpeechOutputKey = outputKey;
    const base = job ? "/media/job/" + encodeURIComponent(job.id) + "/" : "";
    $("#ttsOutput").innerHTML = job?.state === "done" && !job.storage_removed
      ? `<audio id="ttsAudio" controls preload="metadata" src="${esc(mediaUrl(base + "speech.wav"))}"></audio><div class="row tts-downloads"><a class="tts-download" href="${esc(mediaUrl(base + "speech.wav"))}" download="NPK-Giong-doc.wav">${icon("download")}Tải WAV</a><a class="tts-download" href="${esc(mediaUrl(base + "speech.mp3"))}" download="NPK-Giong-doc.mp3">${icon("download")}Tải MP3</a></div><p class="muted">WAV giữ chất lượng gốc; MP3 gọn hơn để chia sẻ.</p>`
      : job?.storage_removed ? '<p class="warning">Tệp âm thanh của tác vụ đã được dọn. Có thể tạo lại từ bản nháp.</p>' : "";
  }
  const recent = jobs.filter((j) => j.kind === "text_to_speech").slice(0, 6);
  $("#ttsRecent").hidden = !recent.length;
  $("#ttsRecent").innerHTML = '<h3>Các lượt tạo gần đây</h3>' + recent.map((j) => `<button class="tts-recent-job" data-tts-job="${esc(j.id)}" aria-pressed="${j.id === standaloneSpeech.jobId}">${esc(stateNames[j.state] || j.state)}${j.created ? " · " + esc(new Date(j.created * 1000).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" })) : ""}</button>`).join("");
  $$('[data-tts-job]').forEach((button) => {
    button.onclick = guard(async () => {
      standaloneSpeech.jobId = button.dataset.ttsJob;
      standaloneSpeechDetail = jobs.find((j) => j.id === standaloneSpeech.jobId);
      storePreference("npk-tts-job", standaloneSpeech.jobId);
      updateTextToSpeechResult(standaloneSpeechDetail);
      await refreshStandaloneSpeechJob();
    });
  });
}
async function refreshStandaloneSpeechJob() {
  if (area !== "text_to_speech" || standaloneSpeechPollBusy) return;
  if (!standaloneSpeech.jobId) {
    updateTextToSpeechResult(null);
    return;
  }
  const id = standaloneSpeech.jobId;
  standaloneSpeechPollBusy = true;
  try {
    const job = await api("/api/studio/jobs/" + encodeURIComponent(id));
    if (standaloneSpeech.jobId !== id || area !== "text_to_speech") return;
    standaloneSpeechDetail = job;
    updateTextToSpeechResult(job);
  } catch (error) {
    if (standaloneSpeech.jobId === id && area === "text_to_speech") {
      if (error.status === 404) {
        standaloneSpeechDetail = { id, state: "error", storage_removed: true, message: "Không còn tác vụ đã lưu. Bản nháp vẫn được giữ để tạo lại." };
        updateTextToSpeechResult(standaloneSpeechDetail);
      } else $("#ttsJobStatus").textContent = "Chưa đọc được tiến trình. NPK sẽ thử lại khi kết nối sẵn sàng.";
    }
  } finally {
    standaloneSpeechPollBusy = false;
  }
}
function renderVoice() {
  const p = P.params;
  panel.innerHTML = `<div class="panel-title"><h2>Giọng &amp; âm thanh</h2></div><label>Nguồn lồng tiếng<select id="voiceSource"><option value="local" ${!p.voice_path ? "selected" : ""}>Giọng trong máy</option><option value="external" ${p.voice_path ? "selected" : ""}>Tệp âm thanh của bạn</option></select></label><div id="voiceSourceBody"></div><details><summary>Âm lượng và phối âm</summary><div>${field("original_gain", "Âm lượng nguồn (0–2)")}${field("voice_gain", "Âm lượng lồng tiếng (0–2)")}${field("bgm_gain", "Âm lượng nhạc nền (0–2)")}<label>Nhạc nền<input id="bgmPath" value="${esc(p.bgm_path)}" placeholder="Chưa chọn nhạc riêng"></label><div class="row"><button id="chooseBgm">Chọn nhạc</button><button id="clearBgm">Bỏ nhạc riêng</button></div>${field("voice_offset", "Dịch thời gian giọng ngoài (giây)")}<p class="warning">Âm nguồn có thể chứa lời gốc. Giữ âm nguồn không có nghĩa là tách riêng nhạc. Đặt âm nguồn bằng 0 nếu không muốn hai giọng chồng nhau.</p><button id="previewMix">${mixEnabled ? "Tắt nghe phối âm" : "Nghe phối âm cùng video"}</button></div></details>`;
  $("#voiceSource").parentElement.childNodes[0].textContent = "Cách tạo giọng";
  panel.querySelector(".panel-title").insertAdjacentHTML("afterend", field("tts_text_source", "Nội dung lồng tiếng", "select", [
    ["translated", "Bản dịch"], ["edited", "Lời đã biên tập"], ["source", "Phụ đề gốc"],
  ]));
  const textChoice = panel.querySelector('[data-key="tts_text_source"]');
  textChoice.value = p.tts_text_source || "edited";
  textChoice.title = "Bản dịch: chỉ đọc bản dịch. Lời đã biên tập: ưu tiên lời đọc riêng rồi bản dịch. Phụ đề gốc: chỉ đọc lời gốc.";
  textChoice.parentElement.insertAdjacentHTML("afterend", '<p id="voiceSpeechSourceHint" class="muted speech-source-hint" role="status"></p><p id="voiceSpeechArtifactStatus" class="muted speech-source-hint" role="status">Đang kiểm tra bản giọng đã tạo…</p>');
  textChoice.parentElement.insertAdjacentHTML('afterend', '<button id="reviewVoiceDialogue">Kiểm tra lời đọc</button>');
  $("#reviewVoiceDialogue").onclick = guard(dialogueReviewDialog);
  updateSpeechSourceHints();
  guard(updateVoiceSpeechState)();
  $("#voiceSource").onchange = () => {
    if ($("#voiceSource").value === "local")
      change((p) => (p.params.voice_path = ""), { render: true });
    else renderExternalVoice();
  };
  if (p.voice_path) renderExternalVoice();
  else renderLocalVoices();
  $("#chooseBgm").onclick = guard(() => pickAudio("bgm_path"));
  $("#clearBgm").onclick = () =>
    change((p) => (p.params.bgm_path = ""), { render: true });
  $("#bgmPath").onchange = guard(async (e) => {
    const result = await api("/api/projects/" + P.id + "/audio/inspect", {
      path: e.target.value,
    });
    change((p) => (p.params.bgm_path = result.path || e.target.value));
  });
  $("#previewMix").onclick = guard(toggleMix);
}
function renderLocalVoices() {
  const body = $("#voiceSourceBody");
  const timingMode = P.params.tts_timing_mode || "legacy";
  body.innerHTML = `<div class="row"><input id="voiceSearch" type="search" aria-label="Tìm giọng đọc" placeholder="Tìm tên giọng" value="${esc(voiceQuery)}"><button id="refreshVoiceCatalog" type="button">Làm mới giọng</button></div><p id="voiceProviderStatus" class="info" role="status"></p><div class="voice-grid" id="voiceGrid"></div><div id="voicePages"></div><div class="two section-end">${field("voice_speed", "Tốc độ đọc mặc định (0,5–2)")}${field("voice_gain", "Âm lượng lồng tiếng (0–2)")}</div><label for="voiceTimingMode">Cách khớp lời đọc<select id="voiceTimingMode" class="param" data-key="tts_timing_mode" aria-describedby="voiceTimingHint"><option value="natural" ${timingMode === "natural" ? "selected" : ""}>Ưu tiên nhịp tự nhiên</option><option value="legacy" ${timingMode === "legacy" ? "selected" : ""}>Khớp nhanh theo cách cũ</option></select></label>${field("tts_auto_schedule", "Tự bố trí lời đọc trong khoảng trống, giữ mốc phụ đề", "check")}<p id="voiceTimingHint" class="info" role="status"></p><button id="editSpokenCues" class="link">Chỉnh lời đọc và khoảng nghỉ từng câu</button>${field("tts_require_translation", "Chỉ đọc lời đã hoàn thiện (bản dịch hoặc lời đọc riêng)", "check")}<button id="generateVoice" class="primary">Tạo lồng tiếng</button><p id="voiceEngineHint" class="muted"></p><details><summary>Câu nghe thử</summary><div><label>Nội dung mẫu<textarea id="sampleText" maxlength="400">${esc(localStorage.getItem("npk-sample-text") || "Xin chào, đây là giọng đọc thử của NPK Auto Sub.")}</textarea></label></div></details><p id="voicePreviewStatus" class="info">Bấm icon loa cạnh tên giọng để nghe; không đổi lựa chọn của video.</p>`;
  $("#voiceTimingHint").insertAdjacentHTML("afterend", field("tts_expressive", "Đọc tiếng cười theo ngữ cảnh · VieNeu", "check") + '<small class="muted">Chuyển Hì hì / Ha ha thành tiếng cười. Không tự đoán cảm xúc hoặc tạo tiếng hét; đổi lựa chọn cần tạo lại lồng tiếng.</small>');
  body.insertAdjacentHTML(
    "afterbegin",
    `<div class="two"><label>Áp dụng giọng cho<select id="voiceScope"><option value="all">Giọng mặc định toàn video</option><option value="selected" ${selected.size ? "" : "disabled"}>${selected.size} câu đã chọn</option></select></label><label class="check"><input id="favoriteOnly" type="checkbox">Chỉ giọng yêu thích</label></div><button id="clearCueVoice" class="link">Bỏ giọng riêng của câu chọn</button>`,
  );
  $("#voiceScope").value =
    voiceScope === "selected" && selected.size ? "selected" : "all";
  $("#voiceScope").onchange = (e) => {
    voiceScope = e.target.value;
    voiceCards();
  };
  $("#favoriteOnly").onchange = voiceCards;
  $("#clearCueVoice").onclick = () => {
    if (!selected.size)
      return notice("Chọn câu trong Phụ đề → Nội dung trước.");
    change((p) => selected.forEach((i) => delete p.segments[i].voice_id));
    voiceCards();
    notice("Các câu đã chọn dùng lại giọng mặc định.");
  };
  $("#voiceSearch").oninput = (e) => {
    voiceQuery = e.target.value;
    voicePage = 0;
    voiceCards();
  };
  $("#refreshVoiceCatalog").onclick = guard(async (e) => {
    const button = e.currentTarget;
    button.disabled = true;
    button.textContent = "Đang làm mới…";
    try {
      catalog = await api("/api/catalog?refresh_voices=true");
      if (area === "voice" && $("#voiceGrid")) voiceCards();
      notice("Đã làm mới thư viện và trạng thái giọng. Lựa chọn của video được giữ nguyên.");
    } finally {
      button.disabled = false;
      button.textContent = "Làm mới giọng";
    }
  });
  $("#sampleText").onchange = (e) =>
    storePreference("npk-sample-text", e.target.value);
  $("#editSpokenCues").onclick = () => {
    subtab = "content";
    setArea("subtitles");
  };
  $("#generateVoice").onclick = guard(async () => {
    if (P.params.voice_gain <= 0) change((p) => (p.params.voice_gain = 1));
    await queue("tts");
  });
  voiceCards();
  bindParams();
  updateVoiceTimingHint();
}
function updateVoiceTimingHint() {
  const hint = $("#voiceTimingHint");
  if (!hint) return;
  const natural = P.params.tts_timing_mode === "natural";
  hint.className = natural ? "info" : "warning";
  hint.textContent = P.params.tts_auto_schedule
    ? "Đo đủ mọi câu rồi bố trí lời đọc theo thứ tự trong video, không chồng lời, không cắt nội dung; giữ mốc phụ đề. Căn nhanh tối đa 1,25× khi cần."
    : natural
    ? "Giữ nhịp đã chọn và khoảng nghỉ; chỉ tăng thêm tối đa 1,15 lần để khớp câu. Lời quá dài cần rút gọn hoặc kéo dài mốc câu trước khi tạo giọng."
    : "Cách cũ có thể tăng tốc mạnh để vừa mốc câu, khiến lời vội và khó nghe. Chuyển sang ưu tiên nhịp tự nhiên khi muốn kiểm soát lời và khoảng nghỉ.";
}
function voiceSampleRateLabel(rate) {
  const value = Number(rate);
  return Number.isFinite(value) && value > 0 ? " · " + (value / 1000).toLocaleString("vi-VN") + " kHz" : "";
}
function voiceDescription(voice) {
  return String(voice.description || "").trim() || [
    { male: "Nam", female: "Nữ" }[String(voice.gender || "").toLowerCase()],
    { north: "Bắc", central: "Trung", south: "Nam" }[String(voice.region || "").toLowerCase()],
  ].filter(Boolean).join(" · ");
}
function voiceCards() {
  if (!catalog) {
    $("#voiceGrid").innerHTML = "<p>Đang đọc thư viện giọng…</p>";
    return;
  }
  const favorites = new Set(
    JSON.parse(localStorage.getItem("npk-favorite-voices") || "[]"),
  );
  const chosenVoice =
    voiceScope === "selected" && selected.size
      ? P.segments[[...selected][0]]?.voice_id || P.params.voice_id
      : P.params.voice_id;
  const vieNeuVoices = catalog.voices.filter((v) => v.provider === "vieneu");
  const vieNeuStatus = catalog.voice_providers?.vieneu;
  const providerHint = $("#voiceProviderStatus");
  if (providerHint) {
    const ready = vieNeuStatus ? vieNeuStatus.available === true : vieNeuVoices.some((v) => v.available === true);
    providerHint.className = ready ? "info" : "warning";
    const label = "VieNeu · tích hợp trong NPK" + voiceSampleRateLabel(vieNeuStatus?.sample_rate);
    const device = String(vieNeuStatus?.device || "").toLowerCase().split(":")[0];
    const deviceLabel = { cuda: "GPU", cpu: "CPU" }[device] || "bộ xử lý cục bộ";
    providerHint.textContent = label + (ready ? " đã sẵn sàng. " : " chưa sẵn sàng. ") +
      (ready ? "NPK tự vận hành engine và giọng đã cài, không cần mở ứng dụng khác hay kết nối Internet. Piper chạy riêng trên CPU." : String(vieNeuStatus?.message || "Kiểm tra bộ xử lý trong Cài đặt, rồi bấm Làm mới giọng. Piper chạy riêng trên CPU.")) +
      (ready && vieNeuStatus?.device ? " VieNeu đang dùng: " + deviceLabel + "." : "");
  }
  const engineHint = $("#voiceEngineHint");
  if (engineHint) engineHint.textContent = String(chosenVoice).startsWith("vieneu:")
    ? "VieNeu tạo giọng trực tiếp trong NPK. Có thể thử [cười], [thở dài], [hắng giọng] hoặc [nghỉ 1s] trong Lời đọc riêng từng câu. Các thẻ được nhập thủ công, không tự phân tích cảm xúc video; giữ phụ đề hiển thị riêng. Câu không đổi được tái dùng từ bộ nhớ đệm."
    : "Piper tổng hợp offline trên CPU và chỉ nạp giọng được dùng. Câu không đổi được tái sử dụng từ bộ nhớ đệm. Piper chưa xử lý các thẻ cảm xúc của VieNeu hoặc tự bắt chước ngữ điệu video.";
  const generate = $("#generateVoice");
  if (generate) {
    const ids = new Set(P.segments.length
      ? P.segments.map((row) => row.voice_id || P.params.voice_id)
      : [P.params.voice_id]);
    const unavailable = catalog.voices.find((v) => ids.has(v.id) && v.available === false);
    generate.disabled = Boolean(unavailable);
    generate.title = unavailable
      ? String(unavailable.unavailable_reason || "Bộ xử lý giọng đã chọn chưa sẵn sàng trên máy. Kiểm tra Cài đặt rồi làm mới giọng.")
      : "";
  }
  const list = catalog.voices.filter(
    (v) =>
      v.installed &&
      (!$("#favoriteOnly")?.checked || favorites.has(v.id)) &&
      [v.name, v.gender, v.region, v.description, v.provider === "vieneu" ? "VieNeu" : "Piper CPU"]
        .filter(Boolean).join(" ").toLocaleLowerCase().includes(voiceQuery.toLocaleLowerCase()),
  );
  voicePage = Math.min(voicePage, Math.max(0, Math.ceil(list.length / 12) - 1));
  $("#voiceGrid").innerHTML = list
    .slice(voicePage * 12, (voicePage + 1) * 12)
    .map((v) => {
      const available = v.available !== false;
      const provider = v.provider === "vieneu" ? "VieNeu · tích hợp trong NPK" + voiceSampleRateLabel(v.sample_rate) : "Piper · CPU";
      const description = voiceDescription(v);
      const reason = available ? "" : v.unavailable_reason || "Kiểm tra bộ xử lý trong Cài đặt rồi làm mới thư viện giọng.";
      const auditionLabel = available ? "Nghe thử " + v.name : "Chưa thể nghe thử " + v.name + ": " + reason;
      return `<div class="voice-card ${chosenVoice === v.id ? "selected" : ""}"><button class="choose" data-voice="${esc(v.id)}" aria-pressed="${chosenVoice === v.id}" ${available ? "" : "disabled"} title="${esc(reason || description || provider)}"><span>${esc(v.name)}</span><small>${esc(provider)}</small>${description ? `<small>${esc(description)}</small>` : ""}${!available ? `<small>Chưa sẵn sàng · kiểm tra bộ xử lý</small>` : ""}</button>${ib("", "star", "Yêu thích " + v.name, `data-favorite="${esc(v.id)}" aria-pressed="${favorites.has(v.id)}"`)}${ib("", previewVoice === v.id && !$("#audition").paused ? "pause" : "volume", auditionLabel, `data-audition="${esc(v.id)}" ${available ? "" : "disabled"}`)}</div>`;
    })
    .join("");
  if (!list.length) {
    const favoriteOnly = $("#favoriteOnly")?.checked;
    $("#voiceGrid").innerHTML = `<div class="voice-empty-state" role="status"><strong>${favoriteOnly && !favorites.size ? "Chưa có giọng yêu thích" : "Không tìm thấy giọng phù hợp"}</strong><small>${favoriteOnly ? "Bấm ngôi sao cạnh một giọng để thêm vào danh sách yêu thích." : "Thử bỏ từ tìm kiếm hoặc làm mới thư viện giọng."}</small><button id="showAllVoices">Hiện tất cả giọng</button></div>`;
    $("#showAllVoices").onclick = () => { $("#favoriteOnly").checked = false; voiceQuery = ""; $("#voiceSearch").value = ""; voicePage = 0; voiceCards(); };
  }
  $("#voicePages").innerHTML =
    `<div class="pagination"><button id="voicesPrev" ${voicePage === 0 ? "disabled" : ""}>Trang trước</button><small>${list.length} giọng · ${voicePage + 1}/${Math.max(1, Math.ceil(list.length / 12))}</small><button id="voicesNext" ${(voicePage + 1) * 12 >= list.length ? "disabled" : ""}>Trang sau</button></div>`;
  $("#voicesPrev").onclick = () => {
    voicePage--;
    voiceCards();
  };
  $("#voicesNext").onclick = () => {
    voicePage++;
    voiceCards();
  };
  $$("[data-voice]").forEach(
    (b) =>
      (b.onclick = () => {
        if (b.disabled) return;
        change((p) => {
          if (voiceScope === "selected" && selected.size)
            selected.forEach((i) => (p.segments[i].voice_id = b.dataset.voice));
          else p.params.voice_id = b.dataset.voice;
          if (p.params.voice_gain <= 0) p.params.voice_gain = 1;
        });
        $$('.param[data-key="voice_gain"]').forEach(
          (input) => (input.value = P.params.voice_gain),
        );
        voiceCards();
      }),
  );
  $$("[data-favorite]").forEach(
    (b) =>
      (b.onclick = () => {
        const id = b.dataset.favorite;
        favorites.has(id) ? favorites.delete(id) : favorites.add(id);
        storePreference(
          "npk-favorite-voices",
          JSON.stringify([...favorites]),
        );
        voiceCards();
      }),
  );
  $$("[data-audition]").forEach(
    (b) =>
      (b.onclick = guard(async () => {
        if (b.disabled) return;
        if (previewVoice === b.dataset.audition && !$("#audition").paused) {
          $("#audition").pause();
          voiceCards();
          return;
        }
        $("#audition").pause();
        previewVoice = b.dataset.audition;
        const result = await api("/api/jobs", {
          kind: "voice_preview",
          params: {
            voice_id: previewVoice,
            voice_speed: P.params.voice_speed,
            workload_mode: P.params.workload_mode,
            tts_expressive: !!P.params.tts_expressive,
          },
          sample_text:
            $("#sampleText")?.value ||
            localStorage.getItem("npk-sample-text") ||
            "Xin chào, đây là giọng đọc thử của NPK Auto Sub.",
        });
        previewJob = result.id;
        if ($("#voicePreviewStatus"))
          $("#voicePreviewStatus").textContent = "Đang chuẩn bị mẫu nghe thử…";
        if (result.state === "done") await playSample(result);
        else await poll();
      })),
  );
}
async function playSample(job) {
  previewJob = null;
  $("#audition").src = mediaUrl("/media/job/" + job.id + "/sample.wav");
  try {
    await $("#audition").play();
    if ($("#voicePreviewStatus"))
      $("#voicePreviewStatus").textContent =
        "Đang phát mẫu giọng. Bấm lại icon loa để dừng.";
  } catch {
    notice("Mẫu đã sẵn sàng. Bấm loa lần nữa để phát.");
  }
  if (area === "voice" && $("#voiceGrid")) voiceCards();
}
$("#audition").onended = () => {
  if ($("#voicePreviewStatus"))
    $("#voicePreviewStatus").textContent = "Đã phát xong mẫu giọng.";
  if ($("#voiceGrid")) voiceCards();
};
function renderExternalVoice() {
  $("#voiceSourceBody").innerHTML =
    `<label>Tệp lồng tiếng<input id="externalVoicePath" value="${esc(P.params.voice_path)}" placeholder="Chọn WAV, MP3 hoặc M4A trên máy"></label><div class="row"><button id="pickExternalVoice" class="primary">Chọn âm thanh</button><button id="inspectExternalVoice">Kiểm tra tệp</button></div><p class="info">Dùng giọng tạo từ dịch vụ khác: tải âm thanh về máy rồi chọn tại đây. Không giả lập kết nối trực tiếp với dịch vụ chưa tích hợp.</p><div id="externalAudioInfo"></div>`;
  $("#pickExternalVoice").onclick = guard(() => pickAudio("voice_path"));
  $("#inspectExternalVoice").onclick = guard(async () => {
    const path = $("#externalVoicePath").value,
      r = await api("/api/projects/" + P.id + "/audio/inspect", { path });
    change((p) => {
      p.params.voice_path = path;
      p.params.voice_gain = 1;
    });
    $("#externalAudioInfo").textContent =
      `Âm thanh dài ${clock(r.duration)}. ${(r.warnings || []).join(" ") || "Đã đọc được tệp. Nghe cùng video để kiểm tra độ khớp lời."}`;
  });
}
async function pickAudio(key) {
  let path;
  if (window.pywebview?.api?.pick_audio)
    path = await window.pywebview.api.pick_audio();
  else {
    openModal(
      "Chọn âm thanh",
      '<label>Đường dẫn đầy đủ trên máy<input id="pickAudioPath" placeholder="D:\\Audio\\voice.wav"></label>',
      [
        { label: "Hủy", run: () => closeModal() },
        {
          label: "Chọn tệp",
          primary: true,
          run: async () => {
            await setAudio(key, $("#pickAudioPath").value);
            closeModal();
          },
        },
      ],
    );
    return;
  }
  if (path) await setAudio(key, path);
}
async function setAudio(key, path) {
  const result = await api("/api/projects/" + P.id + "/audio/inspect", {
    path,
  });
  change(
    (p) => {
      p.params[key] = path;
      if (key === "voice_path") p.params.voice_gain = 1;
    },
    { render: true },
  );
  if (result.warnings?.length) notice(result.warnings.join("\n"));
}
let audioContext = null;
const mixGains = new Map();
function setMixGain(element, value) {
  if (!audioContext) audioContext = new AudioContext();
  if (!mixGains.has(element)) {
    const source = audioContext.createMediaElementSource(element),
      gain = audioContext.createGain();
    source.connect(gain).connect(audioContext.destination);
    mixGains.set(element, gain);
  }
  element.volume = 1;
  mixGains.get(element).gain.value = Math.max(
    0,
    Math.min(2, Number(value) || 0),
  );
}
function stopMix() {
  mixEnabled = false;
  for (const id of ["mixVoice", "mixBgm"]) {
    const a = $("#" + id);
    a.pause();
    a.removeAttribute("src");
    a.load();
  }
  if (mixGains.has(video)) mixGains.get(video).gain.value = 1;
  video.volume = 1;
}
async function toggleMix() {
  if ($("#viewSource").value === "final")
    throw new Error(
      "Chọn Video nguồn hoặc Video đã xử lý chữ trước khi nghe phối âm; bản thành phẩm đã chứa âm thanh.",
    );
  mixEnabled = !mixEnabled;
  if (mixEnabled) {
    await save();
    const a = P.artifacts?.tts;
    if (!P.params.voice_path && P.params.voice_gain > 0) {
      const status = await api(
        "/api/studio/projects/" + P.id + "/artifacts-status",
      );
      if (!status.speech_ready) {
        mixEnabled = false;
        throw new Error(
          "Giọng chưa có hoặc không còn khớp nội dung. Tạo lại lồng tiếng trước khi nghe phối âm.",
        );
      }
    }
    const voice = P.params.voice_path
      ? mediaUrl("/media/external/" + P.id + "/voice")
      : a
        ? mediaUrl("/media/job/" + a.job_id + "/voice.wav")
        : null;
    if (P.params.voice_gain > 0 && !voice) {
      mixEnabled = false;
      throw new Error(
        "Chưa có giọng để nghe phối âm. Tạo hoặc chọn tệp giọng trước.",
      );
    }
    $("#mixVoice").removeAttribute("src");
    $("#mixBgm").removeAttribute("src");
    if (voice) $("#mixVoice").src = voice;
    if (P.params.bgm_path)
      $("#mixBgm").src = mediaUrl("/media/external/" + P.id + "/bgm");
    setMixGain(video, P.params.original_gain);
    await audioContext.resume();
    syncMix(true);
  } else {
    stopMix();
  }
  renderPanel();
}
function syncMix(force = false) {
  if (!mixEnabled || !P) return;
  setMixGain(video, P.params.original_gain);
  for (const [id, gain, offset] of [
    ["mixVoice", P.params.voice_gain, P.params.voice_offset || 0],
    ["mixBgm", P.params.bgm_gain, 0],
  ]) {
    const a = $("#" + id);
    if (!a.getAttribute("src")) continue;
    let t = video.currentTime - offset;
    setMixGain(a, gain);
    a.loop = id === "mixBgm";
    if (a.loop && Number.isFinite(a.duration) && a.duration > 0)
      t = t % a.duration;
    if (!a.loop && t >= a.duration) {
      a.pause();
      continue;
    }
    if (t < 0 || video.paused) {
      a.pause();
      continue;
    }
    if (force || Math.abs(a.currentTime - t) > 0.25) a.currentTime = t;
    if (a.paused && !video.paused) a.play().catch(() => {});
  }
}
function renderExport() {
  panel.innerHTML = `<div class="panel-title"><h2>Xuất video</h2></div><p class="muted">Chọn những phần cần có. Hệ thống tái dùng kết quả còn phù hợp và chỉ tạo phần còn thiếu.</p><div class="card">${field("burn_subtitles", "Gắn phụ đề lên video", "check")}<label class="check"><input id="wantVoice" type="checkbox" ${P.params.voice_gain > 0 ? "checked" : ""}>Có lồng tiếng</label>${field("use_clean", "Xử lý chữ gốc", "check")}${field("pipeline_translate", "Dịch các câu còn thiếu trước khi tạo giọng / xuất", "check")}</div><div class="two">${field("export_name", "Tên video thành phẩm", "text")}${field("video_quality", "Chất lượng (14 cao – 30 gọn)")}</div><label>Thư mục xuất<input id="exportFolder" value="${esc(P.params.export_directory)}" placeholder="Mặc định: thư mục exports trong dữ liệu ứng dụng"></label><button id="chooseExportFolder">Chọn thư mục</button><details><summary>Tùy chọn mã hóa</summary><div>${field(
    "video_encoder",
    "Thiết bị mã hóa",
    "select",
    [
      ["auto", "Tự chọn thiết bị đã kiểm tra"],
      ["cpu", "Bộ xử lý"],
      ["nvenc", "NVIDIA tương thích"],
    ],
  )}<p class="muted">Độ phân giải giữ như video nguồn. Mã hóa H.264; âm thanh AAC khi được chọn.</p></div></details><div id="exportCheck" class="info">Đang kiểm tra các phần đầu ra…</div><button id="startExport" class="primary">Chuẩn bị và xuất video</button><div id="exportResult"></div>`;
  const qualityField = $('[data-key="video_quality"]').closest("label");
  const encodingDetails = panel.querySelector("details > div");
  encodingDetails.prepend(qualityField);
  qualityField.childNodes[0].textContent = "Mức nén nâng cao (14–30)";
  panel
    .querySelector(".two")
    .insertAdjacentHTML(
      "beforeend",
      `<label>Chất lượng<select id="qualityPreset"><option value="18">Cao · tệp lớn hơn</option><option value="20">Cân bằng</option><option value="26">Gọn nhẹ</option><option value="custom">Tùy chỉnh nâng cao</option></select></label>`,
    );
  $("#qualityPreset").value = [18, 20, 26].includes(+P.params.video_quality)
    ? String(P.params.video_quality)
    : "custom";
  $("#qualityPreset").onchange = (e) => {
    if (e.target.value !== "custom") {
      change((p) => (p.params.video_quality = +e.target.value));
      $('[data-key="video_quality"]').value = e.target.value;
    } else panel.querySelector("details").open = true;
  };
  $("#exportFolder").parentElement.insertAdjacentHTML(
    "beforebegin",
    field("export_resolution", "Độ phân giải", "select", [
      ["source", "Giữ kích thước nguồn"],
      ["720", "Tối đa 720p · gọn nhẹ"],
      ["1080", "Tối đa 1080p · Full HD"],
      ["2160", "Tối đa 2160p · 4K"],
    ]),
  );
  encodingDetails.querySelector("p").textContent =
    "Không phóng lớn nguồn nhỏ. Video dọc giữ tỷ lệ dọc; phụ đề được thu theo hình. Mã hóa H.264, âm thanh AAC.";
  $("#wantVoice").onchange = (e) => {
    change((p) => (p.params.voice_gain = e.target.checked ? 1 : 0));
    guard(updateExportPlan)();
  };
  $("#exportFolder").onchange = (e) =>
    change((p) => (p.params.export_directory = e.target.value));
  $("#chooseExportFolder").onclick = guard(async () => {
    if (window.pywebview?.api?.pick_directory) {
      const path = await window.pywebview.api.pick_directory();
      if (path) {
        change((p) => (p.params.export_directory = path));
        $("#exportFolder").value = path;
      }
    } else notice("Nhập đường dẫn thư mục hiện có vào ô phía trên.");
  });
  $("#startExport").onclick = guard(async () => {
    await save();
    const plan = await api("/api/studio/projects/" + P.id + "/export-plan");
    if (
      plan.needs_subtitles &&
      (plan.subtitles.errors.length || plan.subtitles.requires_review)
    ) {
      subtab = "quality";
      setArea("subtitles");
      return notice("Kiểm tra phụ đề trước khi xuất.", true);
    }
    const willTranslate =
      P.params.pipeline_translate &&
      P.segments.some((r) => !r.translated?.trim());
    change((p) => {
      p.params.pipeline_extract = "existing";
      p.params.pipeline_translate = !!willTranslate;
      p.params.translation_scope = "missing";
      p.params.pipeline_tts = p.params.voice_gain > 0 && !p.params.voice_path;
      p.params.pipeline_clean = !!p.params.use_clean;
    });
    await save();
    const check = await api("/api/preflight", {
      project_id: P.id,
      kind: "pipeline",
    });
    if (!check.ok) throw new Error(check.errors.join("\n"));
    if (
      await ask(
        "Xuất video thành phẩm?",
        plan.steps
          .map((x) => (x.reuse ? "Giữ lại: " : "Thực hiện: ") + x.label)
          .join("\n") +
          (willTranslate
            ? "\nPhụ đề sẽ gửi đến " + P.params.provider + "; có thể tính phí."
            : "") +
          "\nVideo nguồn và kết quả trước vẫn giữ nguyên.",
        "Xuất video",
      )
    )
      await queue("pipeline", { replace_segments: true });
  });
  guard(updateExportPlan)();
  const finalArtifact = latestFinalArtifact(),
    result = finalArtifact ? {id: finalArtifact.job_id} : null;
  if (result)
    $("#exportResult").innerHTML =
      `<div class="result-card"><h3>${finalArtifact._final_current?'Video thành phẩm hiện tại':'Video thành phẩm của lượt trước'}</h3><p class="muted">${finalArtifact._final_current?'Cần xem/nghe để kiểm tra nội dung.':esc(finalArtifact._final_reason)}</p><div class="row"><button id="openFinal" class="primary">${finalArtifact._final_current?'Mở video thành phẩm':'Mở video lượt trước'}</button><button id="saveFinalCopy">Lưu bản sao đến thư mục xuất</button></div></div>`;
  if ($("#openFinal"))
    $("#openFinal").onclick = guard(() =>
      api("/api/studio/jobs/" + result.id + "/open-result", {}),
    );
  if ($("#saveFinalCopy"))
    $("#saveFinalCopy").onclick = guard(async () => {
      await save();
      const r = await api("/api/studio/jobs/" + result.id + "/publish", {
        directory: P.params.export_directory,
        name: P.params.export_name,
      });
      notice("Đã lưu: " + r.path);
    });
}
async function updateExportPlan() {
  await save();
  const id = P.id,
    r = await api("/api/studio/projects/" + id + "/export-plan");
  if (area !== "export" || P?.id !== id) return;
  $("#exportCheck").innerHTML =
    r.steps
      .map(
        (x) =>
          `<div class="step">${icon(x.reuse ? "check" : "video")}<div>${esc(x.label)}<small>${x.reuse ? "Đã có kết quả phù hợp · không chạy lại" : "Sẽ thực hiện khi bắt đầu"}</small></div></div>`,
      )
      .join("") +
    (r.needs_subtitles &&
    (r.subtitles.requires_review || r.subtitles.errors.length)
      ? '<div class="warning">Phụ đề cần kiểm tra. Mở Phụ đề → Kiểm tra trước khi xuất.</div><button id="goReview">Kiểm tra phụ đề</button>'
      : "");
  if (r.clean_warnings?.length) {
    $("#exportCheck").insertAdjacentHTML(
      "beforeend",
      r.clean_warnings
        .map((message) => `<p class="warning">${esc(message)}</p>`)
        .join("") +
        '<button id="reviewCleanRegions">Kiểm tra vùng chữ gốc</button>',
    );
    $("#reviewCleanRegions").onclick = () => setArea("clean");
  }
  $("#exportCheck").insertAdjacentHTML(
    "beforeend",
    `<p class="muted">Dự kiến video khoảng ${(r.space.estimated_output_bytes / 1024 ** 2).toFixed(0)} MiB; cần dự phòng ${(r.space.required_bytes / 1024 ** 3).toFixed(1)} GiB. Ổ dữ liệu còn ${(r.space.free_bytes / 1024 ** 3).toFixed(1)} GiB. Đây là ước lượng, không phải kích thước cam kết.</p>`,
  );
  if ($("#goReview"))
    $("#goReview").onclick = () => {
      subtab = "quality";
      setArea("subtitles");
    };
}

function importVideo() {
  openModal(
    "Nhập video",
    '<label>Tên dự án<input id="newName" maxlength="200" placeholder="Đặt tên ngắn, dễ tìm"></label><label>Video trên máy<input id="newSource" placeholder="D:\\Videos\\video.mp4"></label><p class="info">Không tải video lên mạng. Video nguồn, phụ đề và bản xuất được giữ riêng.</p>',
    [
      {
        label: "Chọn tệp",
        run: async () => {
          if (window.pywebview?.api?.pick_video) {
            const path = await window.pywebview.api.pick_video();
            if (path) $("#newSource").value = path;
          } else notice("Nhập đường dẫn đầy đủ của video trong ô phía trên.");
        },
      },
      { label: "Hủy", run: () => closeModal() },
      {
        label: "Tạo dự án",
        primary: true,
        run: async () => {
          const p = await api("/api/projects", {
            path: $("#newSource").value,
            name: $("#newName").value,
          });
          await api(
            "/api/projects/" + p.id,
            {
              expected_revision: p.updated,
              params: {
                use_clean: false,
                method: "blur",
                voice_gain: 0,
                extraction_start: 0,
                extraction_end: 0,
                translation_scope: "missing",
                pipeline_clean: false,
                pipeline_tts: false,
              },
            },
            "PUT",
          );
          closeModal();
          await loadProject(p.id);
          setArea("setup");
          await poll();
        },
      },
    ],
  );
}
$("#importEmpty").onclick = importVideo;
$("#subtitleFile").onchange = guard(async (e) => {
  const file = e.target.files[0];
  e.target.value = "";
  if (!file || !P) return;
  if (file.size > 10000000) throw new Error("Tệp phụ đề quá lớn.");
  const text = await file.text();
  openModal(
    "Nhập phụ đề",
    `<p>${esc(file.name)} sẽ thay bảng hiện tại. Phiên bản trước được giữ để khôi phục.</p><label class="check"><input id="importTarget" type="checkbox">Tệp này đã là ngôn ngữ đầu ra (không cần dịch)</label>`,
    [
      { label: "Hủy", run: () => closeModal() },
      {
        label: "Nhập phụ đề",
        primary: true,
        run: async () => {
          await save();
          P = await api("/api/studio/projects/" + P.id + "/import", {
            text,
            expected_revision: P.updated,
            target_language: $("#importTarget").checked,
          });
          selected.clear();
          closeModal();
          subtab = "content";
          setArea("subtitles");
          notice(
            "Đã nhập phụ đề. Kiểm tra khớp thời gian với video trước khi xuất.",
          );
        },
      },
    ],
  );
});
function downloadDocument(value, name) {
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(value, null, 2)], {
      type: "application/json;charset=utf-8",
    }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 30000);
}
function restoreProjectFile() {
  const picker = document.createElement("input");
  picker.type = "file";
  picker.accept = ".json,.npk";
  picker.onchange = guard(async () => {
    const file = picker.files[0];
    if (!file) return;
    if (file.size > 20000000) throw new Error("Bản sao dự án vượt 20 MB.");
    const document = JSON.parse(await file.text());
    if (document.format !== "npk-project")
      throw new Error("Không phải bản sao dự án NPK.");
    openModal(
      "Khôi phục bản sao dự án",
      `<p>${esc(document.name)} · ${document.segments?.length || 0} câu. Tạo dự án mới, không thay dự án đang có.</p><label>Chọn lại video nguồn<input id="backupSource"></label><p class="info">Bản sao không kèm video, model, khóa hay nhạc ngoài. Kiểm lại vùng và phụ đề sau khi khôi phục; kết quả xử lý sẽ được tạo riêng.</p>`,
      [
        {
          label: "Chọn video",
          run: async () => {
            if (window.pywebview?.api?.pick_video)
              $("#backupSource").value =
                (await window.pywebview.api.pick_video()) || "";
            else notice("Nhập đường dẫn đầy đủ đến video nguồn.");
          },
        },
        { label: "Hủy", run: closeModal },
        {
          label: "Khôi phục",
          primary: true,
          run: async () => {
            await save();
            const restored = await api("/api/studio/restore-project", {
              document,
              source: $("#backupSource").value,
            });
            closeModal();
            await loadProject(restored.id);
            await poll();
            subtab = "content";
            setArea("subtitles");
            notice("Đã khôi phục thành dự án mới. Kiểm phụ đề trước khi xuất.");
          },
        },
      ],
    );
  });
  picker.click();
}
const sizeText = (bytes) => bytes == null ? 'Chưa đo được' : bytes >= 1024 ** 3
  ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
let projectMenuOrigin = null;
function closeProjectMenu(focus = false) {
  $('#projectContextMenu')?.remove();
  projectMenuOrigin?.setAttribute('aria-expanded', 'false');
  if (focus) projectMenuOrigin?.focus();
}

function replaceSourceDialog(mode = 'reset') {
  if (!P) return;
  const id = P.id;
  video.pause();
  openModal('Thay video nguồn',
    `<p>Dự án: <strong>${esc(P.name)}</strong></p>
     <label>Video mới trên máy<input id="replacementPath" placeholder="Chọn tệp hoặc dán đường dẫn đầy đủ"></label>
     <label>Nội dung dự án<select id="replacementMode">
       <option value="reset">Bắt đầu lại nội dung · dùng cho video khác</option>
       <option value="keep">Giữ phụ đề và vùng chữ · cùng thời lượng/kích thước</option>
     </select></label><p class="info" id="replacementHint"></p>
     <p class="muted">Giữ tên, kiểu chữ và giọng đã chọn. Không xóa video hay tệp kết quả cũ; bản dự án trước được giữ trong lịch sử. Không tự chạy xử lý.</p>`,
    [
      {label:'Chọn tệp', run:async () => {
        if (window.pywebview?.api?.pick_video) {
          const path = await window.pywebview.api.pick_video();
          if (path && $('#replacementPath')) $('#replacementPath').value = path;
        } else notice('Dán đường dẫn đầy đủ của video vào ô Video mới trên máy.');
      }},
      {label:'Hủy', run:closeModal},
      {label:'Thay video', primary:true, run:async () => {
        const path = $('#replacementPath').value.trim();
        const mode = $('#replacementMode').value;
        if (!path) throw new Error('Chọn video mới trước khi thay nguồn.');
        if (P?.id !== id) throw new Error('Dự án đang mở đã đổi. Chọn lại thao tác thay nguồn.');
        await save();
        await api('/api/studio/projects/' + id + '/replace-source', {
          path, mode, expected_revision:P.updated,
        });
        closeModal();
        drawMode = false;
        lastDetail = null;
        await loadProject(id, true);
        await poll();
        notice(mode === 'keep'
          ? 'Đã thay nguồn và giữ phụ đề. Hãy kiểm tra lại độ khớp trước khi xuất.'
          : 'Đã thay video nguồn. Bạn có thể tạo phụ đề cho video mới.');
      }},
    ]);
  $('#replacementMode').value = mode;
  const hint = () => $('#replacementHint').textContent = $('#replacementMode').value === 'keep'
    ? 'Giữ phụ đề, vùng chữ và âm thanh nhập riêng. Kết quả xử lý và xác nhận chất lượng cũ không dùng cho nguồn mới; cần kiểm tra lại.'
    : 'Đặt lại phụ đề, vùng chữ, kết quả xử lý và liên kết âm thanh riêng để tránh dùng nhầm nội dung video trước.';
  $('#replacementMode').onchange = hint;
  hint();
}
$('#replaceSource').onclick = () => replaceSourceDialog();
document.addEventListener('pointerdown', (e) => {
  if (!e.target.closest('#projectContextMenu,.project-more')) closeProjectMenu();
});
window.addEventListener('resize', () => closeProjectMenu());
panel.addEventListener('scroll', () => closeProjectMenu());
function showProjectMenu(id, origin, x, y) {
  closeProjectMenu();
  const project = projects.find(p => p.id === id);
  if (!project) return;
  projectMenuOrigin = origin;
  origin.setAttribute('aria-expanded', 'true');
  const menu = document.createElement('div');
  menu.id = 'projectContextMenu'; menu.className = 'project-context-menu';
  menu.setAttribute('role', 'menu'); menu.setAttribute('aria-label', 'Thao tác dự án');
  const actions = [
    ['Mở dự án', () => $(`[data-open-project="${id}"]`)?.click()],
    ['Đổi tên', async () => {
      await save();
      const p = await api('/api/studio/projects/' + id);
      openModal('Đổi tên dự án', `<label>Tên mới<input id="projectNewName" value="${esc(p.name)}" maxlength="200"></label>`, [
        {label:'Hủy', run:closeModal}, {label:'Lưu tên', primary:true, run:async () => {
          const name = $('#projectNewName').value.trim();
          if (!name) throw new Error('Tên dự án không được để trống.');
          const updated = await api('/api/projects/' + id, {name, expected_revision:p.updated}, 'PUT');
          if (P?.id === id) { P = updated; $('#projectName').textContent = P.name; $('#projectsTop').textContent = P.name; }
          closeModal(); await poll(); renderProjects();
        }}]);
    }],
    ['Tạo bản sao', async () => {
      await save(); const p = await api('/api/studio/projects/' + id + '/duplicate', {});
      await loadProject(p.id); await poll(); renderProjects();
    }],
    ['Xuất bản sao dự án', async () => {
      await save(); downloadDocument(await api('/api/studio/projects/' + id + '/backup'), 'NPK-du-an.npk.json');
      notice('Bản sao gồm nội dung và chỉnh sửa, không kèm video hoặc khóa truy cập.');
    }],
    [project.archived ? 'Bỏ lưu trữ' : 'Lưu trữ dự án', async () => {
      await save(); const p = await api('/api/studio/projects/' + id + '/archive', {archived:!project.archived});
      if (P?.id === id) P = p;
      await poll(); renderProjects(); notice('Lưu trữ chỉ đổi cách hiển thị, không xóa tệp.');
    }],
    ['Dung lượng / dọn lượt xử lý cũ', () => storagePlan('cleanup', id)],
    ['Xóa dự án', () => deleteProjectNow(id)],
  ];
  for (const [label, run] of actions) {
    const b = document.createElement('button'); b.textContent = label; b.setAttribute('role','menuitem');
    if (label.startsWith('Xóa')) b.className = 'danger-text';
    b.onclick = guard(async () => { closeProjectMenu(); await run(); }); menu.append(b);
  }
  menu.onkeydown = (e) => {
    const buttons = Array.from(menu.querySelectorAll('button'));
    const index = buttons.indexOf(document.activeElement);
    if (['ArrowDown','ArrowUp','Home','End','Escape','Tab'].includes(e.key)) {
      if (e.key === 'Tab') { closeProjectMenu(true); return; }
      e.preventDefault();
      if (e.key === 'Escape') return closeProjectMenu(true);
      const next = e.key === 'Home' ? 0 : e.key === 'End' ? buttons.length - 1
        : (index + (e.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next].focus();
    }
  };
  document.body.append(menu);
  const r = menu.getBoundingClientRect();
  menu.style.left = Math.max(8, Math.min(x, innerWidth - r.width - 8)) + 'px';
  menu.style.top = Math.max(8, Math.min(y, innerHeight - r.height - 8)) + 'px';
  menu.querySelector('button').focus();
}
function openStorage() { settingsTab = 'storage'; setArea('settings'); }
function clearOpenProject() {
  cancelRegionDrag(); drawMode = false;
  closeRegionMenu();regionSelection.clear();regionSelectionKey='';
  clearTimeout(saveTimer); stopMix(); video.pause(); video.removeAttribute('src'); video.load();
  P = null; dirty = false; conflict = false; undo = []; redo = []; selected.clear();
  quality = null; currentCue = -1; localStorage.removeItem('npk-current');
  window.NPKUpgrade?.reset('current_project', '');
  $('#empty').hidden = false; $('#caption').hidden = true; $('#regions').innerHTML = '';
  $('#projectName').textContent = 'Chưa mở dự án'; $('#projectsTop').textContent = 'Chọn dự án';
  $('#sourceMeta').textContent = ''; $('#saved').textContent = ''; updateUndo();
  $('#scrub').max = 0; $('#scrub').value = 0;
  $('#timecode').textContent = '00:00.00 / 00:00.00';
  $('#viewSource').innerHTML = '<option value="source">Video nguồn</option>';
  $('#replaceSource').disabled = true;
}
const storageActionsInFlight = new Set();
async function deleteProjectNow(id) {
  const key = 'delete:' + id;
  if (storageActionsInFlight.has(key)) return;
  storageActionsInFlight.add(key);
  try {
    await save();
    notice('Đang chuyển dự án vào thùng rác…');
    const plan = await api('/api/studio/storage/plan?action=delete&project_id=' + id);
    if (plan.blocked) throw new Error(plan.blocked);
    const result = await api('/api/studio/storage/execute', {action:'delete', project_id:id, signature:plan.signature});
    if (P?.id === id) clearOpenProject();
    await poll(); openStorage(); notice(result.message);
  } finally {
    storageActionsInFlight.delete(key);
  }
}
async function purgeTrashNow(id, button) {
  const key = 'purge:' + id;
  if (storageActionsInFlight.has(key)) return;
  storageActionsInFlight.add(key);
  button.disabled = true;
  button.textContent = 'Đang xóa…';
  try {
    const plan = await api('/api/studio/storage/trash/' + id + '/purge-plan');
    const result = await api('/api/studio/storage/trash/' + id + '/purge', {signature:plan.signature});
    if (area === 'settings' && settingsTab === 'storage') {
      const scroll = panel.scrollTop;
      await renderStorage();
      if (area === 'settings' && settingsTab === 'storage') panel.scrollTop = scroll;
    }
    notice(`Đã xóa vĩnh viễn ${sizeText(result.removed_bytes)}. Không thể khôi phục trong NPK.`);
  } finally {
    storageActionsInFlight.delete(key);
    button.disabled = false;
    button.textContent = 'Xóa vĩnh viễn';
  }
}
async function storagePlan(action, id = null) {
  if (action === 'delete') return deleteProjectNow(id);
  await save();
  const plan = await api('/api/studio/storage/plan?action=' + action + (id ? '&project_id=' + id : ''));
  const title = action === 'cache' ? 'Dọn bộ nhớ đệm giọng' : 'Dọn lượt xử lý cũ';
  const explanation = action === 'cache' ? 'Dọn các câu giọng lưu đệm và mẫu nghe thử dùng chung. Giọng đã ghép và model được giữ; lần tạo giọng sau có thể chậm hơn.'
    : 'Chỉ dọn lượt xử lý không còn được dự án hoặc lịch sử phiên bản nào dùng. Không xóa phụ đề, giọng hiện tại hay video thành phẩm.';
  const body = `<h3>${esc(plan.name)}</h3><p>${explanation}</p><p>Dữ liệu phát sinh đã đo: ${sizeText(plan.managed_bytes)}${plan.measurement_partial ? ' · có mục chưa đo được' : ''}. Không tính video nguồn hay bản xuất ngoài thư mục tác vụ.</p><div class="storage-summary"><strong>${sizeText(plan.bytes)}</strong><span>${plan.targets.length} mục có thể chuyển vào thùng rác</span></div>
    <p class="info">Chuyển vào thùng rác chưa giải phóng dung lượng. Sau khi kiểm tra, vào Dung lượng & thùng rác → Xóa vĩnh viễn để lấy lại chỗ trống.</p>
    ${plan.blocked ? `<p class="warning">${esc(plan.blocked)}</p>` : ''}
    <details><summary>Xem danh sách ${plan.targets.length} mục sẽ dọn</summary><ul class="storage-paths">${plan.targets.map(t=>`<li><code>${esc(t.path)}</code> · ${sizeText(t.bytes)}</li>`).join('') || '<li>Không có mục cần dọn.</li>'}</ul></details>
    <details><summary>Giữ lại ${plan.retained.length} lượt xử lý</summary><ul class="storage-paths">${plan.retained.map(t=>`<li>${esc(t.reason)} · ${sizeText(t.bytes)}<small>${esc(t.path)}</small></li>`).join('') || '<li>Không có lượt xử lý được giữ riêng.</li>'}</ul></details>`;
  const buttons = [{label:'Hủy', run:closeModal}];
  if (!plan.blocked && plan.targets.length) buttons.push({label:'Chuyển vào thùng rác', primary:true, run:async () => {
    const r = await api('/api/studio/storage/execute', {action, project_id:id, confirmed:true, signature:plan.signature});
    closeModal();
    await poll(); openStorage(); notice(r.message);
  }});
  openModal(title, body, buttons);
}
async function renderStorage() {
  const body = $('#settingsBody');
  body.innerHTML = '<p class="info">Đang đo dung lượng thực tế. Chưa dọn hay xóa tệp nào.</p>';
  const r = await api('/api/studio/storage/overview');
  if (!body.isConnected || settingsTab !== 'storage') return;
  const category = c => `<div class="storage-row"><div><strong>${esc(c.name)}</strong><small>${esc(c.error || c.note)}</small></div><span>${sizeText(c.bytes)}</span></div>`;
  body.innerHTML = `<div class="panel-title"><h3>Ổ dữ liệu còn ${sizeText(r.free_bytes)}</h3><span class="grow"></span><button id="refreshStorage">Đo lại</button></div>
    <p>${esc(r.note)}</p>${r.blocked ? `<p class="warning">${esc(r.blocked)}</p>` : ''}
    <h3>Dữ liệu phát sinh khi dùng tool</h3>${r.categories.filter(c=>!['runtime','models','tests','build'].includes(c.id)).map(category).join('')}
    <div class="row section-end"><button id="cleanCurrentProject" ${P ? '' : 'disabled'}>Dọn lượt cũ của dự án đang mở</button><button id="cleanSharedCache">Dọn bộ nhớ đệm giọng</button></div>
    <details><summary>Thành phần hệ thống và phát triển — không dọn tự động</summary>${r.categories.filter(c=>['runtime','models','tests','build'].includes(c.id)).map(category).join('')}</details>
    <details><summary>Vị trí lưu trên máy</summary><p class="storage-paths">Dự án, phụ đề và lịch sử: <code>${esc(r.database)}</code></p><ul class="storage-paths">${r.categories.map(c=>`<li>${esc(c.name)}<small>${esc(c.path)}</small></li>`).join('')}</ul></details>
    <h3>Thùng rác NPK <span class="badge">${r.trash.length}</span></h3><p class="info">Các mục ở đây vẫn chiếm dung lượng. Bấm Xóa vĩnh viễn sẽ xóa ngay, không hỏi lại và không thể hoàn tác. Video thành phẩm và video nguồn không tự đi vào thùng rác.</p>
    <div id="trashEntries">${r.trash.map(e=>`<div class="trash-entry" data-trash-entry="${e.id}"><strong>${esc(e.name)}</strong><small>${sizeText(e.bytes)} · ${new Date(e.created*1000).toLocaleString('vi-VN')} · ${e.state === 'ready' ? 'Có thể khôi phục' : e.state === 'purging' ? 'Xóa vĩnh viễn chưa xong' : 'Bị gián đoạn — cần khôi phục'}</small><div class="row"><button data-restore-trash="${e.id}" ${e.state === 'purging' ? 'disabled' : ''}>Khôi phục</button><button class="danger-text" data-purge-trash="${e.id}" ${['ready','purging'].includes(e.state) && !storageActionsInFlight.has('purge:' + e.id) ? '' : 'disabled'} title="Xóa ngay, không hỏi lại; không thể hoàn tác">Xóa vĩnh viễn</button></div></div>`).join('') || '<p class="muted">Thùng rác đang trống.</p>'}</div>`;
  $('#refreshStorage').onclick = guard(renderStorage);
  $('#cleanCurrentProject').onclick = guard(() => storagePlan('cleanup', P.id));
  $('#cleanSharedCache').onclick = guard(() => storagePlan('cache'));
  $$('[data-restore-trash]').forEach(b=> b.onclick = guard(async () => {
    if (!(await ask('Khôi phục dữ liệu?', 'Đưa dữ liệu về vị trí cũ. NPK sẽ dừng nếu vị trí đó đã có tệp mới, không ghi đè.', 'Khôi phục'))) return;
    const value = await api('/api/studio/storage/trash/' + b.dataset.restoreTrash + '/restore', {confirmed:true});
    await poll(); await renderStorage(); notice(value.message);
  }));
  $$('[data-purge-trash]').forEach(b=> b.onclick = guard(() => purgeTrashNow(b.dataset.purgeTrash, b)));
}
function renderProjects() {
  closeProjectMenu();
  panel.innerHTML = `<div class="panel-title"><h2>Dự án</h2><span class="grow"></span><button id="newVideo" class="primary">Nhập video</button></div><input id="projectSearch" type="search" aria-label="Tìm dự án" placeholder="Tìm tên dự án"><label class="check section-end"><input id="showArchived" type="checkbox">Hiện dự án đã lưu trữ</label><div id="projectCards"></div>${P ? `<details><summary>Dự án đang mở</summary><div><label>Tên dự án<input id="renameProject" value="${esc(P.name)}" maxlength="200"></label><div class="row"><button id="duplicateProject">Tạo bản sao</button><button id="relinkSource">Tìm lại video nguồn</button><button id="historyProject">Khôi phục phiên bản</button></div></div></details>` : ""}`;
  $("#newVideo").onclick = importVideo;
  $("#projectSearch").insertAdjacentHTML(
    "beforebegin",
    `<div class="row section-end"><button id="backupProject" ${P ? "" : "disabled"}>Xuất bản sao dự án</button><button id="restoreProjectFile">Mở bản sao dự án</button><button id="manageStorage">Dung lượng & thùng rác</button></div><p class="muted">Chuột phải vào dự án hoặc bấm ⋯ để mở các thao tác. Lưu trữ chỉ ẩn dự án, không giải phóng dung lượng.</p>`,
  );
  $("#backupProject").onclick = guard(async () => {
    await save();
    downloadDocument(
      await api("/api/studio/projects/" + P.id + "/backup"),
      "NPK-du-an.npk.json",
    );
    notice("Bản sao lưu chỉnh sửa, không kèm video hoặc khóa truy cập.");
  });
  $("#restoreProjectFile").onclick = restoreProjectFile;
  $("#manageStorage").onclick = openStorage;
  function cards() {
    const q = $("#projectSearch").value.toLocaleLowerCase(),
      archived = $("#showArchived").checked;
    $("#projectCards").innerHTML =
      projects
        .filter(
          (p) =>
            (!p.archived || archived) && p.name.toLocaleLowerCase().includes(q),
        )
        .map(
          (p) =>
            `<div class="project-card" data-project-card="${p.id}"><button class="choose" data-open-project="${p.id}"><strong>${esc(p.name)}</strong><small>${clock(p.duration)} · ${p.count} câu${p.archived ? " · đã lưu trữ" : ""}</small></button><button class="project-more" data-project-menu="${p.id}" aria-label="Thao tác dự án ${esc(p.name)}" aria-haspopup="menu" title="Thao tác dự án">⋯</button></div>`,
        )
        .join("") || '<p class="muted">Chưa có dự án phù hợp.</p>';
    $$("[data-open-project]").forEach(
      (b) =>
        (b.onclick = guard(async () => {
          if (conflict && dirty) {
            if (
              !(await ask(
                "Tải lại dự án?",
                "Có sửa chưa lưu trong cửa sổ này. Tải lại sẽ bỏ phần sửa chưa lưu; bản trên đĩa vẫn nguyên.",
                "Tải lại",
              ))
            )
              return;
          }
          await loadProject(b.dataset.openProject, conflict);
          setArea("subtitles");
        })),
    );
    $$("[data-project-card]").forEach((card) => {
      const button = card.querySelector('.project-more');
      card.oncontextmenu = (e) => {
        e.preventDefault();
        showProjectMenu(card.dataset.projectCard, button, e.clientX, e.clientY);
      };
      card.onkeydown = (e) => {
        if ((e.shiftKey && e.key === 'F10') || e.key === 'ContextMenu') {
          e.preventDefault(); button.click();
        }
      };
      button.onclick = () => {
        const r = button.getBoundingClientRect();
        showProjectMenu(card.dataset.projectCard, button, r.right, r.bottom);
      };
    });
  }
  cards();
  $("#projectSearch").oninput = cards;
  $("#showArchived").onchange = cards;
  if (P) {
    $("#renameProject").onchange = (e) => {
      change((p) => (p.name = e.target.value));
      $("#projectName").textContent = P.name;
      $("#projectsTop").textContent = P.name;
    };
    $("#duplicateProject").onclick = guard(async () => {
      await save();
      const p = await api("/api/studio/projects/" + P.id + "/duplicate", {});
      await loadProject(p.id);
      await poll();
      renderProjects();
    });
    $("#relinkSource").onclick = () => replaceSourceDialog('keep');
    $("#historyProject").onclick = guard(async () => {
      await save();
      const rows = await api("/api/studio/projects/" + P.id + "/revisions");
      openModal(
        "Phiên bản dự án",
        `<label>Chọn bản cần khôi phục<select id="revisionPick">${rows.map((r) => `<option value="${r.updated}">${esc(new Date(r.updated * 1000).toLocaleString("vi-VN"))} · ${r.count} câu</option>`).join("")}</select></label><p class="info">Khôi phục nội dung và thông số của bản đã chọn. Bản hiện tại tiếp tục được giữ trong lịch sử.</p>`,
        [
          { label: "Đóng", run: () => closeModal() },
          {
            label: "Khôi phục",
            primary: true,
            run: async () => {
              P = await api("/api/studio/projects/" + P.id + "/restore", {
                revision: +$("#revisionPick").value,
                expected_revision: P.updated,
              });
              closeModal();
              setMedia();
              renderPanel();
            },
          },
        ],
      );
    });
  }
}
function renderSettings() {
  panel.innerHTML = `<div class="panel-title"><h2>Cài đặt</h2></div><div class="settings-nav">${[
    ["connections", "Kết nối dịch vụ"],
    ["performance", "Hiệu năng"],
    ["storage", "Dung lượng & thùng rác"],
    ["preferences", "Tùy chọn ứng dụng"],
    ["help", "Trợ giúp & chẩn đoán"],
  ]
    .map(
      ([id, n]) =>
        `<button data-settings="${id}" class="${settingsTab === id ? "active" : ""}">${n}</button>`,
    )
    .join("")}</div><div id="settingsBody"></div>`;
  $$("[data-settings]").forEach(
    (b) =>
      (b.onclick = () => {
        settingsTab = b.dataset.settings;
        renderPanel();
      }),
  );
  if (settingsTab === "connections") guard(renderConnections)();
  if (settingsTab === "performance") guard(renderPerformance)();
  if (settingsTab === "storage") guard(renderStorage)();
  if (settingsTab === "preferences") renderPreferences();
  if (settingsTab === "help") renderHelp();
}
let keyProvider = null,
  keyRows = [],
  connectionModelCatalogs = {},
  connectionRouter9BaseUrl = null;
const router9DefaultBaseUrl = "http://127.0.0.1:20128/v1";
const connectionModelDefaults = {
  gemini: "gemini-3.7-flash",
  deepseek: "deepseek-flash",
};
function modelMatchesProvider(provider, model) {
  if (provider === "router9") return typeof model === "string" && !!model.trim() && !/\s/.test(model);
  return typeof model === "string" && model.toLowerCase().startsWith(`${provider}-`);
}
function router9ConnectionBaseUrl() {
  const input = $("#connectionBaseUrl");
  const value = (input ? input.value : connectionRouter9BaseUrl ?? P?.params.router9_base_url ?? router9DefaultBaseUrl).trim().replace(/\/+$/, "");
  try {
    const url = new URL(value);
    if (/^\/dashboard\/?$/.test(url.pathname)) return url.origin + "/v1";
  } catch (_) {}
  return value;
}
function connectionCatalogKey(provider) {
  return provider === "router9" ? provider + ":" + router9ConnectionBaseUrl() : provider;
}
function connectionModels(provider) {
  return connectionModelCatalogs[connectionCatalogKey(provider)] || [];
}
function invalidateRouter9Catalog() {
  if (keyProvider !== "router9") return;
  for (const key of Object.keys(connectionModelCatalogs)) if (key.startsWith("router9:")) delete connectionModelCatalogs[key];
  $("#modelOptions")?.replaceChildren();
}
function connectionModelFor(provider, preferred = "", catalog = null) {
  const available = Array.isArray(catalog)
    ? catalog
    : connectionModels(provider);
  const legacyDeepSeek = ["deepseek-chat", "deepseek-reasoner"];
  const candidateIsValid = (model) =>
    modelMatchesProvider(provider, model) &&
    !(provider === "deepseek" && legacyDeepSeek.includes(model)) &&
    (!available.length || available.includes(model));
  // A catalog refresh must not erase or silently replace a saved router ID.
  // Catalog membership is checked when the user explicitly saves or tests it.
  if (provider === "router9" && modelMatchesProvider(provider, preferred)) return preferred;
  if (candidateIsValid(preferred)) return preferred;
  // Router catalogs can use namespaced IDs. The user chooses a listed model;
  // do not infer a model from the provider name or select the first entry.
  if (provider === "router9") return "";
  const fallback = connectionModelDefaults[provider];
  if (candidateIsValid(fallback)) return fallback;
  return available.find((model) => modelMatchesProvider(provider, model)) || fallback;
}
function connectionContractText(params) {
  if (!params) return "Chưa mở dự án";
  return `${params.provider || "Chưa chọn dịch vụ"} · ${params.translation_model || "Chưa chọn model"}${params.provider === "router9" ? " · " + params.router9_base_url : ""}`;
}
function refreshConnectionReceipt() {
  const receipt = $("#savedConnection");
  if (!receipt) return;
  receipt.textContent = `Đã lưu trong dự án: ${connectionContractText(P?.params)}`;
  const job = jobs.find(row => row.project_id === P?.id && ["queued", "running"].includes(row.state) && row.configuration?.translation_model);
  $("#runningConnection").textContent = job
    ? `Tác vụ ${job.id.slice(0, 8)} đang dùng bản cấu hình đã chốt: ${connectionContractText(job.configuration)}. Lưu model mới sẽ dừng tác vụ trước lượt gọi API tiếp theo; phản hồi đang chờ có thể hoàn tất.`
    : "Cấu hình đã lưu sẽ được dùng khi bắt đầu hoặc tiếp tục tác vụ.";
}
function refreshConnectionDraft() {
  const status = $("#connectionDraft");
  if (!status) return;
  const changed = P && (keyProvider !== P.params.provider || $("#connectionModel").value.trim() !== P.params.translation_model ||
    (keyProvider === "router9" && router9ConnectionBaseUrl() !== P.params.router9_base_url));
  status.textContent = changed
    ? "Model / kết nối đang chỉnh chưa lưu vào dự án. Bấm Lưu model cho dự án để áp dụng; Lưu khóa chỉ lưu token."
    : "Model hiển thị khớp cấu hình đã lưu. Lưu khóa chỉ lưu token, không đổi model.";
  status.className = changed ? "warning" : "muted";
  if (!dirty && !saving) $("#saved").textContent = changed ? "Model đang chỉnh chưa lưu" : "Đã lưu";
}
async function renderConnections() {
  const body = $("#settingsBody");
  if (!keyProvider) keyProvider = P?.params.provider || "gemini";
  if (connectionRouter9BaseUrl === null) connectionRouter9BaseUrl = P?.params.router9_base_url || router9DefaultBaseUrl;
  const initialModel = connectionModelFor(
    keyProvider,
    P?.params.provider === keyProvider ? P?.params.translation_model || "" : "",
  );
  body.innerHTML = `<p class="muted">Khóa truy cập được lưu riêng trong Windows Credential Manager, không nằm trong tệp dự án.</p><label>Nhà cung cấp<select id="keyProvider"><option value="gemini" ${keyProvider === "gemini" ? "selected" : ""}>Gemini</option><option value="deepseek" ${keyProvider === "deepseek" ? "selected" : ""}>DeepSeek</option><option value="router9" ${keyProvider === "router9" ? "selected" : ""}>9Router · API tương thích OpenAI</option></select></label><label id="router9EndpointField" ${keyProvider === "router9" ? "" : "hidden"}>Địa chỉ API 9Router (Base URL)<input id="connectionBaseUrl" type="url" autocomplete="off" spellcheck="false" value="${esc(connectionRouter9BaseUrl)}" placeholder="${router9DefaultBaseUrl}"><small>Địa chỉ API từ 9Router, thường kết thúc bằng /v1. Bấm Kiểm tra kết nối để tải danh mục model.</small></label><div id="keysList">Đang đọc trạng thái…</div><label class="section-end">${keyProvider === "router9" ? "Token truy cập 9Router" : "Thêm khóa truy cập (API key)"}<input id="apiKey" type="password" autocomplete="off" spellcheck="false" placeholder="${keyProvider === "router9" ? "Dán token của 9Router" : "Dán khóa của bạn"}"></label><label class="check"><input id="showKey" type="checkbox">Hiện khóa đang nhập</label><button id="saveKey" class="primary">Lưu khóa</button><div id="connectionStatus" class="info">${keyProvider === "router9" ? "Lưu token, tải danh mục rồi chọn đúng ID model do 9Router cung cấp. Chưa gửi nội dung dịch." : "Dán khóa sẽ tự nhận diện nhà cung cấp phổ biến và chọn model mặc định phù hợp. Chưa gửi nội dung hay gọi API."}</div><p id="savedConnection" class="info" role="status"></p><p id="runningConnection" class="muted"></p><label>Mô hình dịch<input id="connectionModel" list="modelOptions" value="${esc(initialModel)}" ${keyProvider === "router9" ? 'placeholder="Chọn ID từ danh mục 9Router"' : ""}><datalist id="modelOptions"></datalist></label><div class="row"><button id="checkKey">Kiểm tra kết nối</button><button id="testGeneration">Thử dịch ngắn</button><button id="useModel" class="primary">Lưu model cho dự án</button></div><p id="connectionDraft" role="status"></p>`;
  $("#modelOptions").innerHTML = connectionModels(keyProvider)
    .map((model) => `<option value="${esc(model)}"></option>`)
    .join("");
  refreshConnectionReceipt();
  refreshConnectionDraft();
  $("#connectionModel").oninput = refreshConnectionDraft;
  $("#keyProvider").onchange = () => {
    connectionRouter9BaseUrl = router9ConnectionBaseUrl();
    keyProvider = $("#keyProvider").value;
    renderConnections();
  };
  $("#connectionBaseUrl").onchange = () => {
    connectionRouter9BaseUrl = router9ConnectionBaseUrl();
    $("#connectionBaseUrl").value = connectionRouter9BaseUrl;
    if (keyProvider !== "router9") return;
    $("#modelOptions").replaceChildren();
    $("#connectionStatus").textContent = "Địa chỉ API đã đổi. Bấm Kiểm tra kết nối để lấy danh mục model tại địa chỉ mới.";
    $("#connectionStatus").className = "info";
    refreshConnectionDraft();
  };
  $("#apiKey").oninput = () => {
    if (keyProvider === "router9") return;
    const value = $("#apiKey").value.trim();
    const detected = /^sk-[A-Za-z0-9]/.test(value)
      ? "deepseek"
      : /^AIza[A-Za-z0-9_-]/.test(value)
        ? "gemini"
        : null;
    if (!detected || detected === keyProvider) return;
    keyProvider = detected;
    $("#keyProvider").value = detected;
    $("#connectionModel").value = connectionModelFor(detected);
    $("#modelOptions").replaceChildren();
    const label = detected === "deepseek" ? "DeepSeek" : "Gemini";
    $("#connectionStatus").textContent = `Định dạng khóa giống ${label}; đã chọn nhà cung cấp và model phù hợp. Khóa chưa được lưu hoặc gửi đi.`;
    $("#connectionStatus").className = "info";
    keyList().catch(() => {});
  };
  $("#showKey").onchange = (e) =>
    ($("#apiKey").type = e.target.checked ? "text" : "password");
  $("#saveKey").onclick = guard(async () => {
    await api("/api/keys/" + keyProvider + "/add", {
      keys: $("#apiKey").value,
    });
    invalidateRouter9Catalog();
    $("#apiKey").value = "";
    $("#apiKey").type = "password";
    $("#showKey").checked = false;
    await keyList();
    notice(
      "Đã lưu token. Model của dự án chưa thay đổi; dùng nút Lưu model cho dự án để áp dụng model đang chọn.",
    );
  });
  $("#checkKey").onclick = guard(() => checkKey(false));
  $("#testGeneration").onclick = guard(() => checkKey(true));
  $("#testGeneration").disabled = !P;
  $("#testGeneration").title = P ? "Gọi thử bằng cấu hình và ngân sách của dự án hiện tại" : "Mở hoặc chọn dự án trước khi thử dịch";
  if (keyProvider === "router9") $("#connectionModel").closest("label").insertAdjacentHTML("afterend", '<small>Dịch phụ đề dùng model hỗ trợ văn bản. Tự xử lý phụ đề với 9Router cần model nhận ảnh; chọn đúng ID từ danh mục 9Router.</small>');
  $("#useModel").onclick = guard(async () => {
    if (!P) return notice("Mở dự án trước khi chọn mô hình.");
    const provider = keyProvider, model = $("#connectionModel").value.trim();
    const baseUrl = provider === "router9" ? router9ConnectionBaseUrl() : null;
    if (!modelMatchesProvider(provider, model)) throw new Error("Chọn đúng ID model; chưa lưu thay đổi.");
    const availableModels = connectionModels(provider);
    if (!availableModels.length) throw new Error("Bấm Kiểm tra kết nối để tải danh mục trước khi lưu model.");
    if (!availableModels.includes(model)) throw new Error("Model không có trong danh mục của kết nối hiện tại. Chưa lưu hoặc tự đổi model.");
    const id = P.id, button = $("#useModel");
    button.disabled = true;
    try {
      await save();
      if (P?.id !== id) throw new Error("Dự án đã đổi; chưa lưu model.");
      const version = editVersion;
      const result = await api("/api/projects/" + id, {expected_revision: P.updated,
        params: {provider, translation_model: model, ...(baseUrl ? {router9_base_url: baseUrl} : {})}}, "PUT");
      if (P?.id !== id) return;
      if (version !== editVersion) {
        conflict = true;
        throw new Error("Model đã được lưu nhưng có bản chỉnh sửa đồng thời. Tải lại dự án trước khi sửa tiếp.");
      }
      if (result.params.provider !== provider || result.params.translation_model !== model ||
          (baseUrl && result.params.router9_base_url !== baseUrl)) throw new Error("Phản hồi lưu không khớp model đã chọn. Tải lại dự án để kiểm tra.");
      P = result; dirty = false; quality = null;
      $("#saved").textContent = "Đã lưu";
      refreshConnectionReceipt(); refreshConnectionDraft();
      $("#connectionStatus").textContent = `Đã lưu model trên máy chủ: ${connectionContractText(result.params)}. Không gửi yêu cầu dịch.`;
      $("#connectionStatus").className = "info";
      notice(`Đã lưu model cho dự án: ${result.params.translation_model}.`);
    } catch (error) {
      $("#connectionDraft").textContent = "Chưa xác nhận lưu model: " + error.message;
      $("#connectionDraft").className = "warning";
      throw error;
    } finally { if (button.isConnected) button.disabled = false; }
  });
  await keyList();
}
async function keyList() {
  const provider = keyProvider;
  const rows = await api("/api/keys/" + provider);
  if (!$("#keysList") || provider !== keyProvider) return;
  keyRows = rows;
  $("#keysList").innerHTML =
    rows
      .map(
        (r) =>
          `<div class="key-row"><div><code>${esc(r.masked)}</code><small> · ${r.active ? "Đang dùng" : "Chưa chọn"}${r.check ? " · đã kiểm " + new Date(r.check.checked_at * 1000).toLocaleString("vi-VN") : ""}</small></div><button data-select-key="${esc(r.id)}" ${r.active ? "disabled" : ""}>Dùng</button><button data-delete-key="${esc(r.id)}">Xóa</button></div>`,
      )
      .join("") || '<p class="warning">Chưa lưu khóa cho nhà cung cấp này.</p>';
  $$("[data-select-key]").forEach(
    (b) =>
      (b.onclick = guard(async () => {
        await api("/api/keys/" + keyProvider + "/select", {
          id: b.dataset.selectKey,
        });
        invalidateRouter9Catalog();
        await keyList();
      })),
  );
  $$("[data-delete-key]").forEach(
    (b) =>
      (b.onclick = guard(async () => {
        if (
          await ask(
            "Xóa khóa đã lưu?",
            "Chỉ xóa khóa đã chọn khỏi NPK. Dự án và video được giữ nguyên.",
            "Xóa khóa",
          )
        ) {
          await api("/api/keys/" + keyProvider + "/delete", {
            id: b.dataset.deleteKey,
          });
          invalidateRouter9Catalog();
          await keyList();
        }
      })),
  );
}
async function checkKey(generation) {
  if (generation && !P) {
    $("#connectionStatus").textContent = "Mở hoặc chọn dự án trước khi thử dịch để kiểm tra cấu hình và ngân sách API.";
    $("#connectionStatus").className = "warning";
    return;
  }
  const provider = keyProvider,
    model = $("#connectionModel").value.trim(),
    catalogKey = connectionCatalogKey(provider),
    baseUrl = provider === "router9" ? router9ConnectionBaseUrl() : null;
  if (provider === "router9") {
    let validEndpoint = false;
    try { validEndpoint = ["http:", "https:"].includes(new URL(baseUrl).protocol); } catch (_) {}
    if (!validEndpoint) {
      $("#connectionStatus").textContent = "Nhập địa chỉ API 9Router đầy đủ bắt đầu bằng http:// hoặc https://.";
      $("#connectionStatus").className = "warning";
      return;
    }
  }
  if ((generation || provider !== "router9") && !modelMatchesProvider(provider, model)) {
    $("#connectionStatus").textContent =
      "Model không khớp nhà cung cấp; chưa gửi request. Hãy kiểm tra danh mục model để tự chọn model phù hợp.";
    $("#connectionStatus").className = "warning";
    return;
  }
  if (!generation) {
    connectionModelCatalogs[catalogKey] = [];
    $("#modelOptions").replaceChildren();
  }
  const knownModels = connectionModelCatalogs[catalogKey] || [];
  if (generation && !knownModels.length) {
    $("#connectionStatus").textContent =
      "Hãy bấm Kiểm tra kết nối trước để tải model đang được nhà cung cấp hỗ trợ. Chưa phát sinh request thử sinh.";
    $("#connectionStatus").className = "warning";
    return;
  }
  if (generation && knownModels.length && !knownModels.includes(model)) {
    if (provider === "router9") {
      $("#connectionStatus").textContent = "Chọn đúng ID model trong danh mục 9Router trước khi thử dịch. Chưa gửi request thử sinh.";
      $("#connectionStatus").className = "warning";
      return;
    }
    const nextModel = connectionModelFor(provider, "", knownModels);
    $("#connectionModel").value = nextModel;
    $("#connectionStatus").textContent = `Model đã được cập nhật thành ${nextModel} theo danh mục hiện tại. Kiểm tra lại trước khi thử dịch.`;
    $("#connectionStatus").className = "info";
    return;
  }
  if (
    generation &&
    !(await ask(
      "Thử dịch ngắn?",
      "Một yêu cầu ngắn sẽ gửi tới nhà cung cấp bằng khóa đang dùng. Có thể phát sinh phí.",
      "Gọi thử",
    ))
  )
    return;
  $("#connectionStatus").textContent = "Đang kiểm tra với nhà cung cấp…";
  const r = await api("/api/keys/" + provider + "/check", {
    mode: generation ? "generation" : "catalog",
    model,
    confirm_cost: generation,
    project_id: P?.id,
    ...(provider === "router9" ? {base_url: baseUrl} : {}),
  });
  if (!$("#connectionStatus") || provider !== keyProvider || catalogKey !== connectionCatalogKey(provider)) return;
  $("#connectionStatus").textContent =
    r.message ||
    r.detail ||
    (r.ok
      ? generation
        ? "Mô hình đã trả lời yêu cầu thử."
        : "Kết nối được; đã lấy danh mục mô hình. Chưa kiểm hạn mức sinh nội dung."
      : "Chưa kết nối được.");
  $("#connectionStatus").className = r.ok ? "info" : "warning";
  if (provider === "router9" && r.ok && r.base_url !== baseUrl) {
    $("#connectionStatus").textContent = "Địa chỉ API trong kết quả kiểm tra không khớp. Tải lại danh mục từ địa chỉ 9Router đang nhập trước khi chọn model.";
    $("#connectionStatus").className = "warning";
    return;
  }
  if (r.models && (provider !== "router9" || r.ok)) {
    const models = r.models.map(m => typeof m === "string" ? m : m.id || m.name).filter(Boolean);
    connectionModelCatalogs[catalogKey] = models;
    $("#modelOptions").innerHTML = models
      .map(
        (m) =>
          `<option value="${esc(typeof m === "string" ? m : m.id || m.name)}"></option>`,
      )
      .join("");
    const currentModel = $("#connectionModel").value.trim();
    if (provider === "router9") {
      if (currentModel && !models.includes(currentModel)) $("#connectionStatus").textContent += " Model đang hiển thị không có trong danh mục; chưa thay cấu hình đã lưu.";
      $("#connectionStatus").textContent += models.length ? ` Có ${models.length} model. Chọn đúng ID để dùng cho dự án.` : " Danh mục chưa có model; kiểm tra cấu hình 9Router.";
    } else if (models.length && !models.includes(currentModel)) {
      const nextModel = connectionModelFor(
        provider,
        P?.params.provider === provider ? P?.params.translation_model || "" : "",
        models,
      );
      $("#connectionModel").value = nextModel;
      $("#connectionStatus").textContent += ` Đã tự cập nhật model thành ${nextModel} từ danh mục API.`;
    }
  }
  refreshConnectionDraft();
  await keyList();
}
async function renderPerformance() {
  const body = $("#settingsBody");
  body.innerHTML = `${
    P
      ? field("workload_mode", "Tải xử lý cho dự án hiện tại", "select", [
          ["quiet", "Êm · 1 luồng, có nhịp nghỉ"],
          ["balanced", "Cân bằng · 2 luồng"],
          ["fast", "Nhanh · 4 luồng"],
        ])
      : "<p>Mở dự án để thay chế độ xử lý.</p>"
  }<div class="info">Giảm tải có thể kéo dài thời gian. Không thay đổi chế độ nguồn Windows, không ép xung và không hạ chất lượng ngầm.</div><button id="openStorageSettings">Quản lý dung lượng và thùng rác</button>`;
  bindParams();
  $('#openStorageSettings').onclick = openStorage;
}
function renderPreferences() {
  $("#settingsBody").innerHTML =
    `<div class="card"><h3>Tự lưu và phục hồi</h3><p>Tự lưu sau khi ngừng gõ; giữ tối đa 30 phiên bản dự án. Xung đột được báo trước, không tự ghi đè.</p><button id="openHistory">Mở lịch sử dự án</button></div><div class="card"><h3>Bố cục</h3><p>Khung xem và công cụ luôn ở cạnh nhau trên màn rộng. Không có timeline nhiều tầng.</p><button id="resetRatio">Khôi phục bố cục</button></div><div class="card"><h3>Mặc định dự án mới</h3><p>Chỉ áp dụng cho dự án tạo sau này; không sửa các dự án đã có.</p><button id="saveDefaults" ${P ? "" : "disabled"}>Dùng thông số dự án hiện tại làm mặc định</button></div><h3>Phím tắt</h3><p>Space: phát / dừng · ← / →: chuyển khung<br>Ctrl+S: lưu · Ctrl+Z: hoàn tác · Ctrl+Y: làm lại<br>Escape: đóng hộp thoại</p>`;
  $("#openHistory").onclick = () => setArea("projects");
  $("#resetRatio").onclick = () => {
    $("#viewerRatio").value = 54;
    document.documentElement.style.setProperty("--viewer", "54%");
    localStorage.removeItem("npk-viewer-ratio");
    window.NPKUpgrade?.reset('viewer_ratio', 54);
  };
  $("#saveDefaults").onclick = guard(async () => {
    await save();
    if (
      await ask(
        "Lưu mặc định dự án mới?",
        "Thông số xử lý và kiểu phụ đề sẽ áp dụng cho dự án tạo sau này. Không lưu đường dẫn âm thanh của dự án làm mặc định.",
        "Lưu mặc định",
      )
    ) {
      await api(
        "/api/settings",
        {
          ...P.params,
          voice_path: "",
          bgm_path: "",
          extraction_start: 0,
          extraction_end: 0,
        },
        "PUT",
      );
      notice("Đã lưu mặc định cho dự án mới.");
    }
  });
}
function renderHelp() {
  $("#settingsBody").innerHTML =
    `<div class="card"><h3>NPK Auto Sub · 0.8.5 RC9</h3><p>1. Tạo hoặc nhập phụ đề.<br>2. Dịch và kiểm tra nội dung.<br>3. Chọn giọng, xem thử cách xử lý chữ.<br>4. Xuất video và duyệt thành phẩm.</p><p class="muted">Video chỉ xử lý chữ chưa có phụ đề mới hay lồng tiếng. Nút “Mở video thành phẩm” mở đúng bản đã ghép cuối. Mục Tạo giọng hỗ trợ kịch bản nhiều đoạn, nhiều giọng và thư viện bản đọc.</p></div><details><summary>Thông tin xử lý trên máy</summary><p>Ứng dụng dùng máy chủ nội bộ chỉ nghe ở 127.0.0.1; đây không phải địa chỉ tải video lên mạng. Dịch phụ đề và gợi ý rút gọn gọi nhà cung cấp đã chọn. Nhận dạng, xử lý chữ, giọng Việt, waveform và gợi ý nhóm người nói chạy cục bộ.</p><div id="engineNotes"></div></details><details><summary>Giới hạn cần biết</summary><p>Phục hồi nền không bảo đảm giống nền gốc hoặc hết nhấp nháy. Gợi ý nhóm người nói cần nghe lại; phân tích nhịp dựa trên năng lượng và khoảng lặng chưa tự hiểu cảm xúc. Tách giọng/nhạc UVR là tùy chọn offline CPU; cần nghe lại hai stem trước khi dùng. Nhạc hoặc lời chồng nhau có thể làm giảm chất lượng.</p></details><button id="diagnostics">Xem chẩn đoán tác vụ</button><p class="muted">Không gửi key hoặc dữ liệu dự án cho bộ phận hỗ trợ khi chưa kiểm nội dung.</p>`;
  $("#engineNotes").innerHTML = (catalog?.engines?.items || [])
    .map(
      (r) =>
        `<p>${esc(r.name)}: ${r.available ? "có trên máy" : "chưa tích hợp"}</p>`,
    )
    .join("");
  $("#diagnostics").onclick = () => {
    setProgressExpanded(true);
    if (lastDetail || jobs[0]) guard(showJob)(lastDetail || jobs[0].id);
  };
  $("#settingsBody").insertAdjacentHTML(
    "beforeend",
    '<button id="supportBundle">Xem và xuất thông tin hỗ trợ</button>',
  );
  $("#supportBundle").onclick = guard(async () => {
    const report = await api("/api/studio/diagnostics");
    openModal(
      "Thông tin hỗ trợ",
      `<p class="info">Chỉ có phiên bản, cấu hình máy và trạng thái tác vụ. Không kèm khóa, video, phụ đề, tên tệp hoặc log tự do. Bạn xem nội dung rồi tự chọn có chia sẻ hay không.</p><pre class="technical">${esc(JSON.stringify(report, null, 2))}</pre>`,
      [
        { label: "Đóng", run: closeModal },
        {
          label: "Lưu tệp hỗ trợ",
          primary: true,
          run: () => {
            downloadDocument(report, "NPK-chan-doan.json");
            closeModal();
          },
        },
      ],
    );
  });
}
const kindNames = {
  auto_subtitles: "Tự xử lý phụ đề",
  subtitle_preview: "Xem thử kiểu phụ đề",
  asr: "Nghe lời thoại",
  ocr: "Đọc chữ trong video",
  translate: "Dịch phụ đề",
  tts: "Tạo lồng tiếng",
  clean: "Xử lý chữ",
  clip: "Xem thử xử lý chữ",
  preview: "Xem thử một khung",
  detect: "Gợi ý vùng chữ",
  scan: "Theo dõi vùng chữ",
  render: "Xuất video",
  pipeline: "Quy trình làm video",
  voice_preview: "Nghe thử giọng",
  text_to_speech: "Tạo giọng từ văn bản",
  analyze_audio: "Phân tích nhịp giọng nguồn",
  speaker_groups: "Gợi ý nhóm người nói",
  enroll_voice: "Tạo giọng riêng từ mẫu",
  optimize_spoken: "Gợi ý rút gọn lời đọc",
  separate_audio: "Tách giọng / nhạc nguồn · CPU",
};
const stateNames = {
  queued: "Đang chờ",
  running: "Đang xử lý",
  done: "Đã xử lý xong",
  review: "Cần xem lại phụ đề",
  error: "Đã dừng do lỗi",
  cancelled: "Đã hủy",
};
function jobTimingInfo(job, now = Date.now() / 1000) {
  const valid = value => typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null;
  const created = valid(job?.created), started = valid(job?.started), ended = valid(job?.ended);
  const terminal = ['done','review','error','cancelled'].includes(job?.state);
  const finish = terminal ? ended : valid(now);
  return {created,started,ended,terminal,
    elapsed_seconds: started !== null && finish !== null && finish >= started ? finish-started : null,
    wait_seconds: created !== null && (started ?? finish) !== null && (started ?? finish) >= created ? (started ?? finish)-created : null};
}
function processingDuration(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '';
  const total = Math.floor(seconds), hours = Math.floor(total/3600), minutes = Math.floor(total%3600/60), rest = total%60;
  return (hours ? `${hours} giờ ` : '') + (hours || minutes ? `${minutes} phút ` : '') + `${rest} giây`;
}
function jobTimingLabel(job, now = Date.now() / 1000) {
  const timing = jobTimingInfo(job,now);
  if (job?.state === 'queued') return timing.wait_seconds === null ? '' : 'Chờ '+processingDuration(timing.wait_seconds);
  if (timing.elapsed_seconds === null) return '';
  return (job?.state === 'running' ? 'Đã xử lý ' : 'Thời gian xử lý ') + processingDuration(timing.elapsed_seconds);
}
function jobEndedLabel(job) {
  const timing = jobTimingInfo(job);
  if (!timing.terminal || timing.ended === null) return '';
  const time = new Date(timing.ended*1000).toLocaleString('vi-VN',{hour12:false});
  return (job.state === 'error' ? 'Dừng do lỗi lúc ' : job.state === 'cancelled' ? 'Đã hủy lúc ' : 'Kết thúc lúc ') + time + '.';
}
function jobDiagnostics(job) {
  const timing = jobTimingInfo(job), stopped = jobEndedLabel(job), label = jobTimingLabel(job);
  const usage = job.api_usage, requests = usage && Number.isInteger(usage.request_attempts) && Number.isInteger(usage.request_limit)
    ? `<p data-job-api-usage="true">Đã gửi ${esc(usage.request_attempts)}/${esc(usage.request_limit)} lượt API trong tác vụ này. Token chỉ được tính khi dịch vụ trả số đo; chưa xác nhận chi phí.</p>` : '';
  const cumulative = usage && ['lineage_request_attempts','lineage_request_limit'].every(key=>Number.isInteger(usage[key])&&usage[key]>=0)
    ? `<p data-job-api-lineage="true">${usage.history_unknown_jobs>0?'Đã đo được':'Đã ghi nhận'} ${esc(usage.lineage_request_attempts)}/${esc(usage.lineage_request_limit)} lượt API của quy trình.${usage.history_unknown_jobs>0?' Lịch sử các lượt cũ chưa đầy đủ; chưa xác nhận tổng số lượt.':' Chạy tiếp dùng chung ngân sách này.'}</p>` : '';
  const cueCounts = usage && ['reused_cues','newly_completed_cues','remaining_cues'].every(key=>Number.isInteger(usage[key])&&usage[key]>=0)
    ? `<p data-job-translation-counts="true">${esc(usage.reused_cues)} câu dùng lại · ${esc(usage.newly_completed_cues)} câu dịch mới · ${esc(usage.remaining_cues)} câu còn thiếu.</p>` : '';
  const source = P && P.id === job.project_id && (P.source_revision||null) === (job.source_revision||null) ? P.media?.duration : null;
  const lastProgress = ['error','cancelled'].includes(job.state) ? `Tiến độ ghi nhận trước khi dừng: ${Math.max(0,Math.min(100,Math.round((Number(job.progress)||0)*100)))}%.` : '';
  const incomplete = job.kind === 'pipeline' && job.state === 'review';
  const stage = ['error','cancelled'].includes(job.state) ? jobFailureStage(job) : null;
  return `<section role="status" data-job-status="${esc(job.state)}" class="${job.state==='error'||incomplete?'warning':'info'}"><strong>${incomplete?'Quy trình tạm dừng · Chưa có video thành phẩm':esc(stateNames[job.state] || job.state)}${timing.terminal?' · Không còn xử lý':''}</strong>${stage?`<p data-job-failure-stage="${esc(stage.id)}">Dừng ở bước: ${esc(stage.name)}.</p>`:''}${job.configuration?.translation_model ? `<p data-job-connection>${esc(connectionContractText(job.configuration))}</p>` : ""}<p data-job-identity>Tác vụ ${esc(job.id)}${job.created ? " · Tạo lúc " + esc(new Date(job.created * 1000).toLocaleString("vi-VN")) : ""}</p>${requests}${cumulative}${cueCounts}${label?`<p>${esc(label)}.</p>`:''}${stopped?`<p>${esc(stopped)}</p>`:''}${lastProgress?`<p>${esc(lastProgress)}</p>`:''}${Number.isFinite(source)?`<p>Độ dài video nguồn: ${esc(processingDuration(source))}. Đây là thời lượng video, khác với thời gian xử lý.</p>`:''}</section>`;
}
function jobFailureStage(job) {
  const names = window.NPKSingleVideoSetup?.stepNames || {ocr:'Đọc chữ',asr:'Nghe lời thoại',auto_subtitles:'Tự xử lý phụ đề',translate:'Dịch phụ đề',tts:'Tạo giọng',clean:'Xử lý chữ gốc',render:'Xuất video'};
  const id = [job.failure_stage, job.stage].map(value => String(value || '').split(':').pop()).find(value => Object.hasOwn(names, value));
  return id ? {id, name: names[id]} : null;
}
function safeMessage(text) {
  return String(text || "")
    .replace(/\b(?:[A-Z]:\\)[^\n]+/gi, "[Chi tiết ở chẩn đoán]")
    .replace(
      /\b(?:OCR|ASR|TTS|LaMa|stderr|snapshot)\b/g,
      (v) =>
        ({
          OCR: "Đọc chữ",
          ASR: "Nghe lời thoại",
          TTS: "Tạo giọng",
          LaMa: "Phục hồi nền",
          stderr: "nhật ký",
          snapshot: "phiên bản",
        })[v],
    )
    .slice(0, 320);
}
function renderProgress() {
  updateRecognitionUI();
  const relevant = jobs.filter((j) => area === "text_to_speech" ? j.kind === "text_to_speech" : !P || j.project_id === P.id),
    active =
      jobs.find((j) => j.state === "running") ||
      jobs.find((j) => j.state === "queued"),
    last = active || relevant[0];
  $("#progressText").textContent = last
    ? `${kindNames[last.kind] || "Tác vụ"} · ${last.kind==='pipeline'&&last.state==='review'?'Chưa xuất video · Cần xem lại phụ đề':stateNames[last.state] || last.state}${last.state === "running" ? " · " + Math.round(last.progress * 100) + "%" : ""}${jobTimingLabel(last)?' · '+jobTimingLabel(last):''}${last.state==='error'?' · Không còn chạy':''}${active && P && last.project_id!==P.id?' · '+(last.project_name||'Dự án khác'):''}`
    : "Không có tác vụ đang chạy";
  $("#progress").value = last?.progress || 0;
  $("#progress").hidden = !last || !['queued','running'].includes(last.state);
  $("#progress").setAttribute('aria-valuetext',last ? stateNames[last.state] || last.state : 'Không có tác vụ');
  $("#progressText").dataset.state = last?.state || 'idle';
  $("#jobList").innerHTML = relevant
    .slice(0, 20)
    .map(
      (j) =>
        `<button data-job="${j.id}">${kindNames[j.kind] || "Tác vụ"} · ${stateNames[j.state] || j.state}</button>`,
    )
    .join("");
  $$("[data-job]").forEach(
    (b) => (b.onclick = guard(() => showJob(b.dataset.job))),
  );
}
function translationRetryPayload(job) {
  if (job.can_resume) return {resume_from: job.id,
    ...(job.segment_indices ? {segment_indices: job.segment_indices} : {})};
  if (job.kind === "translate") {
    if (!Array.isArray(job.retry_segment_indices) || !job.retry_segment_indices.length)
      throw new Error(job.translation_retry_error || "Không xác minh được các câu còn thiếu của lượt trước. Chọn lại phạm vi trong Dịch phụ đề.");
    return {retry_from: job.id, segment_indices: job.retry_segment_indices};
  }
  return {};
}
function jobEventRows(job) {
  const events = Array.isArray(job.events) ? job.events : [];
  const history = events.map(event => `<div class="step">${icon('check')}<span>${esc(safeMessage(event.message))}</span></div>`).join('');
  if (!['error', 'cancelled'].includes(job.state)) return history;
  const stopped = job.state === 'error' ? 'Đã dừng do lỗi' : 'Đã hủy';
  // Worker history describes the last attempt, not the terminal outcome. Keep
  // a retry/wait event for diagnosis without presenting it as current activity.
  return `<div class="step" data-job-terminal-message="${esc(job.state)}">${icon('close')}<span>${esc(stopped)} · ${esc(safeMessage(job.message) || 'Không còn xử lý tác vụ này.')}</span></div>${history ? `<details data-job-event-history><summary>Nhật ký trước khi ${job.state === 'error' ? 'dừng' : 'hủy'}</summary>${history}</details>` : ''}`;
}
async function showJob(id) {
  lastDetail = id;
  const j = await api("/api/studio/jobs/" + id);
  if(j.result?.primary_file==='final.mp4')rememberFinalJob(j);
  if (lastDetail !== id) return;
  if (j.storage_removed) {
    $('#jobDetails').innerHTML = '<h3>Dữ liệu tác vụ đã được dọn</h3><p>Nhật ký được giữ để tra cứu. Nếu mục còn trong thùng rác, có thể khôi phục; nếu đã xóa vĩnh viễn, cần tạo lại dữ liệu.</p><button id="showStorageFromJob">Mở dung lượng & thùng rác</button>';
    $('#showStorageFromJob').onclick = () => { $('#closeProgress').click(); openStorage(); };
    return;
  }
  if (lastDetail !== id) return;
  $("#jobDetails").innerHTML =
    `<h3>${esc(kindNames[j.kind] || "Tác vụ")} · ${esc(stateNames[j.state] || j.state)}</h3>${jobDiagnostics(j)}${['error','cancelled'].includes(j.state) ? '' : `<p>${esc(safeMessage(j.message))}</p>`}${jobEventRows(j)}<div class="row">${["running", "queued"].includes(j.state) ? '<button id="cancelCurrent">Hủy tác vụ</button>' : ""}${j.state === "review" ? '<button id="reviewCurrent" class="primary">Kiểm tra phụ đề</button>' : ""}${j.state === "done" && j.result?.primary_file === "final.mp4" ? '<button id="jobFinal" class="primary">Mở video thành phẩm</button>' : ""}${["error", "cancelled"].includes(j.state) ? '<button id="retryCurrent">Chạy lại với dự án hiện tại</button>' : ""}</div>${j.result?.warnings?.length ? `<div class="warning">${j.result.warnings.slice(0, 20).map(esc).join("<br>")}</div>` : ""}<details><summary>Chi tiết chẩn đoán</summary><pre>${esc(JSON.stringify({ configuration: j.configuration, state:j.state, stage:j.stage, failure_stage:j.failure_stage, failure_code:j.failure_code, next_action:j.next_action, started:j.started, ended:j.ended, actual_elapsed_seconds:jobTimingInfo(j).elapsed_seconds, worker_phase_elapsed_seconds:j.elapsed_seconds, completed:j.completed, total:j.total, unit:j.unit, message: j.message, verification: j.result?.verification, reused: j.result?.reused, cache_hits: j.result?.cache_hits }, null, 2))}</pre><button id="openTechnicalFolder">Mở thư mục tác vụ</button></details>`;
  if (j.kind === 'pipeline' && (j.state === 'review' || j.continuation?.job_id)) {
    const plan = j.continuation || await api('/api/studio/jobs/' + id + '/continuation');
    if (lastDetail !== id || !$("#jobDetails .row")) return;
    const names = window.NPKSingleVideoSetup.stepNames;
    const view = window.NPKSingleVideoSetup.continuationPresentation(plan, P?.id === j.project_id ? P.segments.length : 0);
    const explanation = document.createElement('div'); explanation.className='setup-pipeline-state warning';
    explanation.innerHTML=`<p>${view.savedText ? esc(view.savedText)+' ' : ''}${esc(view.reason)}</p><div class="setup-stage-list">${(plan.completed_steps||[]).map(step=>`<span data-stage-state="done">✓ ${esc(names[step]||step)}</span>`).join('')}${(plan.remaining_steps||[]).map(step=>`<span data-stage-state="pending">○ ${esc(names[step]||step)}</span>`).join('')}</div>${(plan.errors||[]).map(message=>`<p>${esc(message)}</p>`).join('')}`;
    if (plan.recovery_job_id && plan.recovery_job_id !== j.id) {
      const latest = document.createElement('p'); latest.dataset.newerContinuation = plan.recovery_job_id;
      latest.textContent = `Kế hoạch tiếp tục bên dưới lấy kết quả mới hơn từ tác vụ ${plan.recovery_job_id}. Lỗi phía trên thuộc tác vụ ${j.id}. `;
      const show = document.createElement('button'); show.textContent = 'Xem tác vụ mới hơn';
      show.onclick = guard(() => showJob(plan.recovery_job_id)); latest.append(show); explanation.prepend(latest);
    }
    if(plan.recovery_job_id&&view.translationText)explanation.insertAdjacentHTML('beforeend',`<p>${esc(view.translationText)}</p>`);
    $("#jobDetails .row").before(explanation);
    const resume=document.createElement('button');resume.id='continuePipelineCurrent';resume.className='primary';resume.textContent=view.button;resume.disabled=!plan.can_continue;
    resume.onclick=guard(async()=>{
      resume.disabled=true;
      try{
        if(P?.id!==j.project_id)await loadProject(j.project_id);
        await continuePausedPipeline(j.id);
        notice(view.startedText);
      }finally{if(resume.isConnected)resume.disabled=!plan.can_continue;}
    });
    $("#jobDetails .row").prepend(resume);
    if($("#retryCurrent"))$("#retryCurrent").hidden=true;
    if($("#reviewCurrent"))$("#reviewCurrent").classList.toggle('primary',!plan.can_continue);
  }
  if (j.state === 'done' && j.result?.primary_file === 'final.mp4') {
    const path = j.result.published?.path;
    const location = document.createElement('p');location.className='setup-result-path';
    location.textContent=path?'Video đã lưu: '+path:'Video đã lưu trong thư mục tác vụ; có thể mở hoặc lưu bản sao sang nơi xuất.';
    $("#jobDetails .row").before(location);
    const folder=document.createElement('button');folder.id='jobFinalFolder';folder.textContent='Mở thư mục';
    folder.onclick=guard(()=>api('/api/studio/jobs/'+id+'/open-result-folder',{}));
    $("#jobDetails .row").append(folder);
  }
  if (j.translation_recovery_error) {
    const warning = document.createElement("p");
    warning.className = "warning";
    warning.textContent = j.translation_recovery_error;
    $("#jobDetails .row").before(warning);
  }
  if (j.translation_retry_error && !j.can_resume) {
    const warning = document.createElement("p");
    warning.className = "warning";
    warning.textContent = j.translation_retry_error;
    $("#jobDetails .row").before(warning);
    if ($("#retryCurrent")) $("#retryCurrent").disabled = true;
  }
  if (j.can_restore_translation && j.restorable_translation_count > 0) {
    const hint = document.createElement("p");
    hint.className = "info";
    hint.textContent = `${j.restorable_translation_count} câu đã dịch được lưu trong checkpoint và có thể đưa vào các câu còn trống. Các câu bạn đã sửa tay được giữ nguyên.`;
    $("#jobDetails .row").before(hint);
    const restore = document.createElement("button");
    restore.id = "restoreTranslationCurrent";
    restore.className = "primary";
    restore.textContent = `Khôi phục ${j.restorable_translation_count} câu đã dịch`;
    restore.onclick = guard(async () => {
      restore.disabled = true;
      try {
        if (P?.id !== j.project_id) await loadProject(j.project_id);
        await save();
        const projectId = P.id, version = editVersion;
        const restored = await api("/api/studio/jobs/" + id + "/apply", {expected_revision: P.updated});
        if (P?.id !== projectId) return;
        if (dirty || saving || editVersion !== version) {
          conflict = true;
          $("#saved").textContent = "Có bản khôi phục mới";
          notice("Bản dịch dở đã được lưu vào dự án. Nội dung bạn đang nhập vẫn giữ trong cửa sổ; cần kiểm tra trước khi tải lại dự án.", true);
          return;
        }
        P = restored;
        quality = null;
        undo = []; redo = [];
        renderPanel(); updateUndo();
        notice("Đã khôi phục các câu dịch còn trống. Chọn Dịch phụ đề → Câu chưa dịch để làm tiếp.");
        await poll(); await showJob(id);
      } finally { restore.disabled = false; }
    });
    $("#jobDetails .row").append(restore);
  }
  if ($("#cancelCurrent"))
    $("#cancelCurrent").onclick = guard(async () => {
      if (
        await ask(
          "Hủy tác vụ đang chạy?",
          "Kết quả đã hoàn tất trước đó và video nguồn vẫn được giữ.",
          "Hủy tác vụ",
        )
      ) {
        await api("/api/jobs/" + id + "/cancel", {});
        await poll();
        await showJob(id);
      }
    });
  if ($("#reviewCurrent"))
    $("#reviewCurrent").onclick = guard(async () => {
      await loadProject(j.project_id);
      subtab = "quality";
      setArea("subtitles");
      setProgressExpanded(false);
    });
  if ($("#jobFinal"))
    $("#jobFinal").onclick = guard(() =>
      api("/api/studio/jobs/" + id + "/open-result", {}),
    );
  if ($("#retryCurrent"))
    $("#retryCurrent").onclick = guard(async () => {
      if (j.kind === "text_to_speech") {
        standaloneSpeech.jobId = j.id;
        standaloneSpeechDetail = j;
        storePreference("npk-tts-job", j.id);
        setArea("text_to_speech");
        setProgressExpanded(false);
        notice("Kiểm tra bản nháp và giọng đã chọn, rồi bấm Tạo âm thanh để thử lại.");
        return;
      }
      if (P?.id !== j.project_id) await loadProject(j.project_id);
      if (
        await ask(
          "Chạy lại tác vụ?",
          j.can_resume
            ? (["translate", "pipeline"].includes(j.kind) ? "Tiếp tục các câu chưa dịch từ checkpoint khớp video, phụ đề và cấu hình. Nếu đã thay đổi, NPK sẽ báo để bạn kiểm tra." : "Tiếp tục từ tiến độ nhận dạng đã lưu nếu nguồn, vùng và thông số còn khớp.")
            : j.kind === "translate" && j.retry_segment_indices?.length
              ? `Chỉ dịch tiếp ${j.retry_segment_indices.length} câu còn trống thuộc phạm vi lượt trước. Giữ nguyên các câu đã dịch hoặc đã sửa tay.`
            : ["preview", "clip", "clean"].includes(j.kind)
              ? "Dùng vùng và phương pháp hiện tại. Mẫu thử lấy tại con trỏ video; gần cuối sẽ lùi lại để đủ thời lượng. Video nguồn và kết quả trước giữ nguyên."
            : "Dùng nội dung và thông số hiện tại. Kết quả trước giữ nguyên; giọng không đổi có thể tái dùng bộ nhớ đệm.",
          "Chạy lại",
        )
      )
        await queue(j.kind, {
          replace_segments: true,
          ...translationRetryPayload(j),
        });
    });
  if (j.kind === "text_to_speech" && $("#retryCurrent"))
    $("#retryCurrent").textContent = "Về Tạo giọng để thử lại";
  if (j.kind === "text_to_speech" && j.state === "done") {
    const button = document.createElement("button");
    button.textContent = "Nghe và tải giọng";
    button.className = "primary";
    button.onclick = () => {
      standaloneSpeech.jobId = j.id;
      standaloneSpeechDetail = j;
      storePreference("npk-tts-job", j.id);
      setArea("text_to_speech");
      setProgressExpanded(false);
    };
    $("#jobDetails .row").append(button);
  }
  if (j.can_resume && $("#retryCurrent"))
    $("#retryCurrent").textContent = ["translate", "pipeline"].includes(j.kind) ? "Tiếp tục bản dịch dở" : "Tiếp tục nhận dạng đã lưu";
  else if (j.kind === "translate" && j.retry_segment_indices?.length && $("#retryCurrent"))
    $("#retryCurrent").textContent = `Dịch tiếp ${j.retry_segment_indices.length} câu còn thiếu`;
  if (j.kind === "subtitle_preview" && j.state === "done")
    $("#jobDetails").insertAdjacentHTML(
      "afterbegin",
      `<img style="width:100%;max-height:55vh;object-fit:contain" alt="Phụ đề vẽ bằng bộ xuất" src="${mediaUrl("/media/job/" + id + "/subtitle-preview.png")}">`,
    );
  $("#openTechnicalFolder").onclick = guard(() =>
    api("/api/jobs/" + id + "/open", {}),
  );
}
function setProgressExpanded(expanded) {
  $("#progressDetails").hidden = !expanded;
  $("#progressToggle").setAttribute("aria-expanded", String(expanded));
  $("#progressToggle").title = expanded
    ? "Thu gọn tiến trình"
    : "Bấm để xem chi tiết tiến trình";
}
$("#progressToggle").onclick = () => {
  setProgressExpanded($("#progressDetails").hidden);
  if (!$("#progressDetails").hidden && (lastDetail || jobs[0]))
    guard(showJob)(lastDetail || jobs[0].id);
};
$("#closeProgress").onclick = () => {
  setProgressExpanded(false);
};
const originalShowJob = showJob;
showJob = async function (id) {
  await originalShowJob(id);
  if (lastDetail !== id) return;
  const j = await api("/api/studio/jobs/" + id);
  if (lastDetail !== id) return;
  if (j.storage_removed) return;
  if (
    j.result?.segments?.length ||
    (j.kind === "detect" && j.state === "done") ||
    j.can_restore_translation
  ) {
    const button = document.createElement("button");
    button.id = "applyJobResult";
    button.textContent =
      j.kind === "detect"
        ? "Dùng vùng chữ được gợi ý"
        : ["error", "cancelled"].includes(j.state)
          ? "Phục hồi các câu đã dịch"
          : "Áp dụng bảng phụ đề của tác vụ";
    button.onclick = guard(async () => {
      if (!P || P.id !== j.project_id) await loadProject(j.project_id);
      await save();
      if (
        await ask(
          "Áp dụng kết quả tác vụ?",
          j.kind === "detect"
            ? "Các vùng hiện tại sẽ được thay bằng vùng gợi ý. Bạn có thể hoàn tác hoặc sửa lại."
            : "Bảng kết quả có thể khác bản bạn đang sửa. Bản hiện tại được giữ trong lịch sử. Với tác vụ lỗi, chỉ điền câu dịch còn thiếu và khớp lời gốc.",
          "Áp dụng",
        )
      ) {
        P = await api("/api/studio/jobs/" + id + "/apply", {
          expected_revision: P.updated,
        });
        selected.clear();
        setArea(j.kind === "detect" ? "clean" : "subtitles");
        drawRegions();
        notice("Đã áp dụng kết quả.");
        setProgressExpanded(false);
      }
    });
    $("#jobDetails").prepend(button);
  }
  window.NPKUpgrade?.job(j);
};
let pollBusy = false,
  pollTimer = null,
  pendingCompletedJob = null,
  stateCursor = 0;
function panelHasFocusedEditor() {
  if($('#modal').open)return true;
  const active = document.activeElement;
  return !!active && panel.contains(active) &&
    (active.matches("input, textarea, select") || active.isContentEditable);
}
async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    const data = await api('/api/studio/state?after='+encodeURIComponent(stateCursor)+(P ? '&project_id=' + encodeURIComponent(P.id) : '')),
      previous = new Map(jobs.map((j) => [j.id, j.state]));
    latestRecognition = data.recognition || null;
    window.NPKUpgrade?.state(data);
    projects = data.projects;
    // Active executions have an independent channel; a burst of previews may
    // page their history entry away without releasing this project's lease.
    stateCursor = data.now || stateCursor;
    jobs = [...new Map([...(data.active_jobs||[]),...data.jobs,...(data.job_updates||[])].map(job=>[job.id,job])).values()]
      .sort((a,b)=>(b.created||0)-(a.created||0));
    refreshProjectLock();
    refreshConnectionReceipt();
    renderProgress();
    if (area === "text_to_speech") await refreshStandaloneSpeechJob();
    if (previewJob) {
      const sample = jobs.find((j) => j.id === previewJob);
      if (sample?.state === "done") await playSample(sample);
      else if (sample?.state === "error") {
        previewJob = null;
        notice(
          "Không tạo được mẫu giọng. Mở tiến trình để xem cách xử lý.",
          true,
        );
      }
    }
    const changed = jobs.find(
      (j) =>
        j.project_id === P?.id &&
        (!j.source_revision || j.source_revision === P?.source_revision) &&
        ["done", "review"].includes(j.state) &&
        previous.has(j.id) &&
        previous.get(j.id) !== j.state,
    );
    if (changed) {
      pendingCompletedJob = changed;
      if(changed.state==='done'&&['pipeline','render'].includes(changed.kind))rememberFinalJob(changed);
    }
    const terminalSetupJob = jobs.find(j =>
      j.project_id === P?.id &&
      (!j.source_revision || j.source_revision === P?.source_revision) &&
      ["pipeline", "render", "auto_subtitles"].includes(j.kind) &&
      ["error", "cancelled"].includes(j.state) &&
      previous.has(j.id) && previous.get(j.id) !== j.state,
    );
    if (terminalSetupJob && area === "setup" && !dirty && !saving &&
        !drag && !cleanSliderHeld && !panelHasFocusedEditor()) {
      await setupController?.refresh?.();
    }
    if (pendingCompletedJob && pendingCompletedJob.project_id !== P?.id) pendingCompletedJob = null;
    if (pendingCompletedJob && area !== "text_to_speech" && !dirty && !saving && !drag && !cleanSliderHeld && !panelHasFocusedEditor()) {
      const finished = pendingCompletedJob, id = P.id, version = editVersion;
      const time = video.currentTime;
      const refreshed = await api("/api/studio/projects/" + id);
      if (P?.id !== id || dirty || saving || drag || cleanSliderHeld || panelHasFocusedEditor() || editVersion !== version) return;
      P = refreshed;
      pendingCompletedJob = null;
      quality = null;
      setMedia(finished.kind === "clean" && area === "clean" ? "clean" : $("#viewSource").value);
      video.addEventListener("loadedmetadata", () => seek(time), {
        once: true,
      });
      renderPanel();
      notice(
        finished.project_applied === false
          ? "Tác vụ đã xong; dự án đã thay đổi trong lúc chạy. Kết quả được lưu riêng, chưa thay phụ đề hoặc giọng/video hiện tại."
          : finished.state === "review"
          ? "Quy trình đang dừng sau nhận dạng; chưa có video thành phẩm. Mở Thiết lập & chạy để tiếp tục các bước còn lại hoặc kiểm tra phụ đề."
          : finished.kind === "clean" ? ($("#viewSource").value === "clean" ? "Đã xử lý xong — khung lớn đang hiển thị video đã xử lý chữ." : "Đã xử lý xong. Mở Chữ gốc → Xem video đã xử lý để xem kết quả.")
          : finished.kind === "preview" ? "Ảnh xử lý đã sẵn sàng trong mục Chữ gốc. Bấm Trước / Sau để so sánh."
          : "Tác vụ đã xong. Kết quả sẵn sàng để xem và nghe.",
      );
    }
    if (
      lastDetail &&
      !$("#progressDetails").hidden &&
      jobs.some((j) => j.id === lastDetail &&
        (["running", "queued"].includes(j.state) || previous.get(j.id) !== j.state))
    )
      await showJob(lastDetail);
  } finally {
    pollBusy = false;
  }
}
async function tick() {
  try {
    await poll();
  } catch (e) {
    window.NPKUpgrade?.connectionError(e);
  }
  pollTimer = setTimeout(
    tick,
    window.NPKUpgrade?.retryDelay() || (document.hidden
      ? 8000
      : jobs.some((j) => j.state === "running" || j.state === "queued")
        ? 1300
        : 7000),
  );
}
$("#viewSource").onchange = (event) => {
  cancelRegionDrag(false);closeRegionMenu();drawMode=false;drawRegions();updateRegionInspector();
  if(!P)return;
  if (event) cleanLive.enabled = false;
  cleanLive.reset();
  stopMix();
  const time = video.currentTime, playing = !video.paused,
    projectId = P.id, loadRevision = ++viewerLoadRevision,
    selectedSource = $("#viewSource").value;
  const finalArtifact=selectedSource==='final'?latestFinalArtifact():null;
  const v=selectedSource==='final'&&!finalArtifact?'source':selectedSource;
  if(v!==selectedSource)$("#viewSource").value=v;
  if (event || v === "final") captionPreviewEnabled = false;
  let src = "/media/source/" + P.id;
  if (v !== "source") {
    const a =
      v === "clean"
        ? P.artifacts.clean
        : finalArtifact;
    src =
      "/media/job/" +
      a.job_id +
      "/" +
      (v === "clean" ? "clean.mp4" : "final.mp4");
  }
  video.src = v === 'source' ? sourceMediaUrl() : mediaUrl(src);
  video.addEventListener("loadedmetadata", () => {
    if (P?.id !== projectId || loadRevision !== viewerLoadRevision) return;
    seek(time);
    if (playing) video.play().catch(() => {});
  }, { once: true });
  drawCaption();
};
document.addEventListener("keydown", (e) => {
  if (handleRegionKey(e)) { e.preventDefault(); return; }
  if (e.key === "Escape" && drawMode && !$("#modal").open) {
    e.preventDefault();
    cancelRegionDrag();
    drawMode = false;
    drawRegions();
    notice("Đã hủy vẽ vùng; các vùng đã lưu không thay đổi.");
    return;
  }
  const editing =
    /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) ||
    e.target.isContentEditable;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
    e.preventDefault();
    guard(save)();
    return;
  }
  if (editing || $("#modal").open) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z") {
    e.preventDefault();
    $("#undo").click();
  } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "y") {
    e.preventDefault();
    $("#redo").click();
  } else if (e.code === "Space") {
    e.preventDefault();
    $("#play").click();
  } else if (e.key === "ArrowLeft") {
    e.preventDefault();
    $("#prev").click();
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    $("#next").click();
  }
});
window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
async function boot() {
  icons();
  renderPanel();
  try {
    await window.NPKUpgrade?.hydrate();
    const result = await Promise.allSettled([poll(), api("/api/catalog")]);
    if (result[1].status === 'fulfilled') catalog = result[1].value;
    for (const item of result) if (item.status === 'rejected') window.NPKUpgrade?.connectionError(item.reason);
    const stored = localStorage.getItem("npk-current");
    const first = projects.find((p) => p.id === stored && !p.archived) || projects.find((p) => !p.archived);
    if (first) await loadProject(first.id);
    if (["setup", "voice", "text_to_speech"].includes(area)) renderPanel();
  } finally { tick(); }
}
guard(boot)();
