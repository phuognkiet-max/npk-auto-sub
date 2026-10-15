/* Transient viewer effects. Never mutate projects, enqueue jobs, or write files. */
"use strict";
class NpkCleanLive {
  constructor({ video, canvas, frame, bar, status, button, snapshot, token }) {
    Object.assign(this, { video, canvas, frame, bar, status, button, snapshot, token });
    this.enabled = true;
    this.ctx = canvas.getContext("2d", { willReadFrequently: true });
    this.patch = document.createElement("canvas");
    this.patchCtx = this.patch.getContext("2d", { willReadFrequently: true });
    this.key = null;
    this.serial = 0;
    this.busy = false;
    this.timer = null;
    this.raf = null;
    this.controller = null;
    this.activeRequestId = null;
    this.previewEdge = 1280;
    this.quickAverageMs = 0;
    this.fastPaints = 0;
    button.onclick = () => { this.enabled = !this.enabled; this.refresh(); };
    for (const name of ["loadeddata", "seeked", "seeking", "pause", "play", "emptied", "error"])
      video.addEventListener(name, () => this.refresh());
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.reset(); else this.refresh(); });
  }
  refresh() {
    if (!this.raf) this.raf = requestAnimationFrame(() => { this.raf = null; this.paint(); });
  }
  reset() {
    this.cancelHeavy();
    this.key = null;
    this.serial++;
    clearTimeout(this.timer);
    clearTimeout(this.playTimer);
    this.pending = null;
    this.canvas.hidden = this.frame.hidden = true;
    if (this.url) { URL.revokeObjectURL(this.url); this.url = null; }
  }
  cancelHeavy() {
    if (!this.controller) return;
    const id = this.activeRequestId;
    this.controller.abort();
    this.controller = null;
    this.activeRequestId = null;
    if (id) fetch('/api/studio/live-clean/cancel', {
      method:'POST', headers:{'x-studio-token':this.token,'Content-Type':'application/json'},
      body:JSON.stringify({request_id:id}), keepalive:true,
    }).catch(() => {});
  }
  message(text, kind = "quick") {
    this.status.textContent = text;
    this.status.title = text;
    this.bar.dataset.state = kind;
  }
  paint() {
    if (document.hidden) { this.reset(); return; }
    const s = this.snapshot();
    this.bar.hidden = !s;
    if (!s) { this.reset(); return; }
    this.button.textContent = this.enabled ? "Xem nguồn" : "Bật xem trực tiếp";
    this.button.setAttribute("aria-pressed", String(this.enabled));
    if (!this.enabled || !s.source) {
      this.reset();
      this.message(!s.source ? "Tệp đã xử lý" : "Video nguồn", "off");
      return;
    }
    if (this.video.readyState < 2 || this.video.seeking) {
      this.reset(); this.message("Đang chuyển khung hình…", "waiting"); return;
    }
    const regions = s.regions.filter(r => r.start <= this.video.currentTime && this.video.currentTime < r.end && r.w >= 1 && r.h >= 1);
    if (!regions.length) {
      this.reset(); this.message("Chưa có vùng tại thời điểm này");
    } else if (["box", "blur", "stretch"].includes(s.params.method)) {
      this.cancelHeavy();
      this.key = null;
      this.pending = null;
      clearTimeout(this.timer);
      this.frame.hidden = true;
      try {
        this.quick(s, regions);
        this.canvas.hidden = false;
        this.message("Xem trực tiếp");
      } catch (e) {
        this.canvas.hidden = true;
        this.message(e.message || "Chưa xem được hiệu ứng. Thử Xem ảnh tại vị trí này.", "error");
      }
    } else {
      this.canvas.hidden = true;
      this.heavy(s, regions);
    }
    // Cap lightweight playback preview at 24 fps. No work when paused/hidden.
    if (!this.video.paused && !this.video.ended && !document.hidden) {
      clearTimeout(this.playTimer);
      this.playTimer = setTimeout(() => this.refresh(), 1000 / 24);
    }
  }
  quick(s, regions) {
    const began = performance.now();
    if (s.params.mask_source !== "manual") throw new Error("Vùng bám chuyển động cần dùng Xem ảnh tại vị trí này để đối chiếu đúng mặt nạ.");
    if (regions.length > 100) throw new Error("Xem trực tiếp hỗ trợ tối đa 100 vùng. Gộp vùng hoặc dùng Xem ảnh tại vị trí này.");
    const largeBlur = s.params.method === "blur" && regions.reduce((n, r) => n + r.w * r.h, 0) > s.width * s.height / 4;
    const displayEdge = Math.max(this.video.clientWidth || 0, this.video.clientHeight || 0) * (window.devicePixelRatio || 1);
    const edge = Math.min(largeBlur ? 960 : 1280, this.previewEdge, displayEdge ? Math.max(480, displayEdge) : 1280);
    const scale = Math.min(1, edge / Math.max(s.width, s.height));
    const width = Math.max(1, Math.round(s.width * scale)), height = Math.max(1, Math.round(s.height * scale));
    if (this.canvas.width !== width || this.canvas.height !== height) {
      this.canvas.width = width; this.canvas.height = height;
    }
    const ctx = this.ctx, p = s.params;
    ctx.drawImage(this.video, 0, 0, width, height);
    const opacity = Number(p.cover_opacity);
    if (!opacity) return;
    for (const r of regions) {
      // Match the export's original-coordinate padding before preview scaling.
      const pad = Math.trunc(p.dilate);
      const ox = Math.max(0, Math.floor(r.x) - pad), oy = Math.max(0, Math.floor(r.y) - pad);
      const ex = Math.min(s.width, Math.ceil(r.x + r.w) + pad), ey = Math.min(s.height, Math.ceil(r.y + r.h) + pad);
      const x = Math.round(ox * scale), y = Math.round(oy * scale);
      const w = Math.round(ex * scale) - x, h = Math.round(ey * scale) - y;
      if (w <= 0 || h <= 0) continue;
      const original = ctx.getImageData(x, y, w, h), pixels = original.data;
      let effect;
      if (p.method === "box") {
        effect = new Uint8ClampedArray(pixels.length);
        const color = /^#[0-9a-f]{6}$/i.test(p.cover_color) ? p.cover_color : "#000000";
        const rgb = [1, 3, 5].map(i => parseInt(color.slice(i, i + 2), 16));
        for (let i = 0; i < effect.length; i += 4) { effect.set(rgb, i); effect[i + 3] = 255; }
      } else if (p.method === "blur") {
        const radius = Math.max(1, Math.round(Math.min(p.blur_radius, (ex - ox - 1) / 2, (ey - oy - 1) / 2) * scale));
        effect = NpkCleanLive.boxBlur(pixels, w, h, radius);
      } else {
        const strip = Math.max(1, Math.min(Math.trunc(p.stretch_height), ey - oy));
        let choices = [oy - Math.trunc(p.stretch_offset) - strip, ey + Math.trunc(p.stretch_offset)];
        if (p.stretch_direction === "below") choices.reverse();
        const source = choices.find(v => v >= 0 && v + strip <= s.height);
        if (source === undefined) throw new Error("Không còn dải nền phù hợp. Giảm khoảng cách/độ dày hoặc đổi phía lấy nền.");
        this.patch.width = w; this.patch.height = h;
        this.patchCtx.drawImage(this.canvas, x, source * scale, w, Math.max(1, strip * scale), 0, 0, w, h);
        effect = this.patchCtx.getImageData(0, 0, w, h).data;
      }
      const feather = p.edge_feather * scale;
      for (let j = 0; j < h; j++) for (let i = 0; i < w; i++) {
        const alpha = opacity * (feather ? Math.min(1, Math.min(i, j, w - i - 1, h - j - 1) / feather) : 1);
        const offset = (j * w + i) * 4;
        for (let c = 0; c < 3; c++) pixels[offset + c] = pixels[offset + c] * (1 - alpha) + effect[offset + c] * alpha;
      }
      ctx.putImageData(original, x, y);
    }
    const elapsed = performance.now() - began;
    this.quickAverageMs = this.quickAverageMs ? this.quickAverageMs * .75 + elapsed * .25 : elapsed;
    if (this.quickAverageMs > 35) {
      this.previewEdge = Math.max(480, Math.round(this.previewEdge * .8));
      this.fastPaints = 0;
    } else if (this.quickAverageMs < 12 && ++this.fastPaints >= 8) {
      this.previewEdge = Math.min(1280, Math.round(this.previewEdge * 1.1));
      this.fastPaints = 0;
    }
  }
  static boxBlur(pixels, w, h, radius) {
    let input = new Uint8ClampedArray(pixels);
    const temp = new Uint8ClampedArray(pixels.length), size = radius * 2 + 1;
    // Three separable box passes, O(pixels) rather than O(pixels * radius^2).
    for (let pass = 0; pass < 3; pass++) {
      for (let y = 0; y < h; y++) for (let c = 0; c < 3; c++) {
        let sum = 0;
        for (let k = -radius; k <= radius; k++) sum += input[(y * w + Math.max(0, Math.min(w - 1, k))) * 4 + c];
        for (let x = 0; x < w; x++) {
          temp[(y * w + x) * 4 + c] = sum / size;
          sum += input[(y * w + Math.min(w - 1, x + radius + 1)) * 4 + c] - input[(y * w + Math.max(0, x - radius)) * 4 + c];
        }
      }
      for (let x = 0; x < w; x++) for (let c = 0; c < 3; c++) {
        let sum = 0;
        for (let k = -radius; k <= radius; k++) sum += temp[(Math.max(0, Math.min(h - 1, k)) * w + x) * 4 + c];
        for (let y = 0; y < h; y++) {
          input[(y * w + x) * 4 + c] = sum / size;
          sum += temp[(Math.min(h - 1, y + radius + 1) * w + x) * 4 + c] - temp[(Math.max(0, y - radius) * w + x) * 4 + c];
        }
      }
    }
    return input;
  }
  heavy(s, regions) {
    if (!this.video.paused || s.interacting) {
      this.reset(); this.message(!this.video.paused ? "Tạm dừng để tự xem thử một khung phục hồi" : "Thả chuột/dừng chỉnh để tự cập nhật khung phục hồi", "waiting"); return;
    }
    if (!Number(s.params.cover_opacity)) { this.reset(); this.message("Độ đậm 0% · đang thấy nguyên video nguồn"); return; }
    const body = { source_revision: s.revision, time: this.video.currentTime, params: s.params, regions };
    const key = JSON.stringify([s.id, body]);
    if (this.key === key) return;
    this.cancelHeavy();
    this.key = key;
    this.serial++;
    this.frame.hidden = true;
    clearTimeout(this.timer);
    this.pending = { key, id: s.id, body, serial: this.serial, readyAt: performance.now() + 650 };
    this.message("Đang chờ bạn dừng chỉnh để cập nhật một khung…", "waiting");
    this.timer = setTimeout(() => this.request(), 650);
  }
  async request() {
    const work = this.pending;
    if (this.busy || !work || work.key !== this.key) return;
    if (performance.now() < work.readyAt) {
      this.timer = setTimeout(() => this.request(), work.readyAt - performance.now()); return;
    }
    this.pending = null;
    this.busy = true;
    this.message("Đang phục hồi một khung… · không chạy cả video", "working");
    const controller = new AbortController(), requestId = crypto.randomUUID(), timeout = setTimeout(() => controller.abort(), 65000);
    this.controller = controller;
    this.activeRequestId = requestId;
    try {
      const response = await fetch('/api/studio/projects/' + encodeURIComponent(work.id) + '/live-clean-frame', {
        method: 'POST', headers: { 'x-studio-token': this.token, 'Content-Type': 'application/json' },
        body: JSON.stringify({...work.body, request_id:requestId}), signal: controller.signal,
      });
      if (!response.ok) {
        if (response.status === 429 && this.key === work.key && this.serial === work.serial &&
            performance.now() - (work.firstRequestedAt ||= performance.now()) < 5000) {
          work.readyAt = performance.now() + 350;
          this.pending = work;
          return;
        }
        const detail = await response.json(); throw new Error(detail.detail || "Chưa tạo được khung phục hồi.");
      }
      const blob = await response.blob();
      if (this.key !== work.key || this.serial !== work.serial) return;
      const url = URL.createObjectURL(blob), img = new Image();
      try { img.src = url; await img.decode(); } catch { URL.revokeObjectURL(url); throw new Error("Không đọc được ảnh xem thử."); }
      if (this.key !== work.key || this.serial !== work.serial) { URL.revokeObjectURL(url); return; }
      if (this.url) URL.revokeObjectURL(this.url);
      this.url = url;
      this.frame.src = url;
      this.frame.hidden = false;
      this.message("Khung phục hồi đã cập nhật · kiểm tra mẫu 3 giây trước khi xuất", "ready");
    } catch (e) {
      if (this.key === work.key && this.serial === work.serial) {
        this.frame.hidden = true;
        this.message(e.name === "AbortError" ? "Khung phục hồi chưa xong. Dùng Xem ảnh tại vị trí này để thử riêng." : e.message, "error");
      }
    } finally {
      clearTimeout(timeout);
      if (this.controller === controller) { this.controller = null; this.activeRequestId = null; }
      this.busy = false;
      if (this.pending && this.pending.key === this.key) this.request();
    }
  }
}
