// 通用 UI 件与格式化工具（Toast / Switch / Pop 气泡锚定 / 时间体积格式化）。
// Toast 与旧版同款语义：类型化轻提示（ok 绿 / busy 橙 / err 红），后到覆盖先到。
import { createContext, useContext, useEffect, useRef, useState,
         type ReactNode } from "react";

// ---------------------------------------------------------------------------
// 格式化（自旧 replay.html 原样移植，保证展示口径零变化）
// ---------------------------------------------------------------------------
export function fmtMB(b?: number | null): string {
  if (!b) return "—";
  return b >= 1e9 ? (b / 1e9).toFixed(2) + " GB" : (b / 1e6).toFixed(1) + " MB";
}

export function fmtDur(s?: number | null): string {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" +
         String(sec).padStart(2, "0");
}

export function fmtShort(s?: number | null): string {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return (h ? h + ":" + String(m).padStart(2, "0") : "" + m) + ":" +
         String(x).padStart(2, "0");
}

export function parseT(v: string): number | null {
  v = String(v || "").trim();
  if (/^\d+$/.test(v)) return +v;
  const p = v.split(":").map((x) => x.trim());
  if (p.some((x) => x === "" || isNaN(+x))) return null;
  return p.length === 3 ? +p[0] * 3600 + +p[1] * 60 + +p[2]
       : p.length === 2 ? +p[0] * 60 + +p[1] : null;
}

export const wallHM = (iso: string) => (iso || "").slice(11, 16);
export const wallHMS = (iso: string) => (iso || "").slice(11, 19);
export const fmtTime = (iso: string) =>
  iso ? iso.replace("T", " ").slice(5, 16) : "";

export const WEEKDAYS = "日一二三四五六";
export function afDateLabel(ds: string): { main: string; sub: string } {
  const [y, m, d] = ds.split("-").map(Number);
  if (!y) return { main: "", sub: "" };
  const day = new Date(y, m - 1, d);
  const t0 = new Date();
  t0.setHours(0, 0, 0, 0);
  const diff = Math.round((t0.getTime() - day.getTime()) / 86400000);
  const rel = diff === 0 ? " · 今天" : diff === 1 ? " · 昨天" : "";
  return { main: `${m}月${d}日`, sub: `周${WEEKDAYS[day.getDay()]}${rel}` };
}

export const REDUCED = () =>
  typeof matchMedia !== "undefined" &&
  matchMedia("(prefers-reduced-motion: reduce)").matches;

// ---------------------------------------------------------------------------
// Toast（模块级单例：命令式调用 toast("...", "ok")，任何组件可用）
// ---------------------------------------------------------------------------
let toastTimer: number | undefined;
let toastEl: HTMLDivElement | null = null;
export function toast(msg: string, type?: "" | "ok" | "busy" | "err") {
  if (!toastEl) {
    toastEl = document.createElement("div");
    toastEl.id = "toast";
    document.body.appendChild(toastEl);
  }
  toastEl.textContent = msg;
  toastEl.className = type || "";
  toastEl.style.display = "block";
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => {
    if (toastEl) toastEl.style.display = "none";
  }, 2600);
}

// ---------------------------------------------------------------------------
// 删除收起补位动画（WAAPI，同旧版：后台标签页冻结 CSS transition 的场景
// 也能推进；reduced-motion 直接隐藏）
// ---------------------------------------------------------------------------
export function collapseEl(el: Element | null, ms = 300): Promise<void> {
  return new Promise((res) => {
    if (!el) {
      res();
      return;
    }
    if (REDUCED()) {
      (el as HTMLElement).style.display = "none";
      res();
      return;
    }
    const he = el as HTMLElement;
    const cs = getComputedStyle(he);
    try {
      const anim = he.animate(
        [
          {
            height: he.offsetHeight + "px",
            opacity: 1,
            marginTop: cs.marginTop,
            marginBottom: cs.marginBottom,
            paddingTop: cs.paddingTop,
            paddingBottom: cs.paddingBottom,
            borderWidth: cs.borderWidth,
          },
          {
            height: "0px",
            opacity: 0,
            marginTop: "0px",
            marginBottom: "0px",
            paddingTop: "0px",
            paddingBottom: "0px",
            borderWidth: "0px",
          },
        ],
        { duration: ms, easing: "ease", fill: "forwards" },
      );
      anim.onfinish = () => res();
      anim.oncancel = () => res();
    } catch {
      he.style.display = "none";
    }
    window.setTimeout(res, ms + 80);
  });
}

