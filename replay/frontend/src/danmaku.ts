// 视频外挂弹幕渲染层（自 bili-monitor v0.10 原生 JS 移植为 TS，行为零变化）。
// <canvas> 覆盖原生 <video>（pointer-events:none），rAF 以 video.currentTime
// 为唯一时钟——拖动/暂停/倍速天然正确；数据 setItems() 注入。
type Item = { t: number; text: string; guard: number };

const BDM_PREF = {
  get on() { return localStorage.getItem("bdm:on") !== "0"; },
  set on(v: boolean) { localStorage.setItem("bdm:on", v ? "1" : "0"); },
  get opacity() { return parseFloat(localStorage.getItem("bdm:opacity") || "0.9"); },
  set opacity(v: number) { localStorage.setItem("bdm:opacity", String(v)); },
};

const BDM_GUARD_COLOR: Record<number, string> = {
  1: "#ffb03c", 2: "#ff7aa5", 3: "#61a8ff",
};

function _isPureEmot(text: string): boolean {
  return !text.replace(/\[[^\[\]\s]{1,32}\]/g, "").trim();
}

export class DanmakuLayer {
  private video: HTMLVideoElement;
  private canvas: HTMLCanvasElement;
  private ctx: CanvasRenderingContext2D;
  private wrap: HTMLDivElement;
  private _ro: ResizeObserver;
  private _raf = 0;
  private items: Item[] = [];
  approx = false;
  private pointer = 0;
  private running: { item: Item; lane: number; w: number; t: number }[] = [];
  private lanes: ({ item: Item; lane: number; w: number; t: number } | null)[] = [];
  private lastCt = -1;
  private destroyed = false;
  private enabled: boolean;
  private opacity: number;
  private density = 1;   // 固定中档密度（v2.0.0 精简控制条后无 UI 可调）
  private fontSize = 15;
  private laneH = 22;
  private laneCount = 1;
  private _approxEl: HTMLElement | null = null;

  constructor(video: HTMLVideoElement) {
    this.video = video;
    this.enabled = BDM_PREF.on;
    this.opacity = Math.min(1, Math.max(0.2, BDM_PREF.opacity));

    const wrap = document.createElement("div");
    wrap.className = "bdm-wrap";
    video.parentNode!.insertBefore(wrap, video);
    wrap.appendChild(video);
    this.wrap = wrap;
    this.canvas = document.createElement("canvas");
    this.canvas.className = "bdm-canvas";
    this.ctx = this.canvas.getContext("2d")!;
    wrap.appendChild(this.canvas);
    wrap.appendChild(this._buildBar());

    this._ro = new ResizeObserver(() => this._resize());
    this._ro.observe(wrap);
    this._resize();
    this._raf = requestAnimationFrame(() => this._frame());
  }

  setItems(items: Item[] | undefined, approx = false) {
    this.items = (items || []).filter(
      (it) => it && it.text && !_isPureEmot(it.text));
    this.approx = approx;
    this._resync(true);
    if (this._approxEl)
      this._approxEl.style.display = approx ? "inline-block" : "none";
  }

  // 控制条（v2.0.0 精简）：仅保留「弹幕开关」与「不透明度」；
  // 「时间轴近似」是旧录像的状态徽标（非设置），保留展示。
  private _buildBar(): HTMLElement {
    const bar = document.createElement("div");
    bar.className = "bdm-bar";
    const btnToggle = document.createElement("button");
    btnToggle.className = "bdm-btn bdm-toggle" + (this.enabled ? " on" : "");
    btnToggle.textContent = this.enabled ? "弹幕开" : "弹幕关";
    btnToggle.onclick = () => {
      this.enabled = !this.enabled;
      BDM_PREF.on = this.enabled;
      btnToggle.textContent = this.enabled ? "弹幕开" : "弹幕关";
      btnToggle.classList.toggle("on", this.enabled);
      if (this.enabled) this._resync();
      else {
        this.running = [];
        this._paint(true);
      }
    };
    const rng = document.createElement("input");
    rng.type = "range";
    rng.className = "bdm-range";
    rng.min = "0.3";
    rng.max = "1";
    rng.step = "0.1";
    rng.value = String(this.opacity);
    rng.title = "弹幕不透明度";
    rng.oninput = () => {
      this.opacity = parseFloat(rng.value);
      BDM_PREF.opacity = this.opacity;
    };
    const approxEl = document.createElement("span");
    approxEl.className = "bdm-approx";
    approxEl.title =
      "该成片录制于时间轴功能上线前，弹幕位置为近似值，断流过的录像可能错位";
    approxEl.textContent = "时间轴近似";
    this._approxEl = approxEl;
    bar.append(btnToggle, rng, approxEl);
    return bar;
  }

