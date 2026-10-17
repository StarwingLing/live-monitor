// bili 监控站通用件：Toast / 数字与身份徽标渲染助手。
// ECharts hook 在 ./chart.ts（避免 echarts 进全站首屏 chunk）。

// ---------------------------------------------------------------------------
// Toast（同旧版语义）
// ---------------------------------------------------------------------------
let toastTimer: number | undefined;
let toastEl: HTMLDivElement | null = null;
export function toast(msg: string) {
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.id = "toast";
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = msg;
  toastEl.style.display = "block";
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    if (toastEl) toastEl.style.display = "none";
  }, 2200);
}

// ---------------------------------------------------------------------------
// 格式化与渲染助手（自旧 base/room 模板原样移植）
// ---------------------------------------------------------------------------
export function fmtNum(n?: number | null): string {
  if (n == null) return "—";
  if (n >= 10000) return (n / 10000).toFixed(1) + "万";
  return String(n);
}

// 头像兜底色（按房间号取渐变）
export function avatarGradient(id: number): string {
  const cs = [["#7c5cff", "#4f8cff"], ["#ff7ca7", "#ff9d6c"], ["#22c55e", "#0ea5e9"],
              ["#f59e0b", "#ef4444"], ["#06b6d4", "#7c5cff"], ["#ec4899", "#8b5cf6"]];
  const [a, b] = cs[id % cs.length];
  return `linear-gradient(135deg,${a},${b})`;
}
export const avatarStyle = (id: number) => `background:${avatarGradient(id)}`;

// 环比文案（"↑12.5% 较昨日"）
export function cmpLabel(today: number, yesterday: number): React.ReactNode {
  if (yesterday === 0)
    return today > 0 ? <span className="up">昨日为 0</span> : <span>—</span>;
  const pct = ((today - yesterday) / yesterday) * 100;
  const cls = pct >= 0 ? "up" : "down";
  const arrow = pct >= 0 ? "↑" : "↓";
  return (
    <span className={cls}>
      {arrow}{Math.abs(pct).toFixed(1)}% 较昨日
    </span>
  );
}

function esc(s: string): string {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
}

// 十六进制颜色加深（B站粉丝牌等级段比牌名段深一档）
function shade(hex: string, k: number): string {
  const n = parseInt(hex.slice(1), 16);
  const f = (v: number) => Math.round(v * k).toString(16).padStart(2, "0");
  return "#" + f((n >> 16) & 255) + f((n >> 8) & 255) + f(n & 255);
}

// B站风格粉丝牌胶囊 [牌名 | 等级]（外形胶囊化在 monitor.css .medal）
export function medalHtml(name: string, level: number, color: number): React.ReactNode {
  if (!name) return null;
  let c1: string;
  if (color) {
    c1 = "#" + (color & 0xffffff).toString(16).padStart(6, "0");
  } else {
    const palettes = ["#f973b9", "#8b7cff", "#ff9d5c", "#4fc3f7", "#66d9a0", "#ffd166"];
    let h = 0;
    for (const ch of name) h = (h * 31 + ch.codePointAt(0)!) >>> 0;
    c1 = palettes[h % palettes.length];
  }
  const lvColor = level >= 50 ? "#f5a623" : level >= 40 ? "#8b5cf6"
    : level >= 30 ? "#1e5fd6" : level >= 20 ? "#4a9eff" : null;
  const c = lvColor || c1;
  return (
    <span className="medal">
      <span className="m-name" style={{ background: c }}>{name}</span>
      <span className="m-lv" style={{ background: shade(c, .78) }}>{level}</span>
    </span>
  );
}

// 舰队徽章（总督红/提督紫粉/舰长蓝；B站式胶囊 + 上下渐变）
export function guardBadge(level: number): React.ReactNode {
  const G: Record<number, [string, string]> = {
    1: ["总督", "#ff4d4f"], 2: ["提督", "#c46bff"], 3: ["舰长", "#4d8bff"],
  };
  const g = G[level];
  if (!g) return null;
  return (
    <span className="medal guard">
      <span className="m-name" style={{
        background: `linear-gradient(180deg, ${g[1]}, ${shade(g[1], .78)})`,
      }}>{g[0]}</span>
    </span>
  );
}

// SC 档位背景色（B站默认 SC 配色口径）
export function scColor(price: number): string {
  if (price >= 1000) return "rgba(255,120,120,.14)";
  if (price >= 500) return "rgba(255,180,110,.13)";
  if (price >= 100) return "rgba(255,215,120,.12)";
  if (price >= 50) return "rgba(190,160,255,.12)";
  if (price >= 30) return "rgba(120,180,255,.12)";
  return "rgba(130,230,165,.12)";
}

// 弹幕文本渲染（emots 占位替换为表情图）
// 修复（2026-10-09）：旧实现按 emots 键逐个 indexOf 替换，同名表情每条
// 弹幕只换第一处（"[哇][哇]" 第二个起保留原文字）；改为正则全局切分，
// 每一处占位都替换（长键优先，避免键互为子串时误切）。
export function dmContentHtml(x: any): React.ReactNode {
  let emots: Record<string, { url: string }> = {};
  try {
    emots = x.emots ? JSON.parse(x.emots) : {};
  } catch { /* 旧数据无此字段 */ }
  const text = String(x.text || "");
  const keys = Object.keys(emots).filter(Boolean)
    .sort((a, b) => b.length - a.length);
  if (keys.length === 0) return text;
  const rx = new RegExp(
    keys.map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"), "g");
  const parts: React.ReactNode[] = [];
  let rest = 0, i = 0, m: RegExpExecArray | null;
  while ((m = rx.exec(text)) !== null) {
    if (m.index > rest) parts.push(text.slice(rest, m.index));
    parts.push(
      <img className="dm-emot" key={i++} src={emots[m[0]].url} alt={m[0]}
           loading="lazy" onError={(e) => (e.target as HTMLImageElement).remove()} />);
    rest = m.index + m[0].length;
  }
  parts.push(text.slice(rest));
  return parts;
}

export { esc };