// ---------------------------------------------------------------------------
// Switch（B站风格滑动开关：管理页自动全程录制 / 高能开关同款）
// ---------------------------------------------------------------------------
export function Switch(props: {
  on: boolean;
  onClick?: (e: React.MouseEvent) => void;
  title?: string;
}) {
  return (
    <span
      className={"he-switch" + (props.on ? " on" : "")}
      title={props.title}
      onClick={props.onClick}
    />
  );
}

// ---------------------------------------------------------------------------
// Pop：fixed 定位气泡（锚定在触发按钮下方，视口夹边；点击外部关闭）。
// 旧 renamePop/dlPop/投稿气泡共用同款定位逻辑。
// ---------------------------------------------------------------------------
export function Pop(props: {
  anchor: HTMLElement | null;
  onClose: () => void;
  width?: number;
  children: ReactNode;
  /** 点击气泡外部是否关闭。v2.2.4：回灌气泡拉取中/完成态传 false，
   * 防止拉取过程中误点别处把进度气泡关掉（依赖完成后的自动关闭） */
  closeOnOutside?: boolean;
  /** 动态解析锚点：组卡重渲染会重建按钮节点、静态 anchor 会失联，
   * 提供解析函数时每次重定位都重新查找当前按钮（v2.2.4 回灌气泡用） */
  anchorResolve?: () => HTMLElement | null;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ left: number; top: number }>({
    left: -9999,
    top: -9999,
  });
  useEffect(() => {
    let lastKey = "";
    const place = () => {
      const a = props.anchorResolve ? props.anchorResolve() : props.anchor;
      // 锚点暂不可用（已摘除/被替换）时保持原位，避免 rect 全零甩到左上角
      if (!a || !ref.current || !a.isConnected) return;
      const r = a.getBoundingClientRect();
      const w = props.width ?? 300;
      const h = ref.current.offsetHeight || 160;
      const left = Math.max(8, Math.min(window.innerWidth - w - 16, r.left));
      const top = Math.min(window.innerHeight - h - 16, r.bottom + 8);
      const key = left + "|" + top;
      if (key !== lastKey) {
        // 坐标未变不触发渲染（轻量轮询零开销空转）
        lastKey = key;
        setPos({ left, top });
      }
    };
    place();
    // 跟随锚点：滚动（capture 捕捉内部滚动容器）/缩放即时重定位 +
    // 400ms 轻量轮询（覆盖归档 2.5s 刷新引起的组卡重排/锚点重建）
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    const iv = window.setInterval(place, 400);
    // 外部点击关闭挂在 **capture 相**：React 18 对真实点击会在事件处理器
    // 返回后同步提交 DOM（如回灌气泡点「拉取原件」→ phase 切换 → 该按钮
    // 立即被摘除），bubble 相到达 document 时 contains(已摘除的按钮)=false
    // 会被误判为「点了外部」瞬间关掉气泡；capture 相先于目标阶段与 React
    // 提交执行，判断时目标仍在气泡内，结果正确。
    let onDoc: ((ev: MouseEvent) => void) | null = null;
    const t = window.setTimeout(() => {
      onDoc = (ev: MouseEvent) => {
        if (props.closeOnOutside === false) return;
        if (ref.current && !ref.current.contains(ev.target as Node)) {
          props.onClose();
        }
      };
      document.addEventListener("click", onDoc, true);
      place();
    }, 0);
    return () => {
      window.clearTimeout(t);
      window.clearInterval(iv);
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
      if (onDoc) document.removeEventListener("click", onDoc, true);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.anchor, props.closeOnOutside]);
  return (
    <div
      ref={ref}
      className="af-pop"
      style={{
        left: pos.left + "px",
        top: pos.top + "px",
        width: (props.width ?? 300) + "px",
      }}
    >
      {props.children}
    </div>
  );
}

// 会话上下文（Me 拉取一次，全树可用；IS_ADMIN/CAN_WRITE 派生同旧版）
export const MeCtx = createContext<{
  me: Me0 | null;
  refresh: () => Promise<void>;
}>({ me: null, refresh: async () => {} });
export type Me0 = {
  role: "admin" | "clipper" | "guest" | null;
  username: string | null;
  me: string | null;
  room_id?: number;
  nickname?: string;
};
export function useMe() {
  return useContext(MeCtx);
}