  private _resize() {
    const w = this.wrap.clientWidth;
    const h = this.video.clientHeight || 200;
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.max(1, Math.round(w * dpr));
    this.canvas.height = Math.max(1, Math.round(h * dpr));
    this.canvas.style.width = w + "px";
    this.canvas.style.height = h + "px";
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.fontSize = Math.max(12, Math.min(24, Math.round((h / 360) * 15)));
    this.laneH = Math.round(this.fontSize * 1.45);
    this.laneCount = Math.max(1, Math.floor((h * 0.82) / this.laneH));
    this.lanes = new Array(this.laneCount).fill(null);
    this._paint(true);
  }

  private _frame() {
    if (this.destroyed) return;
    this._raf = requestAnimationFrame(() => this._frame());
    if (!this.items.length || !this.enabled) return;
    const ct = this.video.currentTime || 0;
    if (this.lastCt >= 0 && (ct < this.lastCt - 0.05 || ct - this.lastCt > 1.2)) {
      this._resync();
    }
    this.lastCt = ct;
    this._spawn(ct);
    this._paint();
  }

  private _resync(clearOnly = false) {
    const key = (it: Item) => it.t;
    const ct = clearOnly ? 0 : this.video.currentTime || 0;
    if (clearOnly) this.running = [];
    let lo = 0,
      hi = this.items.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (key(this.items[mid]) <= ct) lo = mid + 1;
      else hi = mid;
    }
    this.pointer = lo;
    this.running = this.running.filter((r) => key(r.item) + 8 > ct);
  }

  private _spawn(ct: number) {
    const speedBase = Math.max(60, this.canvas.clientWidth * 0.9);
    const usable = Math.max(
      1,
      Math.round(this.laneCount * [0.4, 0.7, 1][Math.min(2, Math.max(0, this.density))]),
    );
    while (this.pointer < this.items.length) {
      const it = this.items[this.pointer];
      const et = it.t;
      if (et > ct) break;
      this.pointer++;
      if (et < ct - 8) continue;
      if (this.running.length >= usable * 2) continue;
      this.ctx.font = `600 ${this.fontSize}px "PingFang SC","Microsoft YaHei",sans-serif`;
      const w = Math.min(
        this.canvas.clientWidth * 1.5,
        this.ctx.measureText(it.text).width,
      );
      let lane = -1;
      for (let i = 0; i < this.laneCount; i++) {
        if (i >= usable && lane >= 0) break;
        const prev = this.lanes[i];
        const free =
          !prev ||
          ((ct - prev.t) * speedBase) / 8 > prev.w + w * 0.6 + 24;
        if (free && (lane < 0 || i < usable)) {
          lane = i;
          if (i < usable) break;
        }
      }
      if (lane < 0) continue;
      const rec = { item: it, lane, w, t: et };
      this.lanes[lane] = rec;
      this.running.push(rec);
    }
  }

  private _paint(force = false) {
    const ctx = this.ctx;
    const W = this.canvas.clientWidth;
    const H = this.canvas.clientHeight;
    if (!W || !H) return;
    const ct = this.video.currentTime || 0;
    if (force || this.enabled) ctx.clearRect(0, 0, W, H);
    if (!this.running.length) return;
    ctx.globalAlpha = this.opacity;
    ctx.font = `600 ${this.fontSize}px "PingFang SC","Microsoft YaHei",sans-serif`;
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,.75)";
    const keep: typeof this.running = [];
    for (const r of this.running) {
      const age = ct - r.t;
      if (age < 0 || age > 8) continue;
      const x = W - (age / 8) * (W + r.w);
      if (x + r.w < 0) continue;
      const y = r.lane * this.laneH + this.laneH / 2 + 2;
      ctx.strokeText(r.item.text, x, y);
      ctx.fillStyle = BDM_GUARD_COLOR[r.item.guard] || "#ffffff";
      ctx.fillText(r.item.text, x, y);
      keep.push(r);
    }
    this.running = keep;
    ctx.globalAlpha = 1;
  }

  destroy() {
    this.destroyed = true;
    cancelAnimationFrame(this._raf);
    this._ro.disconnect();
    const wrap = this.wrap;
    const video = this.video;
    if (video.parentNode === wrap) {
      if (wrap.parentNode) wrap.parentNode.insertBefore(video, wrap);
      wrap.remove();
    }
  }
}

export function attachDanmaku(video: HTMLVideoElement): DanmakuLayer {
  return new DanmakuLayer(video);
}
