// 回放页（核心视图）：日期归档 + B站选集播放器 + 打点切片抽屉 + 直播截取
// 大按钮状态机 + 注释/重命名 + v2.0.0 下载气泡（ASS 旁挂）与投稿气泡 +
// v2.0.1 原片回灌气泡（本地已释放 → 确认从云端拉原件 + 进度条 + 完成
// 轻提示，切片动线配套）与删除双选项气泡（删除视频=界面移除 / 删除
// 服务器存档=仅已上云可用，云端归档恒不删）。
// v2.2.2：full 组按日合并——同日多场直播/轮换多段不再每行一张组卡，
// 全部录制段在单板块内按时间置顶（桥接衔接片带徽标、落在相邻两段
// 之间，且走 rec 形态携带 oss 键可正常云端播放）。
//
// 行为对齐口径（与旧 replay.html 逐项一致）：
// - 2.5s 轮询 /auto-recordings；结构变化由 React 按稳定 key 协调（key 不变
//   的 <video> 不重挂载 → 不闪黑；进度按 selKey 存取）；
// - 播放候选链：本机 low.mp4 → 本机原片 →（已释放）OSS low → OSS 本体；
// - 删除收起动画（WAAPI）+ 动画期间抑制重建（_collapsing 同款）；
// - 大按钮六态；diffToast 状态转换轻提示；
// - 三角色按钮级门控 ME / IS_ADMIN / CAN_WRITE；
// - 移动端三断点（≤1020 组卡堆叠 / ≤1000 侧栏折叠 / ≤560 抽屉全屏）。
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router";
import {
  api, postJson, patchJson, getOssUrl, getOssDownloadUrl, triggerDownload, logout, BASE,
  fetchMe, type Me,
} from "../api";
import {
  toast, collapseEl, Switch, Pop, fmtMB, fmtDur, fmtShort, parseT,
  wallHM, wallHMS, afDateLabel,
} from "../ui";
import { attachDanmaku, DanmakuLayer } from "../danmaku";

// ---------------------------------------------------------------------------
// 数据形状（/auto-recordings）
// ---------------------------------------------------------------------------
export interface Note {
  id: number; author: string; content: string;
  created_at: string; updated_at: string;
}
export interface ClipRow {
  id: number; etype?: string; recording_id?: number; title: string;
  start_sec: number; end_sec: number; start_wall: string; end_wall: string;
  file_path: string; file_size: number; duration_sec: number;
  status: string; reason: string; created_by: string; created_at: string;
  ended_at?: string; file_url?: string; in_local?: boolean; on_oss: boolean;
  oss_key: string; oss_low_key: string; notes: Note[];
  // v2.3.0 投稿结果（服务器持久真源）：刷新/重启后「已投稿」徽标仍显示；
  // alive=0 = 长周期核验发现稿件/分P已被删（胶囊隐藏）
  submission?: { bv: string; mode: string; title: string;
                 submitted_at: string; season_note: string;
                 alive?: number } | null;
}
export interface RecRow {
  id: number; status: string; title: string; started_at: string;
  ended_at: string | null; qn: number; file_path: string;
  file_size: number; duration_sec: number; source: string;
  oss_key: string; oss_low_key: string; live_size?: number;
  file_url?: string; in_local: boolean; on_oss: boolean;
  session_end?: boolean;   // v2.2.2：该段是其场次最后一段（上方隔线=另一场）
  clips: ClipRow[]; notes: Note[];
}
export interface Group {
  kind: "full" | "standalone" | "orphan";
  // v2.2.2：full 组按日合并——同日全部整场行（auto 段/bridge 桥接/进行
  // 中/长手动）的 recs 列表（新→旧）；非 full 组恒为空数组
  recs: RecRow[];
  rec: RecRow | null;   // 恒 null（保留字段仅为兼容旧引用形状）
  clips: ClipRow[];
}
export interface Archive {
  auto_full: boolean; live: boolean; anchor_name: string;
  groups: { date: string; groups: Group[] }[];
}

const ACTIVE_SET = new Set(["requested", "recording", "stop_requested", "finalizing"]);
const AF_TERMINAL = new Set(["finished", "interrupted", "failed"]);
const SEARCH_CHIPS = ["歌切"];

// 条目统一形态（resolveSel 的产物）
interface Entry {
  etype: "rec" | "clip"; id: number; recId: number; title: string;
  fileUrl: string; fileSize: number; durationSec: number; status: string;
  live: boolean; startSec: number; endSec: number;
  startWall: string; endWall: string; createdBy: string; createdAt: string;
  endedAt: string; reason: string; notes: Note[]; qn: number;
  isFullRec: boolean; disabled: boolean; inLocal: boolean; onOss: boolean;
  ossFullKey: string; ossLowKey: string;
  source: string;   // 录制行来源（auto/bridge/manual；bridge=衔接片命名用）
  submission?: { bv: string; mode: string; title: string;
                 submitted_at: string; season_note: string;
                 alive?: number } | null;
}

function clipEntry(c: ClipRow, recId: number): Entry {
  return {
    etype: c.etype === "rec" ? "rec" : "clip", id: c.id, recId,
    title: c.title || "", fileUrl: c.file_url || "",
    fileSize: c.file_size || 0, durationSec: c.duration_sec || 0,
    status: c.status, live: c.status === "live",
    startSec: c.start_sec, endSec: c.end_sec,
    startWall: c.start_wall || "", endWall: c.end_wall || "",
    createdBy: c.created_by || "", createdAt: c.created_at || "",
    endedAt: c.ended_at || "", reason: c.reason || "",
    notes: c.notes || [], qn: 0, isFullRec: false, disabled: false,
    inLocal: c.in_local !== undefined ? !!c.in_local : !!c.file_url,
    onOss: !!c.on_oss, ossFullKey: c.oss_key || "",
    ossLowKey: c.oss_low_key || "", source: "",
    submission: c.submission || null,
  };
}

function recEntry(r: RecRow): Entry {
  const fullDisabled = r.status === "failed" || (!r.file_url && !r.on_oss);
  return {
    etype: "rec", id: r.id, recId: r.id, title: r.title || "",
    fileUrl: r.file_url || "", fileSize: r.file_size || 0,
    durationSec: r.duration_sec || 0, status: r.status, live: false,
    startSec: 0, endSec: r.duration_sec || 0, startWall: "", endWall: "",
    createdBy: "", createdAt: r.started_at || "",
    endedAt: r.ended_at || "", reason: "", notes: r.notes || [],
    qn: r.qn, isFullRec: true, disabled: fullDisabled,
    inLocal: r.in_local !== false && !!r.file_url,
    onOss: !!r.on_oss, ossFullKey: r.oss_key || "",
    ossLowKey: r.oss_low_key || "", source: r.source || "",
  };
}

const selStrOf = (e: Entry) =>
  e.isFullRec ? "r" + e.id : e.etype === "rec" ? "s" + e.id : "c" + e.id;

// 桥接片（轮换衔接）默认名：衔接 HH:MM – HH:MM（起止墙钟）
function bridgeName(startedAt: string, endedAt: string): string {
  const s = (startedAt || "").slice(11, 16);
  const t = endedAt ? endedAt.slice(11, 16) : "";
  return t ? `衔接 ${s} – ${t}` : `衔接 ${s} 起`;
}

function entryName(e: Entry): string {
  if (e.title) return e.title;
  if (e.isFullRec) {
    return e.source === "bridge"
      ? bridgeName(e.createdAt, e.endedAt)
      : "本场录播 " + (e.createdAt || "").slice(11, 16);
  }
  if (e.startWall)
    return wallHM(e.startWall) + (e.live ? " 起" : " – " + wallHM(e.endWall));
  return e.endSec ? fmtShort(e.startSec) + " – " + fmtShort(e.endSec) : "切片";
}

// 键盘控制的 目标播放器：最近一次起播的视频（VideoStage 的 play 监听登记，
// 组件销毁时清除）。页面无任何在播视频时键盘兜底选 DOM 里第一个视频。
let activeVideo: HTMLVideoElement | null = null;

// ===========================================================================
// 主页面
// ===========================================================================
export default function Replay() {
  const { roomId: roomStr } = useParams();
  const ROOM = Number(roomStr);
  const [me, setMe] = useState<Me | null>(null);
  const [arc, setArc] = useState<Archive | null>(null);
  const [err, setErr] = useState("");
  const ME = me?.me || "";
  const IS_ADMIN = me?.role === "admin";
  const CAN_WRITE = me?.role === "admin" || me?.role === "clipper";

  // 选择 / 进度 / 打点 / 草稿（值可为 null：在录组无切片的默认空态）
  const [selMap, setSelMap] = useState<Record<string, string | null>>({});
  const vtMap = useRef<Record<string, number>>({});
  const [marks, setMarks] = useState<Record<number, { inV: number | null; outV: number | null }>>({});
  const nameTouched = useRef<Record<number, boolean>>({});
  const nameDraft = useRef<Record<number, string>>({});

  // 搜索 / 侧栏 / 抽屉
  const [searchQ, setSearchQ] = useState("");
  const [sideOpen, setSideOpen] = useState(false);
  const [drawerKey, setDrawerKey] = useState<string | null>(null);

  // 气泡：重命名 / 下载（v2.0.0）+ 回灌/删除（v2.0.1）
  const [renamePop, setRenamePop] = useState<{ key: string; anchor: HTMLElement } | null>(null);
  const [dlPop, setDlPop] = useState<{ key: string; anchor: HTMLElement } | null>(null);
  const [restorePop, setRestorePop] = useState<{ key: string; anchor: HTMLElement } | null>(null);
  const [delPop, setDelPop] = useState<{ key: string; anchor: HTMLElement } | null>(null);
  // 投稿抽屉（v2.3.0：气泡改抽屉，放下合集/分P 表单）：key = 组键
  const [subDrawer, setSubDrawer] = useState<string | null>(null);

  // 投稿任务（v2.0.0）：clipId → 任务态；3s 轮询 uploading
  // v2.3.0：mode（new/part）用于成功 toast 文案区分；season_note 为
  // 「投稿成功但加入合集失败」的补充提示
  const [jobs, setJobs] = useState<Record<number, { id: string; status: string; bv: string; error: string; mode?: string; seasonNote?: string }>>({});

  // 播放器懒加载（v2.0.0）：进入页面仅第一个含可播视频的组挂载播放器
  // （其余显示占位，点击该视频/选择列表后再加载），配合「点击即起播 →
  // 单播放器互斥暂停其余」保证任一时刻只有一个播放器在拉流（3Mbps
  // 出向带宽下避免整页 N 个播放器同时探测/拉元数据/拉弹幕）。
  // armLock：初始自动加载的竞态锁——挂载批内多个组同时满足条件时，
  // 先到者胜出且仅一个
  const [armedKeys, setArmedKeys] = useState<Record<string, true>>({});
  const armLock = useRef(false);
  const anyArmed = Object.keys(armedKeys).length > 0;
  const arm = useCallback((k: string, first = false) => {
    if (first) {
      if (armLock.current) return;
      armLock.current = true;
    }
    setArmedKeys((m) => (m[k] ? m : { ...m, [k]: true }));
  }, []);

  const collapsing = useRef(0);
  const prevSt = useRef<{ rec: Record<number, string>; clip: Record<number, string>; name: Record<number, string>; local: Record<number, boolean> } | null>(null);
  // v2.0.1 回灌登记：rec_id -> 触发时刻（毫秒）。in_local 在归档轮询里
  // false→true 翻转且 30 分钟内触发过回灌 → 完成 toast（气泡被提前关闭
  // 也能收到提示；超龄条目视为外部回灌/陈旧，不提示）
  const restoring = useRef<Map<number, number>>(new Map());
  const dmCache = useRef<Record<number, { items: any[]; approx: boolean }>>({});
  const [tick, setTick] = useState(0);

  // ---- 身份 ----
  useEffect(() => {
    fetchMe().then(setMe).catch(() => {});
  }, []);

  // ---- 播放器键盘控制 ----
  // ←/→ 快退/快进 10 秒；同一方向键连续按 3 次后，第 4 次起改为 60 秒
  // （「连续」= 同键且间隔 ≤2s，换方向或其他按键即重新计数）；
  // ↑/↓ 音量 ±5%；空格 播放/暂停。输入框/按钮等交互元素聚焦时不接管。
  // v2.2.4 修复：改挂 **capture 相**并在接管时 stopPropagation——焦点在
  // <video>（原生控件影子 DOM）上时一次物理按键会多路到达/被原生控件
  // 同时响应，造成「左键退 20 秒、空格暂停又播放」的双执行；capture 先于
  // 目标阶段拦截，原生控件不再看到按键，任何焦点状态下都只执行一次。
  useEffect(() => {
    const STEP = 10, BIG_STEP = 60, VOL_STEP = 0.05, STREAK_MS = 2000;
    let streakKey = "", streakN = 0, streakAt = 0;
    const pick = () =>
      activeVideo && activeVideo.isConnected
        ? activeVideo : document.querySelector("video");
    const seek = (v: HTMLVideoElement, delta: number) => {
      const d = isFinite(v.duration) ? v.duration : Number.MAX_SAFE_INTEGER;
      try { v.currentTime = Math.min(Math.max(0, v.currentTime + delta), d); }
      catch { /* 元数据未就绪，忽略本次 */ }
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return;
      const t = e.target;
      if (t instanceof Element &&
          t.closest("input, textarea, select, button, a, [contenteditable]")) return;
      const v = pick();
      if (!v) return; // 页面暂无可控播放器（如空归档日），不拦截按键
      if (e.key === " " || e.code === "Space") {
        e.preventDefault();
        e.stopPropagation();
        if (v.paused) v.play().catch(() => {});
        else v.pause();
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        e.preventDefault();
        e.stopPropagation();
        const now = Date.now();
        if (e.key !== streakKey || now - streakAt > STREAK_MS) {
          streakKey = e.key; streakN = 0;
        }
        streakN++; streakAt = now;
        const step = streakN >= 4 ? BIG_STEP : STEP;
        seek(v, e.key === "ArrowRight" ? step : -step);
        toast((e.key === "ArrowRight" ? "快进 " : "快退 ") + step + " 秒");
        return;
      }
      if (e.key === "ArrowUp" || e.key === "ArrowDown") {
        e.preventDefault();
        e.stopPropagation();
        v.muted = false;
        v.volume = Math.min(1, Math.max(0,
          Math.round((v.volume + (e.key === "ArrowUp" ? VOL_STEP : -VOL_STEP)) * 100) / 100));
        toast("音量 " + Math.round(v.volume * 100) + "%");
        return;
      }
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);

  // ---- 轮询 2.5s ----
  const refresh = useCallback(async () => {
    try {
      const d = await api<Archive>(`/api/rooms/${ROOM}/auto-recordings`);
      setArc(d);
      setErr("");
      diffToast(d);
    } catch (e: any) {
      if (e.status && e.status !== 401) setErr(e.message);
    }
  }, [ROOM]);

  useEffect(() => {
    refresh();
    const t = window.setInterval(refresh, 2500);
    return () => window.clearInterval(t);
  }, [refresh]);

  // 直播中时长跳动（每秒）
  const hasActive = useMemo(
    () => !!arc?.groups.some((d) => d.groups.some(
      (g) => g.kind === "full" &&
        (g.recs || []).some((r) => !AF_TERMINAL.has(r.status)))),
    [arc]);
  useEffect(() => {
    if (!hasActive) return;
    const t = window.setInterval(() => setTick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [hasActive]);

  // 投稿任务轮询（存在 uploading 任务时每 3s）
  const uploading = useMemo(
    () => Object.values(jobs).filter((j) => j.status === "uploading").length > 0,
    [jobs]);
  useEffect(() => {
    if (!uploading) return;
    const t = window.setInterval(async () => {
      const next = { ...jobs };
      let changed = false;
      for (const [cidS, j] of Object.entries(jobs)) {
        if (j.status !== "uploading" && j.status !== "pending") continue;
        try {
          const r = await api<{ status: string; bv: string; error: string;
                                mode?: string; season_note?: string }>(
            `/api/rooms/${ROOM}/clips/${cidS}/submit/${j.id}`);
          if (r.status !== j.status) {
            changed = true;
            next[+cidS] = { ...j, status: r.status, bv: r.bv, error: r.error,
                            mode: r.mode, seasonNote: r.season_note };
            if (r.status === "success" && r.bv)
              toast(r.mode === "part" ? `分P已追加：${r.bv}` : `投稿成功：${r.bv}`, "ok");
            else if (r.status === "failed")
              toast("投稿失败：" + r.error, "err");
            // fail-soft：投稿成功但加入合集失败 → 单独红提示（手动补加）
            if (r.status === "success" && r.season_note)
              toast(r.season_note, "err");
          }
        } catch (e: any) {
          // v2.3.0：404 = 任务已过期（服务重启/2h TTL 清空）——静默重试会
          // 让「投稿中」永远挂着，改为提示并摘除任务（成功结果会经
          // clip_submissions 落库在刷新后仍显示「已投稿」）
          if (e.status === 404) {
            changed = true;
            delete next[+cidS];
            toast("投稿任务已过期（服务重启或超时），请刷新页面查看投稿结果", "err");
          }
        }
      }
      if (changed) setJobs(next);
    }, 3000);
    return () => window.clearInterval(t);
  }, [uploading, jobs, ROOM]);

  // ---- 状态转换轻提示（diffToast 同款）----
  // 回灌完成 toast 的唯一出口：气泡轮询与归档 diff 两路竞争，先到先消费
  // 登记（Map delete），保证只提示一次
  function consumeRestore(recId: number, staleOk = false) {
    const at = restoring.current.get(recId);
    if (at === undefined) return;
    restoring.current.delete(recId);
    if (staleOk || Date.now() - at < 30 * 60 * 1000)
      toast("录播拉取已完成，可切片", "ok");
  }
  function diffToast(d: Archive) {
    const recSt: Record<number, string> = {};
    const clipSt: Record<number, string> = {};
    const clipName: Record<number, string> = {};
    const recLocal: Record<number, boolean> = {};
    for (const day of d.groups)
      for (const g of day.groups) {
        if (g.kind === "full") {
          for (const r of g.recs || []) {
            recSt[r.id] = r.status;
            recLocal[r.id] = !!r.in_local;
            for (const c of r.clips || []) {
              clipSt[c.id] = c.status;
              clipName[c.id] = c.title;
            }
          }
        } else {
          for (const c of g.clips || []) {
            if (c.etype === "rec") {
              recSt[c.id] = c.status === "done" ? "finished"
                : c.status === "failed" ? "failed" : "finalizing";
            } else {
              clipSt[c.id] = c.status;
              clipName[c.id] = c.title;
            }
          }
        }
      }
    const prev = prevSt.current;
    if (prev) {
      for (const id in recSt) {
        const p = prev.rec[id];
        if (p === undefined || p === recSt[id]) continue;
        if (recSt[id] === "finished" &&
            (p === "recording" || p === "stop_requested" || p === "finalizing"))
          toast("录制完成，成片已生成", "ok");
      }
      for (const id in clipSt) {
        const p = prev.clip[id];
        if (p === undefined || p === clipSt[id]) continue;
        if (clipSt[id] === "done" &&
            (p === "pending" || p === "processing" || p === "live"))
          toast("切片完成" + (clipName[id] ? "：" + clipName[id] : ""), "ok");
        else if (clipSt[id] === "failed") toast("切片失败，该片段未生成", "err");
      }
      // v2.0.1 回灌完成：本机原片 false→true 且登记仍在本页（气泡被提前
      // 关闭时由此兜底提示；气泡在位时其 1s 轮询先消费，不会双提示）
      if (prev.local && restoring.current.size)
        for (const id in recLocal) {
          if (recLocal[id] && prev.local[id] === false)
            consumeRestore(+id);
        }
    }
    prevSt.current = { rec: recSt, clip: clipSt, name: clipName, local: recLocal };
  }

  // ---- 组键 / 条目解析 ----
  // v2.2.2：full 组按日合并后同日仅一个，键即日期后缀（轮换新增段不换键，
  // 组卡不重挂载、播放器不闪断）；standalone/orphan 同日至多各一，键含序号
  const grpKey = (date: string, g: Group, i: number) =>
    g.kind === "full"
      ? date + "-full"
      : date + "-" + g.kind + "-g" + i;

  const groupsFlat = useMemo(() => {
    const out: { date: string; key: string; g: Group; i: number }[] = [];
    (arc?.groups || []).forEach((d) =>
      d.groups.forEach((g, i) => out.push({ date: d.date, key: grpKey(d.date, g, i), g, i })));
    return out;
  }, [arc]);

  function clipsOf(g: Group): ClipRow[] {
    return g.kind === "full"
      ? (g.recs || []).flatMap((r) => r.clips || [])
      : g.clips;
  }

  function resolveSel(key: string, g: Group): Entry | null {
    const sel = selMap[key];
    if (g.kind === "full") {
      const recs = g.recs || [];
      // v2.2.2：full 组多段置顶——选中串 "r<id>" 定位录制段、"c<id>" 定位
      // 任一段下挂的切片；选中串陈旧（段/切片已被删除）时落回组默认
      //（在录段 → 直播占位；终态 → 最新段），不误显空态盒
      if (sel && sel[0] === "r") {
        const r = recs.find((x) => "r" + x.id === sel);
        if (r) {
          if (!r.file_url && !AF_TERMINAL.has(r.status)) return null;
          return recEntry(r);
        }
      } else if (sel && sel[0] === "c") {
        for (const r of recs) {
          const c = (r.clips || []).find((x) => "c" + x.id === sel);
          if (c) return clipEntry(c, r.id);
        }
      }
      const act = recs.find((r) => !AF_TERMINAL.has(r.status)) || recs[0];
      if (!act) return null;
      return act.file_url ? recEntry(act) : null;
    }
    const clips = g.clips;
    if (!sel) return clips[0] ? clipEntry(clips[0], clips[0].recording_id || clips[0].id) : null;
    const c = clips.find((x) => "s" + x.id === sel || "c" + x.id === sel);
    return c ? clipEntry(c, c.recording_id || c.id) : null;
  }

  function defaultSel(g: Group): string | null {
    if (g.kind === "full") {
      const recs = g.recs || [];
      const act = recs.find((r) => !AF_TERMINAL.has(r.status));
      if (act) {
        const first = (act.clips || [])[0];
        return first ? selStrOf(clipEntry(first, act.id)) : null;
      }
      return recs[0] ? "r" + recs[0].id : null;
    }
    const first = g.clips[0];
    return first ? selStrOf(clipEntry(first, first.recording_id || first.id)) : null;
  }

  // 保证每个组有选中态（组首见设默认；在录组切片出现后自动重算——
  // undefined 与 null+在录 full 的语义照旧版）
  useEffect(() => {
    if (!arc) return;
    let next: Record<string, string | null> | null = null;
    for (const { key, g } of groupsFlat) {
      const cur = selMap[key];
      const needDefault =
        cur === undefined ||
        (cur === null && g.kind === "full" &&
          (g.recs || []).some((r) => !AF_TERMINAL.has(r.status)));
      if (needDefault) {
        next = next || { ...selMap };
        next[key] = defaultSel(g);
      }
    }
    if (next) setSelMap(next);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [arc]);

  // ---- 大按钮状态机 ----
  const recs = useMemo(
    () => groupsFlat.filter((x) => x.g.kind === "full").flatMap((x) => x.g.recs || []),
    [groupsFlat]);
  const activeRec = recs.find((r) => ACTIVE_SET.has(r.status)) || null;
  const liveCut = useMemo(() => {
    for (const d of arc?.groups || [])
      for (const g of d.groups) {
        if (g.kind !== "full") continue;
        for (const r of g.recs || [])
          for (const c of r.clips || [])
            if (c.status === "live") return { c, rec: r };
      }
    return null;
  }, [arc]);

  async function toggleRecord() {
    if (!CAN_WRITE) return;
    try {
      if (liveCut) {
        await postJson(`/api/rooms/${ROOM}/recordings/live-cut/${liveCut.c.id}/stop`, {});
        toast("已停止截取，正在切出片段…", "busy");
      } else if (arc?.auto_full && activeRec && activeRec.source === "auto") {
        await postJson(`/api/rooms/${ROOM}/recordings/live-cut/start`, {});
        toast("已开始截取直播片段，再点按钮可停止切出", "busy");
      } else if (!activeRec) {
        await postJson(`/api/rooms/${ROOM}/recordings/start`, {});
        toast("已请求开始录制，启动中…", "busy");
      } else if (activeRec.status === "requested" || activeRec.status === "recording") {
        await postJson(`/api/rooms/${ROOM}/recordings/${activeRec.id}/stop`, {});
        toast("已停止录制，正在拼接保存成片…", "busy");
      }
    } catch (e: any) {
      toast(e.message, "err");
    }
    window.setTimeout(refresh, 400);
  }

  const BigBtn = (() => {
    if (!CAN_WRITE) return null;
    let cls = "rp-big";
    let html: React.ReactNode = "开始录制";
    let disabled = false;
    let tip = "";
    if (liveCut) {
      const dur = (Date.now() - new Date(liveCut.c.start_wall).getTime()) / 1000;
      cls += " rec";
      html = <>停止录制<span className="sub">{fmtDur(dur)}</span></>;
      tip = "正在截取直播片段 · 点击停止并切出（保存在下方当天板块）";
    } else if (arc?.auto_full && activeRec && activeRec.source === "auto") {
      html = "开始录制";
      tip = "自动全程录制进行中 · 点「开始录制」可随时截取当前直播片段";
    } else if (!activeRec) {
      if (arc?.live) {
        html = "开始录制";
      } else {
        disabled = true;
        html = <>开始录制<span className="sub">当前未开播</span></>;
        tip = "等待开播 · 开播后即可手动截取片段";
      }
    } else if (activeRec.status === "requested") {
      html = <>取消请求<span className="sub">启动中…</span></>;
      tip = "正在启动录制…";
    } else if (activeRec.status === "recording") {
      const dur = (Date.now() - new Date(activeRec.started_at || "").getTime()) / 1000;
      cls += " rec";
      html = <>停止录制<span className="sub">{fmtDur(dur)} · {fmtMB(activeRec.live_size)}</span></>;
      tip = "录制中 · 下播后也会自动停止并保存";
    } else {
      disabled = true;
      html = <>正在保存<span className="sub">请稍候…</span></>;
      tip = "正在拼接保存录像，完成后出现在下方列表";
    }
    return (
      <>
        <div className="rp-btn-wrap">
          <button className={cls} disabled={disabled} onClick={toggleRecord}>
            {html}
          </button>
        </div>
        <div className="rp-tip">{tip}</div>
      </>
    );
  })();

  // ---- 搜索 ----
  function recMatches(r: { title?: string; notes?: Note[];
                           clips?: { title?: string; created_by?: string }[] },
                      q: string) {
    const lq = q.toLowerCase();
    if ((r.title || "").toLowerCase().includes(lq)) return true;
    if ((r.notes || []).some((n) =>
      (n.content || "").toLowerCase().includes(lq) ||
      (n.author || "").toLowerCase().includes(lq))) return true;
    return (r.clips || []).some((c) =>
      (c.title || "").toLowerCase().includes(lq) ||
      (c.created_by || "").toLowerCase().includes(lq));
  }
  const matchClipEntry = (c: ClipRow, q: string) =>
    recMatches({ title: c.title, notes: c.notes, clips: [{ title: "", created_by: c.created_by }] }, q);

  const filterHits = useMemo(() => {
    if (!searchQ) return [];
    const hits: { key: string; selStr: string; name: string; tag: string }[] = [];
    for (const { date, key, g } of groupsFlat) {
      if (g.kind === "full") {
        for (const r of g.recs || []) {
          if (recMatches(r, searchQ))
            hits.push({ key, selStr: "r" + r.id, name: entryName(recEntry(r)),
                        tag: date + " · 录播" });
          for (const c of r.clips || [])
            if (matchClipEntry(c, searchQ))
              hits.push({ key, selStr: "c" + c.id, name: entryName(clipEntry(c, r.id)),
                          tag: date + " · " + (r.status === "recording" ? "直播切片" : "切片") });
        }
      } else {
        for (const c of g.clips)
          if (matchClipEntry(c, searchQ))
            hits.push({ key, selStr: (c.etype === "rec" ? "s" : "c") + c.id,
                        name: entryName(clipEntry(c, c.recording_id || c.id)),
                        tag: date + " · " + (g.kind === "standalone" ? "独立片段" : "已归档切片") });
      }
    }
    return hits;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchQ, arc]);

  function frJump(h: { key: string; selStr: string }) {
    if (selMap[h.key] !== h.selStr)
      setSelMap((m) => ({ ...m, [h.key]: h.selStr }));
    const card = document.getElementById("gcard-" + h.key);
    if (!card) return;
    card.scrollIntoView({ block: "center", behavior: "smooth" });
    card.classList.remove("flash");
    void card.offsetWidth;
    card.classList.add("flash");
  }

  // ---- 操作 ----
  // v2.0.2 删除双选项（DelPop 的执行体）：purgeEntry=删除视频（真删除：
  // 行+本地文件+云端对象，录播级联全部挂载切片；收起动画沿用原删除动线）；
  // releaseEntry=删除服务器存档（本地文件即刻释放，条目保留、播放转 OSS）
  async function purgeEntry(e: Entry) {
    toast("正在删除…", "busy");
    try {
      const url = e.etype === "rec"
        ? `/api/rooms/${ROOM}/recordings/${e.id}`
        : `/api/rooms/${ROOM}/clips/${e.id}`;
      await api(url, { method: "DELETE" });
      if (e.etype === "rec") delete dmCache.current[e.id];
      toast("已删除（含相关切片与云端对象）", "ok");
      let target: HTMLElement | null = null;
      if (e.etype === "rec") {
        for (const { key, g } of groupsFlat) {
          if (g.kind === "full" && (g.recs || []).some((r) => r.id === e.id)) {
            // v2.2.2：full 组含多段时只收起被删段所在的列表条目；仅剩
            // 一段（或行已不存在）时收起整张组卡
            target = (g.recs || []).length > 1
              ? document.querySelector(
                  `#gcard-${CSS.escape(key)} .sel-item[data-sel="r${e.id}"]`)
              : document.getElementById("gcard-" + key);
          } else if (g.kind === "standalone" && g.clips.some((c) => c.id === e.id)) {
            target = g.clips.length > 1
              ? document.querySelector(`#gcard-${CSS.escape(key)} .sel-item[data-sel="s${e.id}"]`)
              : document.getElementById("gcard-" + key);
          }
        }
      } else {
        target = document.querySelector(`.sel-item[data-sel="c${e.id}"]`);
        if (target) {
          const card = target.closest(".af-group") as HTMLElement | null;
          const grp = groupsFlat.find(
            ({ g }) => clipsOf(g).some((c) => c.id === e.id))?.g;
          if (grp && grp.kind !== "full" && clipsOf(grp).length <= 1)
            target = card;
        }
      }
      collapsing.current++;
      try {
        await collapseEl(target);
      } finally {
        collapsing.current--;
      }
      refresh();
    } catch (err: any) {
      toast(err.message, "err");
    }
  }

  async function releaseEntry(e: Entry) {
    toast("正在删除服务器存档…", "busy");
    try {
      const url = e.etype === "rec"
        ? `/api/rooms/${ROOM}/recordings/${e.id}/release-local`
        : `/api/rooms/${ROOM}/clips/${e.id}/release-local`;
      await postJson<{ ok: boolean }>(url, {});
      toast("已删除服务器存档（云端保留，播放转云端）", "ok");
      refresh();
    } catch (err: any) {
      toast(err.message, "err");
    }
  }

  async function cancelLiveCut(clipId: number) {
    if (!confirm("取消当前截取？该片段将被放弃且不会保留任何文件。")) return;
    try {
      // v2.0.2 修复：改调 cancel（废弃，条目与文件均不产生）——此前误调
      // stop，行为与停止并保存完全相同
      await postJson(
        `/api/rooms/${ROOM}/recordings/live-cut/${clipId}/cancel`, {});
      toast("已取消截取", "ok");
      refresh();
    } catch (e: any) {
      toast(e.message, "err");
    }
  }

  // ---- 渲染 ----
  const total = groupsFlat.reduce(
    (n, { g }) => n + (g.kind === "full" ? 1 : g.clips.length), 0);

  if (err && !arc)
    return <div className="rp-page"><div className="empty">加载失败：{err}</div></div>;

  return (
    <>
      <div className="rp-corner">
        {IS_ADMIN && <a href={BASE + "manage"}>管理</a>}
        <button onClick={() => void logout()}>
          {me?.role === "guest" ? "切换房间" : "退出"}
        </button>
      </div>

      <aside className={"rp-side" + (sideOpen ? " open" : "")}>
        <div className="side-head">
          <div>
            <div className="side-cap">筛选</div>
            <div className="side-sub">按名字、注释或切片搜索 · 点按钮快速填充</div>
          </div>
          <button className="side-toggle" onClick={() => setSideOpen(!sideOpen)}>
            {sideOpen ? "收起 ▴" : "筛选 ▾"}
          </button>
        </div>
        <div className="side-body">
          <div className="rp-search" style={{ marginBottom: 14 }}>
            <input maxLength={60} placeholder="搜索名字、注释或切片…"
                   value={searchQ}
                   onChange={(e) => setSearchQ(e.target.value)} />
          </div>
          <div className="rp-chips">
            {SEARCH_CHIPS.map((w) => (
              <button key={w} className={"rp-chip" + (searchQ === w ? " on" : "")}
                      onClick={() => setSearchQ(searchQ === w ? "" : w)}>{w}</button>
            ))}
          </div>
          {searchQ && (
            <div className="filter-results">
              {filterHits.length ? (
                <>
                  <div className="fr-cap">筛选结果（{filterHits.length}）· 点击定位</div>
                  {filterHits.slice(0, 40).map((h, i) => (
                    <button key={i} className="fr-item" onClick={() => frJump(h)}>
                      <div className="t">{h.name}</div>
                      <div className="d">{h.tag}</div>
                    </button>
                  ))}
                </>
              ) : (
                <div className="fr-cap">没有匹配的录制或切片</div>
              )}
            </div>
          )}
        </div>
      </aside>

      <div className="rp-page">
        <div className="rp-head">
          <div className="t1">
            {(arc?.anchor_name || "房间 " + ROOM) + " · 直播回放"}
          </div>
          <div className="t2">
            {CAN_WRITE
              ? "点击按钮开始录制，再次点击或下播后自动停止并保存（成片上云长期保存 · 无清理期限）"
              : "录播与切片归档 · 游客只读（切片员登录后可录制/切片）"}
          </div>
        </div>
        {BigBtn}
        {total > 0 && (
          <div className="rp-sec-title">
            直播录制
            <span className="hint">按日期归档 · 自动全程与手动片段 · 可打点切片</span>
          </div>
        )}
        {arc && arc.groups.map((d) => (
          <div key={d.date}>
            <DateRow date={d.date} />
            {d.groups.map((g, i) => (
              <GroupCard
                key={grpKey(d.date, g, i)}
                gkey={grpKey(d.date, g, i)}
                g={g}
                ROOM={ROOM}
                sel={selMap[grpKey(d.date, g, i)] ?? null}
                onPick={(s) => {
                  const k = grpKey(d.date, g, i);
                  setSelMap((m) => ({ ...m, [k]: s }));
                  arm(k);  // 选择列表点击 = 点开该视频：按需加载所在组
                }}
                entry={resolveSel(grpKey(d.date, g, i), g)}
                armed={!!armedKeys[grpKey(d.date, g, i)]}
                autoArm={!anyArmed}
                arm={arm}
                ME={ME}
                IS_ADMIN={IS_ADMIN}
                CAN_WRITE={CAN_WRITE}
                vtMap={vtMap}
                dmCache={dmCache}
                tick={tick}
                marks={marks}
                setMarks={setMarks}
                nameTouched={nameTouched}
                nameDraft={nameDraft}
                drawerKey={drawerKey}
                setDrawerKey={setDrawerKey}
                onRename={(anchor) => setRenamePop({ key: grpKey(d.date, g, i), anchor })}
                onDl={(anchor) => setDlPop({ key: grpKey(d.date, g, i), anchor })}
                onRestore={(anchor) => setRestorePop({ key: grpKey(d.date, g, i), anchor })}
                onDel={(anchor) => setDelPop({ key: grpKey(d.date, g, i), anchor })}
                onSubmit={() => setSubDrawer(grpKey(d.date, g, i))}
                onCancelCut={cancelLiveCut}
                refresh={refresh}
                jobs={jobs}
              />
            ))}
          </div>
        ))}
        {arc && total === 0 && !err && (
          <div className="empty">还没有录制记录</div>
        )}
      </div>

      {renamePop && (
        <RenamePop
          anchor={renamePop.anchor}
          entry={resolveSel(renamePop.key, groupsFlat.find((x) => x.key === renamePop.key)!.g)}
          ROOM={ROOM}
          onClose={() => setRenamePop(null)}
          refresh={refresh}
        />
      )}
      {dlPop && (
        <DlPop
          anchor={dlPop.anchor}
          entry={resolveSel(dlPop.key, groupsFlat.find((x) => x.key === dlPop.key)!.g)}
          ROOM={ROOM}
          onClose={() => setDlPop(null)}
        />
      )}
      {subDrawer && (
        <SubmitDrawer
          entry={resolveSel(subDrawer, groupsFlat.find((x) => x.key === subDrawer)!.g)}
          ROOM={ROOM}
          onClose={() => setSubDrawer(null)}
          onJob={(clipId, job) =>
            setJobs((j) => ({ ...j, [clipId]: job }))}
        />
      )}
      <ClipScrim open={!!subDrawer} onClose={() => setSubDrawer(null)} />
      {restorePop && (
        <RestorePop
          anchor={restorePop.anchor}
          entry={resolveSel(restorePop.key, groupsFlat.find((x) => x.key === restorePop.key)!.g)}
          ROOM={ROOM}
          onClose={() => setRestorePop(null)}
          refresh={refresh}
          onStarted={(recId) => restoring.current.set(recId, Date.now())}
          onDone={(recId) => consumeRestore(recId, true)}
        />
      )}
      {delPop && (
        <DelPop
          anchor={delPop.anchor}
          entry={resolveSel(delPop.key, groupsFlat.find((x) => x.key === delPop.key)!.g)}
          IS_ADMIN={IS_ADMIN}
          CAN_WRITE={CAN_WRITE}
          onClose={() => setDelPop(null)}
          onDelete={purgeEntry}
          onRelease={releaseEntry}
        />
      )}
      <ClipScrim open={!!drawerKey} onClose={() => setDrawerKey(null)} />
    </>
  );
}

// ---------------------------------------------------------------------------
// 日期行
// ---------------------------------------------------------------------------
function DateRow({ date }: { date: string }) {
  const l = afDateLabel(date);
  if (!l.main) return null;
  return (
    <div className="rp-date">
      <span className="d">
        {l.main}
        <small>{l.sub}</small>
      </span>
      <span className="rule" />
    </div>
  );
}
// ---------------------------------------------------------------------------
// 组卡
// ---------------------------------------------------------------------------
function GroupCard(p: {
  gkey: string;
  g: Group;
  ROOM: number;
  sel: string | null;
  onPick: (s: string) => void;
  entry: Entry | null;
  armed: boolean;
  autoArm: boolean;
  arm: (k: string, first?: boolean) => void;
  ME: string;
  IS_ADMIN: boolean;
  CAN_WRITE: boolean;
  vtMap: React.RefObject<Record<string, number>>;
  dmCache: React.RefObject<Record<number, { items: any[]; approx: boolean }>>;
  tick: number;
  marks: Record<number, { inV: number | null; outV: number | null }>;
  setMarks: React.Dispatch<React.SetStateAction<Record<number, { inV: number | null; outV: number | null }>>>;
  nameTouched: React.RefObject<Record<number, boolean>>;
  nameDraft: React.RefObject<Record<number, string>>;
  drawerKey: string | null;
  setDrawerKey: (k: string | null) => void;
  onRename: (anchor: HTMLElement) => void;
  onDl: (anchor: HTMLElement) => void;
  onRestore: (anchor: HTMLElement) => void;
  onDel: (anchor: HTMLElement) => void;
  onSubmit: () => void;   // v2.3.0：气泡改抽屉，不再需要锚点
  onCancelCut: (cid: number) => void;
  refresh: () => void;
  jobs: Record<number, { id: string; status: string; bv: string; error: string }>;
}) {
  const { g, gkey, sel, entry } = p;
  // v2.2.2：full 组同日多段——recs 全部置顶（新→旧）；rec = 当前选中的
  // 录制段（无选中时落到在录段/最新段，驱动直播占位盒与切片抽屉）
  const recs = g.kind === "full" ? (g.recs || []) : [];
  const rec = g.kind === "full"
    ? (recs.find((r) => sel === "r" + r.id) ||
       recs.find((r) => !AF_TERMINAL.has(r.status)) || recs[0] || null)
    : null;
  const clips = g.kind === "full" ? (rec?.clips || []) : g.clips;
  const selKey = sel || "";

  const effEntry = entry;

  // 初始自动加载：本组是「当前无可加载组」时第一个可播放的组 → 胜出
  // （armLock 保证挂载批内仅一个；纯在录/占位组不参与）
  useEffect(() => {
    const playable = !!effEntry && !effEntry.disabled && !effEntry.live &&
      !!(effEntry.fileUrl || effEntry.onOss);
    if (p.autoArm && playable) p.arm(gkey, true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.autoArm, effEntry?.id, effEntry?.disabled, effEntry?.live, gkey]);
  const name = effEntry ? entryName(effEntry) : "";

  const recSt = rec?.status || "";
  const saving = recSt === "finalizing" || recSt === "stop_requested";

  // 操作区（气泡锚定用 e.currentTarget，无需常驻 ref）
  const job = effEntry && !effEntry.isFullRec ? p.jobs[effEntry.id] : undefined;

  // 切片条目按钮（full 组各段与 standalone/orphan 桶共用同一渲染）
  function selClipBtn(c: ClipRow, recId: number) {
    const e = clipEntry(c, recId);
    const s = selStrOf(e);
    const cj = p.jobs[c.id];
    return (
      <button
        key={s}
        className={"sel-item" + (sel === s ? " on" : "")}
        data-sel={s}
        onClick={() => p.onPick(s)}
      >
        <span className="sel-title">
          {entryName(e)}
          {e.live ? "（截取中）" : ""}
        </span>
        {e.live && <span className="badge rec pulse">截取中</span>}
        {e.status === "failed" && <span className="badge err">失败</span>}
        {e.status === "processing" && <span className="badge cut pulse">切片中</span>}
        {e.status === "pending" && <span className="badge pend">排队中</span>}
        {(cj || (c.submission && c.submission.alive !== 0)) && (
          <span className={"badge " + (cj && cj.status !== "success" && cj.status !== "failed" ? "pend pulse" : "cut")}>
            {cj && cj.status !== "success" && cj.status !== "failed" ? "投稿中" : "已投稿"}
          </span>
        )}
        {!e.live && (
          <span className="sel-dur">
            {fmtShort(e.durationSec || e.endSec - e.startSec || 0)}
          </span>
        )}
      </button>
    );
  }

  return (
    <div className="af-group" id={"gcard-" + gkey}>
      <div className="af-main">
        <div className="af-main-head">
          <div className="row1">
            <span className="gtitle-bold">{name || (saving ? "正在生成录播成片" : "直播中 · 暂无切片")}</span>
            <span className="g-sub">
              {effEntry
                ? effEntry.live
                  ? "截取中"
                  : effEntry.etype === "rec"
                    ? (effEntry.createdAt || "").slice(5, 16).replace("T", " ") +
                      (effEntry.endedAt ? " – " + effEntry.endedAt.slice(5, 16).replace("T", " ") : " 起")
                    : effEntry.createdBy ? effEntry.createdBy + " 切出" : ""
                : ""}
            </span>
            <div className="ops">
              {effEntry && effEntry.live && p.CAN_WRITE && (
                <button className="danger" onClick={() => p.onCancelCut(effEntry.id)}>
                  取消截取
                </button>
              )}
              {effEntry && !effEntry.live && !effEntry.disabled && (
                <>
                  {/* v2.0.0：下载入口统一走气泡（开关「同期弹幕下载」+ 双下载连发） */}
                  {(effEntry.inLocal !== false && effEntry.fileUrl) ||
                   (effEntry.onOss && effEntry.ossFullKey) ? (
                    <button onClick={(e) => p.onDl(e.currentTarget)}>下载</button>
                  ) : null}
                  {p.CAN_WRITE && effEntry.isFullRec &&
                    effEntry.fileUrl && effEntry.inLocal !== false && (
                    <button onClick={() => p.setDrawerKey(gkey)}>切片</button>
                  )}
                  {/* v2.0.1：本地已释放 → 切片改为弹回灌气泡（云端拉原件后可切）。
                      data-restore-btn 供回灌气泡动态重锚（组卡重渲染重建按钮时跟随） */}
                  {p.CAN_WRITE && effEntry.isFullRec && effEntry.onOss &&
                    effEntry.ossFullKey && effEntry.inLocal === false && (
                    <button onClick={(e) => p.onRestore(e.currentTarget)}
                            data-restore-btn={effEntry.id}
                            title="本地原片已按留存策略移除；点击从云端拉取原件后即可切片">
                      切片
                    </button>
                  )}
                  {effEntry.etype === "rec"
                    ? p.IS_ADMIN && (
                        <button onClick={(e) => p.onRename(e.currentTarget)}>
                          {effEntry.title ? "重命名" : "命名"}
                        </button>)
                    : p.CAN_WRITE && (
                        <button onClick={(e) => p.onRename(e.currentTarget)}>
                          重命名
                        </button>)}
                  {/* v2.0.0 切片投稿（clipper/admin） */}
                  {!effEntry.isFullRec && effEntry.etype === "clip" && p.CAN_WRITE &&
                    effEntry.status === "done" && (
                    <button onClick={() => p.onSubmit()}>投稿</button>
                  )}
                  {/* v2.0.1 删除双选项气泡（原 confirm 直删改为 DelPop） */}
                  {effEntry.etype === "rec"
                    ? p.IS_ADMIN && (
                      <button className="danger"
                              onClick={(ev) => p.onDel(ev.currentTarget)}>删除</button>)
                    : p.CAN_WRITE && (
                      <button className="danger"
                              onClick={(ev) => p.onDel(ev.currentTarget)}>删除</button>)}
                </>
              )}
            </div>
          </div>
          {g.kind === "standalone" && (
            <div className="meta">独立片段收录 · 无对应全段录播</div>
          )}
        </div>

        <div className={
          "af-player" +
          (p.drawerKey === gkey && effEntry?.isFullRec && !effEntry.live ? " docked" : "")
        }>
          <VideoStage
            entry={effEntry}
            selKey={selKey}
            gkey={gkey}
            ROOM={p.ROOM}
            rec={rec}
            saving={saving}
            vtMap={p.vtMap}
            dmCache={p.dmCache}
            tick={p.tick}
            armed={p.armed}
            onArm={() => p.arm(gkey)}
          />
        </div>

        {effEntry && effEntry.status !== "live" && !effEntry.live && (
          <div className="meta">
            {(effEntry.isFullRec
              ? (effEntry.source === "bridge" ? "轮换衔接 · " : "本场录播 · ")
              : "") +
              fmtDur(effEntry.durationSec) + " · " + fmtMB(effEntry.fileSize) + " · " +
              (effEntry.etype === "rec"
                ? (effEntry.qn >= 200 ? "4K 档" : "1080P60")
                : "切片") +
              (effEntry.onOss && effEntry.inLocal === false ? " · 云端" : "")}
            {((job || effEntry?.submission) && (!effEntry?.submission || effEntry.submission.alive !== 0)) && (
              <span className="badge pend pulse" style={{ marginLeft: 10 }}>
                {job && job.status !== "success" && job.status !== "failed"
                  ? "投稿中" : "已投稿"}
              </span>
            )}
          </div>
        )}
        {effEntry && effEntry.live && (
          <div className="meta">
            起点 {wallHMS(effEntry.startWall)} · 点击「取消截取」可放弃该片段
          </div>
        )}

        {effEntry && (
          <Notes
            entry={effEntry}
            ROOM={p.ROOM}
            ME={p.ME}
            CAN_WRITE={p.CAN_WRITE}
            refresh={p.refresh}
          />
        )}
      </div>

      <div className="af-side-list">
        <div className="pl-cap">
          视频列表（{g.kind === "full"
            ? recs.length + recs.reduce((n, r) => n + (r.clips?.length || 0), 0)
            : clips.length}）
        </div>
        {/* v2.2.2：孤儿组说明自主区 meta 移到「视频列表」下一行（用户
            2026-10-14 定稿：说明跟随切片列表而非组头） */}
        {g.kind === "orphan" && (
          <div className="pl-cap-sub">源录播已按保留策略清理，切片独立存续</div>
        )}
        {/* v2.2.2：同日全部录制段置顶（新→旧，轮换桥接片按其窗口起点
            自然落在相邻两段之间），其下按段分组挂各自切片；段与更早一场
            之间（session_end 行上方）画场次分隔线 */}
        {g.kind === "full" && recs.map((r) => (
          <Fragment key={r.id}>
            {r.session_end && <div className="sel-divider" />}
            <TopRecItem rec={r} cur={sel === "r" + r.id}
                        onPick={() => p.onPick("r" + r.id)} />
          </Fragment>
        ))}
        {g.kind === "full"
          ? recs.map((r) => {
              const rclips = r.clips || [];
              if (!rclips.length) return null;
              const live = r.status === "recording" || r.status === "requested";
              const saving = r.status === "finalizing" || r.status === "stop_requested";
              return (
                <div key={"rc-" + r.id}>
                  <div className="sel-divider">
                    <i>{recs.length > 1
                      ? (live || saving ? "直播切片" : wallHM(r.started_at) + " 场切片")
                      : (live || saving ? "直播切片" : "本场切片")}</i>
                  </div>
                  {rclips.map((c) => selClipBtn(c, r.id))}
                </div>
              );
            })
          : clips.map((c) => selClipBtn(c, c.recording_id || c.id))}
      </div>

      <ClipDrawer
        gkey={gkey}
        open={p.drawerKey === gkey}
        onClose={() => p.setDrawerKey(null)}
        rec={rec}
        entry={effEntry}
        ROOM={p.ROOM}
        ME={p.ME}
        CAN_WRITE={p.CAN_WRITE}
        marks={p.marks}
        setMarks={p.setMarks}
        nameTouched={p.nameTouched}
        nameDraft={p.nameDraft}
        refresh={p.refresh}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 置顶录播条目（full 组；同日多段时逐段渲染，衔接片带徽标）
function TopRecItem(p: {
  rec: RecRow; cur: boolean; onPick: () => void;
}) {
  const r = p.rec;
  const live = r.status === "recording" || r.status === "requested";
  const saving = r.status === "finalizing" || r.status === "stop_requested";
  const cutting = !live && !saving &&
    (r.clips || []).some((c) => c.status === "pending" || c.status === "processing");
  const disabled = live || saving || r.status === "failed" || !r.file_url;
  const bridge = r.source === "bridge";
  return (
    <button
      className={"sel-item" + (p.cur ? " on" : "")}
      data-sel={"r" + r.id}
      disabled={disabled}
      onClick={p.onPick}
    >
      <span className="sel-title">
        {r.title
          || (bridge
            ? bridgeName(r.started_at, r.ended_at || "")
            : "本场录播 " + (r.started_at || "").slice(11, 16))}
        {live ? "（录制中）" : ""}
        {saving ? "（正在保存）" : ""}
      </span>
      {bridge && <span className="badge cut">衔接</span>}
      {live && <span className="badge rec pulse">直播中</span>}
      {saving && <span className="badge cut pulse">保存中</span>}
      {cutting && <span className="badge cut pulse">切片中</span>}
      {r.status === "failed" && <span className="badge err">失败</span>}
      {r.status === "interrupted" && <span className="badge err">已中断保存</span>}
      <span className="sel-dur">{live || saving ? "—" : fmtShort(r.duration_sec)}</span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// 播放器（候选链 + 弹幕层 + 进度保存）
// ---------------------------------------------------------------------------
function VideoStage(p: {
  entry: Entry | null;
  selKey: string;
  gkey: string;
  ROOM: number;
  rec: RecRow | null;
  saving: boolean;
  vtMap: React.RefObject<Record<string, number>>;
  dmCache: React.RefObject<Record<number, { items: any[]; approx: boolean }>>;
  tick: number;
  armed: boolean;      // 懒加载：armed 才挂载 <video>（此前只显占位）
  onArm: () => void;
}) {
  const { entry, selKey } = p;
  const vidRef = useRef<HTMLVideoElement>(null);
  const dmRef = useRef<DanmakuLayer | null>(null);
  const [cloudFail, setCloudFail] = useState(false);
  // 点击占位加载后自动起播（真实点击手势允许带声播放；被策略拦截时
  // 由用户手动按播放）。置位后等挂载了 video 消费一次
  const playOnLoad = useRef(false);

  // 卸载/切换时保存进度
  useEffect(() => {
    const v = vidRef.current;
    return () => {
      if (v && v.currentTime > 0) p.vtMap.current![selKey] = v.currentTime;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selKey]);

  // 挂载候选链（本机 low → 原片；OSS low → 本体；失败占位）
  useEffect(() => {
    setCloudFail(false);
    const v = vidRef.current;
    if (!v || !entry) return;
    dmRef.current?.destroy();
    dmRef.current = null;

    // v1.3.x 候选链按类型分化（2026-10-11 用户定稿）：全程录播 OSS 优先
    // ——服务器零留存（上云即释），播放流量不经服务器 3Mbps 上行；本机
    // 文件仅在上传完成前的窗口或 OSS 不可用时兜底。
    // 手动切片维持本机优先（同零留存：上云即释，本机文件只存在于处理
    // 完成前的窗口），本机无文件再走 OSS
    const ossFirst = entry.isFullRec;
    if (entry.fileUrl && entry.inLocal !== false && !ossFirst) {
      const lowUrl = entry.fileUrl.replace(/\.mp4$/, "/low.mp4");
      v.src = lowUrl;
      v.onerror = () => {
        if (v.getAttribute("src") !== entry.fileUrl) v.src = entry.fileUrl;
      };
    } else if (entry.onOss || (entry.fileUrl && entry.inLocal !== false)) {
      const localFallback = () => {
        // OSS 全失败 → 本机兜底（仅上传完成前的窗口内有本地文件）
        if (entry.fileUrl && entry.inLocal !== false) {
          const lowUrl = entry.fileUrl.replace(/\.mp4$/, "/low.mp4");
          v.src = lowUrl;
          v.onerror = () => {
            if (v.getAttribute("src") !== entry.fileUrl) v.src = entry.fileUrl;
          };
        } else {
          setCloudFail(true);
        }
      };
      const keys = [entry.ossLowKey, entry.ossFullKey].filter(Boolean);
      let dead = false;
      (async () => {
        for (const k of keys) {
          try {
            const url = await getOssUrl(k);
            if (dead || !vidRef.current) return;
            v.src = url;
            const ok = await new Promise<boolean>((res) => {
              const good = () => res(true);
              v.addEventListener("loadedmetadata", good, { once: true });
              v.addEventListener("error", () => res(false), { once: true });
            });
            if (ok) return;
          } catch { /* 下一个候选 */ }
        }
        if (!dead) localFallback();
      })();
      return () => {
        dead = true;
      };
    } else {
      v.removeAttribute("src");
    }
    // p.armed 进依赖：懒加载 arm 前占位分支不渲染 <video>（vidRef 为
    // null 时本 effect 直接返回），arm 后重新渲染出 video 才真正挂 src
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry?.id, entry?.fileUrl, entry?.inLocal, entry?.ossLowKey, entry?.ossFullKey, p.armed]);

  // 进度恢复 + 弹幕层（rec 形态）
  useEffect(() => {
    const v = vidRef.current;
    if (!v || !entry) return;
    // 单播放器激活：本视频开始播放时，暂停主界面其余所有视频；
    // 同时把自己登记为键盘控制的目标播放器（activeVideo）
    const onPlay = () => {
      activeVideo = v;
      document.querySelectorAll("video").forEach((o) => {
        if (o !== v && !o.paused) o.pause();
      });
    };
    v.addEventListener("play", onPlay);
    const pv = p.vtMap.current![selKey];
    if (pv) {
      const once = () => {
        try {
          v.currentTime = pv;
        } catch { /* 元数据未就绪 */ }
      };
      v.addEventListener("loadedmetadata", once, { once: true });
    }
    if (entry.etype === "rec") {
      const layer = attachDanmaku(v);
      dmRef.current = layer;
      const cached = p.dmCache.current![entry.id];
      if (cached) {
        layer.setItems(cached.items, cached.approx);
      } else {
        fetch(`${BASE}api/rooms/${p.ROOM}/recordings/${entry.id}/danmaku`)
          .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
          .then((d) => {
            p.dmCache.current![entry.id] = {
              items: d.items || [], approx: !!d.approx,
            };
            layer.setItems(d.items || [], !!d.approx);
          })
          .catch(() => {});
      }
    }
    return () => {
      v.removeEventListener("play", onPlay);
      if (activeVideo === v) activeVideo = null;
      dmRef.current?.destroy();
      dmRef.current = null;
    };
    // p.armed 进依赖：同上，arm 后 video 才挂载，此时才挂弹幕层/拉弹幕
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [entry?.id, entry?.etype, selKey, p.armed]);

  // 点击占位加载 → 自动起播一次（消费 playOnLoad 标记）。不直接 play()：
  // 候选链可能先探测 low.mp4（404 后换 src），挂在失败 src 上的 play()
  // 会被拒绝且换 src 后不重试——改为等 canplay 就绪后再起播
  useEffect(() => {
    const v = vidRef.current;
    if (!v || !playOnLoad.current) return;
    playOnLoad.current = false;
    const tryPlay = () => v.play().catch(() => {
      /* 自动播放被策略拦截则由用户手动播放 */ });
    if (v.readyState >= 2) tryPlay();
    else v.addEventListener("canplay", tryPlay, { once: true });
    return () => v.removeEventListener("canplay", tryPlay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.armed, entry?.id, selKey]);

  // 状态盒（样式与旧版各分支一致）。armed 后 <video> 恒挂载：弹幕层
  // （danmaku.ts）构造时会把 React 挂载的 <video> 挪进自建 bdm-wrap，
  // 若某状态（cloudFail/live/disabled/entry 翻转）把渲染分支从 <video>
  // 切回消息盒，React 卸载 video 时 removeChild 找不到直接子节点 →
  // NotFoundError 在 commit 阶段抛出 → React 拆除整个 root（2026-10-12
  // 本地复现：OSS 未启用 + in_local=false → cloudFail 翻转必触发）。
  // useEffect 属 passive 副作用，其清理在 DOM 移除之后才执行，无法在
  // 卸载前归还节点——故从结构上杜绝：armed 后状态盒作同级渲染，video
  // 以 display:none 隐藏（2026-10-12 修复）。
  let overlay: React.ReactNode = null;
  if (!entry) {
    // 在录/保存中：黑底占位（时长随 tick 跳动）
    const started = p.rec?.started_at;
    const lv = started
      ? fmtDur((Date.now() - new Date(started).getTime()) / 1000)
      : "0:00";
    overlay = (
      <div className="af-livebox">
        {p.saving
          ? <span className="badge cut pulse">正在生成</span>
          : <span className="badge rec pulse">直播中</span>}
        <span className="lv">{lv}</span>
        <span className="lb-tip">
          {p.saving ? "本场直播已结束，正在生成录播成片…" : "正在直播中，无录播视频"}
        </span>
      </div>
    );
  } else if (entry.disabled) {
    overlay = (
      <div className="af-placeholder">
        <span className="t1">{entry.reason || "录制失败"}</span>
        <span className="t2">该条录制没有可用成片</span>
      </div>
    );
  } else if (entry.live) {
    overlay = (
      <div className="af-livebox">
        <span className="lv">—</span>
        <span className="badge rec pulse">截取中</span>
      </div>
    );
  } else if ((entry.status === "processing" || entry.status === "pending") && !entry.fileUrl && !entry.onOss) {
    overlay = (
      <div className="af-livebox">
        <span className="badge cut pulse">切片中</span>
        <span className="lb-tip">切片正在生成，完成后此处可播放</span>
      </div>
    );
  } else if (cloudFail) {
    overlay = (
      <div className="af-placeholder">
        <span className="t1">云端视频暂时无法加载</span>
        <span className="t2">请稍后重试或联系管理员检查 OSS 状态</span>
      </div>
    );
  }
  // armed 前的各状态（直播中/失败/切片中）：从未挂载过 video，直接渲染
  // 状态盒（与旧行为一致）；armed 且状态正常：唯一无覆盖层的纯播放态
  if (!p.armed) {
    if (overlay) return overlay;
    // 懒加载占位：不挂载 <video>（不探测候选链/不拉元数据/不拉弹幕），
    // 点击后才开始加载并自动起播（起播会经单播放器互斥暂停其余）
    return (
      <div className="af-stage-ph" role="button" tabIndex={0}
           onClick={() => { playOnLoad.current = true; p.onArm(); }}
           onKeyDown={(e) => {
             if (e.key === "Enter" || e.key === " ") {
               e.preventDefault();
               playOnLoad.current = true;
               p.onArm();
             }
           }}>
        <span className="ph-play">▶</span>
        <span className="ph-name">{entry ? entryName(entry) : "视频"}</span>
        <span className="ph-hint">点击加载并播放</span>
      </div>
    );
  }
  return (
    <>
      <video ref={vidRef} controls preload="metadata" data-selkey={selKey}
             style={overlay ? { display: "none" } : undefined} />
      {overlay}
    </>
  );
}

// ---------------------------------------------------------------------------
// 注释区
// ---------------------------------------------------------------------------
function Notes(p: {
  entry: Entry; ROOM: number; ME: string; CAN_WRITE: boolean;
  refresh: () => void;
}) {
  const [adding, setAdding] = useState<string | undefined>(undefined);
  const [editing, setEditing] = useState<number | null>(null);
  const [editVal, setEditVal] = useState("");
  const notes = p.entry.notes || [];
  const dk = p.entry.etype + p.entry.id;

  async function submit() {
    const v = (adding || "").trim();
    if (!v) {
      toast("请输入注释内容");
      return;
    }
    const base = p.entry.etype === "rec"
      ? `/recordings/${p.entry.id}/notes`
      : `/clips/${p.entry.id}/notes`;
    try {
      await postJson(`/api/rooms/${p.ROOM}${base}`, { content: v });
      setAdding(undefined);
      p.refresh();
    } catch (e: any) {
      toast(e.message);
    }
  }

  async function save(noteId: number) {
    const v = editVal.trim();
    if (!v) {
      toast("注释内容不能为空");
      return;
    }
    const base = p.entry.etype === "rec"
      ? `/recordings/${p.entry.id}/notes/${noteId}`
      : `/clips/${p.entry.id}/notes/${noteId}`;
    try {
      await patchJson(`/api/rooms/${p.ROOM}${base}`, { content: v });
      setEditing(null);
      p.refresh();
    } catch (e: any) {
      toast(e.message);
    }
  }

  async function del(noteId: number) {
    if (!confirm("确定删除这条注释吗？")) return;
    const base = p.entry.etype === "rec"
      ? `/recordings/${p.entry.id}/notes/${noteId}`
      : `/clips/${p.entry.id}/notes/${noteId}`;
    try {
      await api(`/api/rooms/${p.ROOM}${base}`, { method: "DELETE" });
      p.refresh();
    } catch (e: any) {
      toast(e.message);
    }
  }

  return (
    <div className="note-sec">
      <div className="ns-cap">
        注释<span className="cnt">({notes.length})</span>
        {adding === undefined && p.ME && (
          <button className="ns-add" onClick={() => setAdding("")}>添加注释</button>
        )}
        {adding === undefined && !p.ME && (
          <span style={{ fontSize: 11, color: "var(--faint)", marginLeft: "auto" }}>
            {p.CAN_WRITE
              ? "重新登录（填写昵称）后可添加注释"
              : "游客只读 · 切片员登录后可添加注释"}
          </span>
        )}
      </div>
      {adding !== undefined ? (
        <>
          <textarea rows={2}
                    placeholder={`以「${p.ME || ""}」的身份添加注释…`}
                    value={adding}
                    onChange={(e) => setAdding(e.target.value)} />
          <div className="ns-submit">
            <button className="btn sm" onClick={submit}>添加</button>
            <button className="btn sm" onClick={() => setAdding(undefined)}>取消</button>
          </div>
        </>
      ) : notes.length ? (
        notes.map((n) => (
          <div className="note-card" key={n.id + "-" + dk}
               style={{ marginBottom: 6 }}>
            <div className="nc-head">
              <b>{n.author}</b>
              <span>
                {(n.updated_at || "").replace("T", " ").slice(5, 16)}
                {n.updated_at !== n.created_at ? " · 已编辑" : ""}
              </span>
              {p.ME && n.author === p.ME && (
                <span className="nc-ops">
                  <button onClick={() => { setEditing(n.id); setEditVal(n.content); }}>
                    编辑
                  </button>
                  <button onClick={() => del(n.id)}>删除</button>
                </span>
              )}
            </div>
            {editing === n.id ? (
              <div className="nc-body">
                <textarea rows={3} value={editVal}
                          onChange={(e) => setEditVal(e.target.value)} />
                <div className="nc-editops">
                  <button className="btn sm primary" onClick={() => save(n.id)}>保存</button>
                  <button className="btn sm" onClick={() => setEditing(null)}>取消</button>
                </div>
              </div>
            ) : (
              <div className="nc-body">{n.content}</div>
            )}
          </div>
        ))
      ) : (
        <div className="note-empty">还没有注释</div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 重命名气泡
// ---------------------------------------------------------------------------
function RenamePop(p: {
  anchor: HTMLElement | null;
  entry: Entry | null;
  ROOM: number;
  onClose: () => void;
  refresh: () => void;
}) {
  const [val, setVal] = useState(p.entry?.title || "");
  const inputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const el = inputRef.current;
    if (el) {
      el.focus();
      el.setSelectionRange(el.value.length, el.value.length);
    }
  }, []);
  async function save() {
    if (!p.entry) {
      p.onClose();
      return;
    }
    const v = val.trim();
    const base = p.entry.etype === "rec"
      ? `/recordings/${p.entry.id}` : `/clips/${p.entry.id}`;
    try {
      await patchJson(`/api/rooms/${p.ROOM}${base}`, { title: v });
      p.onClose();
      p.refresh();
    } catch (e: any) {
      toast(e.message);
    }
  }
  return (
    <Pop anchor={p.anchor} onClose={p.onClose}>
      <div className="cap">重命名 · {p.entry?.etype === "rec" ? "录制" : "切片"}</div>
      <input maxLength={60} ref={inputRef} value={val}
             placeholder="名字（留空保存=清除）"
             onChange={(e) => setVal(e.target.value)}
             onKeyDown={(e) => e.key === "Enter" && save()} />
      <div className="ops">
        <button className="btn sm primary" onClick={save}>保存</button>
        <button className="btn sm" onClick={p.onClose}>取消</button>
      </div>
    </Pop>
  );
}

// ---------------------------------------------------------------------------
// v2.0.0 下载气泡（ASS 旁挂：开关默认关；开启后双下载连发）
// ---------------------------------------------------------------------------
function DlPop(p: {
  anchor: HTMLElement | null;
  entry: Entry | null;
  ROOM: number;
  onClose: () => void;
}) {
  const [withDm, setWithDm] = useState(false);
  const e = p.entry;
  if (!e) return null;
  const base = `room${p.ROOM}-${e.etype}${e.id}`;
  const ent: Entry = e;

  async function doDownload() {
    try {
      if (withDm) {
        // ASS 先发（与视频严格同 basename，播放器同名自动加载）
        const dmPath = ent.etype === "rec"
          ? `/api/rooms/${p.ROOM}/recordings/${ent.id}/danmaku?format=ass&filename=${base}`
          : `/api/rooms/${p.ROOM}/clips/${ent.id}/danmaku?format=ass&filename=${base}`;
        const r = await fetch(BASE + dmPath);
        if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "弹幕文件生成失败");
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        triggerDownload(url, base + ".ass");
        setTimeout(() => URL.revokeObjectURL(url), 8000);
      }
      // 视频下载：本机直链（恒原画质）或 OSS 签名（已带附件头）
      if (ent.fileUrl && ent.inLocal !== false) {
        triggerDownload(ent.fileUrl, base + ".mp4");
      } else if (ent.onOss && ent.ossFullKey) {
        const url = await getOssUrl(ent.ossFullKey);
        window.open(url, "_blank", "noopener");
      }
      p.onClose();
    } catch (err: any) {
      toast(err.message, "err");
    }
  }

  return (
    <Pop anchor={p.anchor} onClose={p.onClose} width={310}>
      <div className="cap">下载 · {entryName(e)}</div>
      <label style={{ display: "flex", alignItems: "center", gap: 10, fontSize: 13, cursor: "pointer" }}>
        <Switch on={withDm} onClick={() => setWithDm(!withDm)} />
        同期弹幕下载（.ass）
      </label>
      <div className="ops">
        <button className="btn sm primary" onClick={doDownload}>下载</button>
        <button className="btn sm" onClick={p.onClose}>取消</button>
      </div>
      {withDm && (
        <div className="note-line">
          将同时下载视频与同名 .ass 弹幕字幕（本地播放器同名自动加载）；
          浏览器可能询问「允许下载多个文件」，点允许即可。
          弹幕不随 B 站投稿（投稿不吃外挂字幕）。
        </div>
      )}
    </Pop>
  );
}

// ---------------------------------------------------------------------------
// v2.0.1 原片回灌气泡（切片动线：本地已释放 → 确认从云端拉原件 + 进度条）
// ---------------------------------------------------------------------------
// 估时基准：内网 endpoint 顺序拉取实测 ~70MiB/s，按 3.5GiB/分保守取整
const RESTORE_GIB_PER_MIN = 3.5;

function RestorePop(p: {
  anchor: HTMLElement | null;
  entry: Entry | null;
  ROOM: number;
  onClose: () => void;
  refresh: () => void;
  onStarted: (recId: number) => void;
  onDone: (recId: number) => void;
}) {
  const e = p.entry;
  // phase：idle 确认态 → running 拉取中（1s 轮询进度）→ done 完成后停 3s
  // 自动关闭 → failed 可重试
  const [phase, setPhase] = useState<"idle" | "running" | "done" | "failed">("idle");
  const [st, setSt] = useState<{ status: string; got: number;
                                 total: number; error: string } | null>(null);
  const estMin = e && e.fileSize
    ? Math.max(1, Math.ceil(e.fileSize / (RESTORE_GIB_PER_MIN * 1024 ** 3)))
    : 1;

  // 完成后 3 秒自动关气泡（期间展示 100% 进度）。提前手动关闭会取消定时器，
  // 完成通知由归档 2.5s diff 兜底（保持 v2.0.1「提前关不中断拉取」的语义）
  const closeTimer = useRef<number | null>(null);
  useEffect(() => () => {
    if (closeTimer.current) window.clearTimeout(closeTimer.current);
  }, []);

  function finish() {
    p.onDone(e!.id);
    p.refresh();          // 立即刷归档：in_local 翻转、切片按钮恢复
    p.onClose();
  }

  function complete() {
    setPhase("done");
    if (closeTimer.current != null) return;   // 防 1s 轮询重复排程
    closeTimer.current = window.setTimeout(finish, 3000);
  }

  // 拉取中每 1s 轮询任务状态；done → 停 3s 自动关，failed → 可重试。
  // v2.2.4 修复：此前用裸 fetch(BASE + "/api/...") 拼出 /replay//api 双斜杠
  // 路径（BASE 尾带斜杠），轮询恒 404 静默失败——改走 api()（自动去重斜杠）；
  // 连续 404 超限（任务表丢失，如 web 重启）→ 转 failed 可重试
  useEffect(() => {
    if (phase !== "running" || !e) return;
    let dead = false;
    let missCount = 0;
    const t = window.setInterval(async () => {
      try {
        const s = await api<{ status: string; got: number;
                              total: number; error: string }>(
          `/api/rooms/${p.ROOM}/recordings/${e.id}/restore-oss`);
        if (dead) return;
        missCount = 0;
        setSt(s);
        if (s.status === "done") complete();
        else if (s.status === "failed") {
          setPhase("failed");
          toast("录播拉取失败：" + (s.error || "未知错误"), "err");
        }
      } catch (err: any) {
        if (dead) return;
        // 瞬时失败下一轮再试；任务表丢失（404）连续 10 次 → 转可重试
        if (err && err.status === 404 && ++missCount >= 10) {
          setPhase("failed");
          toast("回灌任务已丢失（服务可能重启过），请重试", "err");
        }
      }
    }, 1000);
    return () => { dead = true; window.clearInterval(t); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [phase, e?.id]);

  async function start() {
    if (!e) return;
    setPhase("running");
    try {
      const r = await postJson<{ status: string; got: number;
                                 total: number; error: string }>(
        `/api/rooms/${p.ROOM}/recordings/${e.id}/restore-oss`, {});
      setSt(r);
      if (r.status === "done") {
        p.onStarted(e.id);   // 幂等命中（他处已回灌）：先登记再走完成口
        complete();
        return;
      }
      if (r.status === "failed") {
        setPhase("failed");
        toast("录播拉取失败：" + (r.error || "未知错误"), "err");
        return;
      }
      p.onStarted(e.id);   // 登记完成提示（气泡被关也由归档 diff 兜底）
    } catch (err: any) {
      setPhase("failed");
      toast(err.message, "err");
    }
  }

  if (!e) return null;
  const pct = st && st.total
    ? Math.min(100, Math.floor((st.got / st.total) * 100)) : 0;

  return (
    <Pop anchor={p.anchor} onClose={p.onClose} width={330}
         closeOnOutside={phase === "idle" || phase === "failed"}
         anchorResolve={() => (e ? (document.querySelector(
           `button[data-restore-btn="${e.id}"]`) as HTMLElement | null) : null)}>
      <div className="cap">切片 · 原片回灌 · {entryName(e)}</div>
      {phase === "idle" ? (
        <>
          <div className="rs-text">
            本地录播原片已移除（按留存策略释放），切片需要本机原片。
            是否从云端拉取原件？预计约 {estMin} 分钟，完成后即可正常切片。
          </div>
          <div className="ops">
            <button className="btn sm primary" onClick={start}>拉取原件</button>
            <button className="btn sm" onClick={p.onClose}>取消</button>
          </div>
          <div className="note-line">
            仅拉取原画质原件用于切片（不拉低清副本）；回灌后本地保留 24 小时，
            到期自动释放。
          </div>
        </>
      ) : (
        <>
          {/* v2.2.4 改版：气泡不消失，按钮行原位换进度条；完成后停 3 秒自动关 */}
          <div className="rs-text">
            {phase === "failed"
              ? "拉取失败，可重试（服务端已清理半截文件，重试为整档重来）。"
              : phase === "done"
                ? "拉取完成，即将自动关闭…"
                : "正在从云端拉取原件…"}
          </div>
          <div className="rs-progress-row">
            <div className="rs-bar">
              <div style={{ width: (phase === "done" ? 100 : pct) + "%" }} />
            </div>
            <span className="rs-meta mono-num">
              {st ? fmtMB(st.got) : "0 MB"} / {st && st.total ? fmtMB(st.total) : "…"}（{pct}%）
            </span>
          </div>
          {phase === "failed" && (
            <div className="ops">
              <button className="btn sm primary" onClick={start}>重试</button>
              <button className="btn sm" onClick={p.onClose}>取消</button>
            </div>
          )}
        </>
      )}
    </Pop>
  );
}

// ---------------------------------------------------------------------------
// v2.0.2 删除双选项气泡：删除视频（真删除：行+本地文件+云端对象，录播
// 级联全部挂载切片）/ 删除服务器存档（仅已上云可用，云端保留）
// ---------------------------------------------------------------------------
function DelPop(p: {
  anchor: HTMLElement | null;
  entry: Entry | null;
  IS_ADMIN: boolean;
  CAN_WRITE: boolean;
  onClose: () => void;
  onDelete: (e: Entry) => void;
  onRelease: (e: Entry) => void;
}) {
  const e = p.entry;
  const [busy, setBusy] = useState(false);
  if (!e) return null;
  const isRec = e.etype === "rec";
  // 与原删除按钮同门：录播条目=管理员；切片=可写（服务端再校验创建者）。
  // 服务器存档属磁盘资产管理，恒为管理员 + 已上云（未上云本地即唯一副本）
  const canDel = isRec ? p.IS_ADMIN : p.CAN_WRITE;
  const canRelease = p.IS_ADMIN && e.onOss;

  async function del() {
    if (busy) return;
    setBusy(true);
    p.onClose();
    p.onDelete(e!);
  }

  async function release() {
    if (busy) return;
    setBusy(true);
    p.onClose();
    p.onRelease(e!);
  }

  return (
    <Pop anchor={p.anchor} onClose={p.onClose} width={340}>
      <div className="cap">删除 · {entryName(e)}</div>
      <div className="rs-text" style={{ marginBottom: 2 }}>
        「删除视频」连本地文件与云端对象一并删除；「删除服务器存档」保留
        云端副本：
      </div>
      <button className="del-opt" disabled={!canDel || busy} onClick={del}>
        <span className="del-name">删除视频</span>
        <span className="del-desc">
          {isRec && e.isFullRec
            ? "彻底删除该录播及其全部相关切片（本地文件与云端 OSS 对象一并删除，不可恢复）。"
            : "彻底删除该条目（本地文件与云端 OSS 对象一并删除，不可恢复）。"}
        </span>
      </button>
      <button className="del-opt" disabled={!canRelease || busy} onClick={release}>
        <span className="del-name">删除服务器存档</span>
        <span className="del-desc">
          {e.onOss
            ? "立即删除服务器本地文件（云端副本保留，条目保留、播放转云端，可由回灌气泡拉回）。"
            : "未上云，暂不可用——本地文件是该视频唯一副本。"}
        </span>
      </button>
    </Pop>
  );
}

// ---------------------------------------------------------------------------
// v2.3.0 投稿抽屉（v2.0.0 投稿气泡改抽屉：内容放不下合集/分P 表单）
// ---------------------------------------------------------------------------
const TID_OPTIONS: [number, string][] = [
  [4, "游戏"], [171, "电子竞技"], [172, "手机游戏"], [65, "网络游戏"],
  [3, "音乐"], [129, "舞蹈"], [5, "娱乐"], [160, "生活"], [36, "知识"],
  [188, "科技"], [234, "运动"], [1, "动画"], [181, "影视"],
];

interface SeasonSec { id: number; title: string; ep_count: number }
interface Season { season_id: number; title: string; ep_total: number; sections: SeasonSec[] }
interface Arc { aid: number; bvid: string; title: string }
interface ArcPart { cid: number; index: number; title: string }
interface Targets {
  account: { ok: boolean; uname: string; mid: number; face: string };
  seasons: Season[];
  arcs: Arc[];
}

// 记住上次的合集选择（该主播所有视频进同一合集 → 每次预选）
const SUB_SEASON_KEY = "replay-sub-season";

function SubmitDrawer(p: {
  entry: Entry | null;
  ROOM: number;
  onClose: () => void;
  onJob: (clipId: number, job: { id: string; status: string; bv: string; error: string; mode?: string }) => void;
}) {
  const e = p.entry;
  const [open, setOpen] = useState(false);   // 挂载后下一帧翻开（滑入动画）
  const [mode, setMode] = useState<"new" | "part">("new");
  // new 模式表单（v2.0.0 原字段全保留）
  const [title, setTitle] = useState(e?.title || "");
  const [desc, setDesc] = useState("");
  const [tags, setTags] = useState("");
  const [tid, setTid] = useState(171);
  const [sourceType, setSourceType] = useState<"original" | "reprint">("original");
  const [sourceUrl, setSourceUrl] = useState("");
  const [cover, setCover] = useState<string>("");   // data URL / blob URL
  const [coverAvail, setCoverAvail] = useState(false);
  // 合集（v2.3.0）：0 = 不加入
  const [seasonId, setSeasonId] = useState(0);
  const [sectionId, setSectionId] = useState(0);
  // part 模式（v2.3.0）
  const [arcAid, setArcAid] = useState(0);
  const [arcFilter, setArcFilter] = useState("");
  const [parts, setParts] = useState<ArcPart[] | null>(null);
  const [partTitle, setPartTitle] = useState(e?.title || "");
  // 目标数据（账号合集/稿件）与提交态
  const [targets, setTargets] = useState<Targets | null>(null);
  const [tgtErr, setTgtErr] = useState("");
  const [busy, setBusy] = useState(false);
  const fileRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const t = requestAnimationFrame(() => setOpen(true));
    return () => cancelAnimationFrame(t);
  }, []);

  const loadTargets = useCallback(async () => {
    setTgtErr("");
    try {
      const d = await api<Targets>("/api/submit/targets");
      setTargets(d);
      // 回填上次选择的合集（仍存在才生效；小节优先记忆值，缺失回第一节）
      const saved = localStorage.getItem(SUB_SEASON_KEY) || "";
      const [sid, sec] = (saved.split(":").map(Number) as number[]);
      const s = d.seasons.find((x) => x.season_id === sid);
      if (s) {
        setSeasonId(s.season_id);
        setSectionId(s.sections.some((x) => x.id === sec)
          ? sec : s.sections[0]?.id || 0);
      }
    } catch (err: any) {
      setTgtErr(err.message || "账号数据加载失败");
    }
  }, []);

  useEffect(() => {
    if (!e || e.isFullRec || e.etype !== "clip") return;
    // 默认封面预览（本机切片中点抽帧；仅云端时 404 → 引导上传自定义）
    fetch(`${BASE}api/rooms/${p.ROOM}/clips/${e.id}/cover-preview`)
      .then((r) => (r.ok ? r.blob() : Promise.reject(r.status)))
      .then((b) => setCover(URL.createObjectURL(b)))
      .then(() => setCoverAvail(true))
      .catch(() => setCoverAvail(false));
    loadTargets();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [e?.id]);

  // 选中目标稿件 → 拉现有分P（命名查重确认）
  useEffect(() => {
    if (mode !== "part" || !arcAid) { setParts(null); return; }
    let dead = false;
    api<{ parts: ArcPart[] }>(`/api/submit/arcs/${arcAid}/parts`)
      .then((d) => { if (!dead) setParts(d.parts); })
      .catch(() => { if (!dead) setParts(null); });
    return () => { dead = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, arcAid]);

  function pickCover(ev: React.ChangeEvent<HTMLInputElement>) {
    const f = ev.target.files?.[0];
    if (!f) return;
    const rd = new FileReader();
    rd.onload = () => {
      setCover(String(rd.result));
      setCoverAvail(true);
    };
    rd.readAsDataURL(f);
  }

  function pickSeason(sid: number) {
    setSeasonId(sid);
    const s = (targets?.seasons || []).find((x) => x.season_id === sid);
    setSectionId(s?.sections[0]?.id || 0);
  }

  async function submit() {
    if (!e || busy) return;
    if (mode === "part" && !arcAid) { toast("请先选择目标稿件", "err"); return; }
    if (mode === "part" && !partTitle.trim()) { toast("分P名不能为空", "err"); return; }
    setBusy(true);
    try {
      const body = mode === "part"
        ? { mode: "part", target_vid: String(arcAid), part_title: partTitle.trim() }
        : {
            mode: "new", title: title.trim(), desc, tags, tid,
            source_type: sourceType,
            source_url: sourceUrl.trim(),
            cover_b64: cover.startsWith("data:") ? cover : "",
            season_id: seasonId || null,
            section_id: seasonId ? (sectionId || null) : null,
          };
      const r = await postJson<{ job: string }>(
        `/api/rooms/${p.ROOM}/clips/${e.id}/submit`, body);
      if (mode === "new" && seasonId)
        localStorage.setItem(SUB_SEASON_KEY, `${seasonId}:${sectionId}`);
      p.onJob(e.id, { id: r.job, status: "uploading", bv: "", error: "", mode });
      toast(mode === "part"
        ? "已加入投稿队列（本地投稿器在线将自动接管上传；否则服务器兜底串行上传）"
        : "投稿任务已提交（上传中，3Mbps 上行大文件需数分钟）", "busy");
      p.onClose();
    } catch (err: any) {
      toast(err.message, "err");
      setBusy(false);
    }
  }

  if (!e) return null;
  const sizeMB = (e.fileSize || 0) / 1e6;
  const arcs = (targets?.arcs || []).filter((a) => {
    const q = arcFilter.trim();
    return !q || a.title.includes(q) || a.bvid.includes(q);
  });
  const curSeason = (targets?.seasons || []).find((s) => s.season_id === seasonId) || null;
  const dupPart = !!parts?.some((x) => x.title === partTitle.trim());

  return (
    <aside className={"rp-drawer sub-drawer" + (open ? " open" : "")} data-clip={e.id}>
      <div className="dw-head">
        <div className="tt">
          <div className="dw-cap">投稿到 B 站</div>
          <div className="dw-sub">
            {entryName(e)} · {sizeMB.toFixed(1)} MB · 预计{" "}
            {Math.max(1, Math.round(sizeMB / 22 / 0.375))} 分钟
            {targets ? ` · 账号 ${targets.account.uname}` : ""}
          </div>
        </div>
        <button className="btn sm" onClick={p.onClose}>收起 ›</button>
      </div>
      <div className="dw-body">
        {tgtErr && (
          <div className="sub-warn">
            账号数据加载失败：{tgtErr}
            <button className="btn sm" onClick={() => void loadTargets()}>重试</button>
          </div>
        )}

        {/* 模式切换：新视频（完整表单）/ 追加分P（只管目标与分P名） */}
        <div className="sub-mode-tabs" role="tablist">
          <button className={mode === "new" ? "on" : ""}
                  onClick={() => setMode("new")}>新视频投稿</button>
          <button className={mode === "part" ? "on" : ""}
                  onClick={() => setMode("part")}>追加为分P</button>
        </div>

        {mode === "new" ? (
          <>
            <div className="sub-cover">
              {cover
                ? <img src={cover} alt="封面预览" />
                : coverAvail === false ? "本机已释放，可上传自定义封面" : "封面生成中…"}
              <div className="cover-ops">
                <button className="btn sm" onClick={() => fileRef.current?.click()}>换图</button>
              </div>
              <input ref={fileRef} type="file" accept="image/*" style={{ display: "none" }}
                     onChange={pickCover} />
            </div>
            <div className="sub-form">
              <input type="text" placeholder="标题（空=切片名）" maxLength={80}
                     value={title} onChange={(ev) => setTitle(ev.target.value)} />
              <textarea rows={2} placeholder="简介" maxLength={2000}
                        style={{ resize: "vertical" }}
                        value={desc} onChange={(ev) => setDesc(ev.target.value)} />
              <div className="sub-row2">
                <select value={tid} onChange={(ev) => setTid(+ev.target.value)}>
                  {TID_OPTIONS.map(([v, label]) => (
                    <option key={v} value={v}>{label}</option>
                  ))}
                </select>
                <select value={sourceType}
                        onChange={(ev) => setSourceType(ev.target.value as any)}>
                  <option value="original">自制</option>
                  <option value="reprint">转载</option>
                </select>
              </div>
              <input type="text" placeholder="标签（逗号分隔，最多 10 个）"
                     value={tags} onChange={(ev) => setTags(ev.target.value)} />
              {sourceType === "reprint" && (
                <input type="text" placeholder="转载来源（必填）"
                       value={sourceUrl} onChange={(ev) => setSourceUrl(ev.target.value)} />
              )}
              <div className="sub-season">
                <span className="lab">加入合集</span>
                <select value={seasonId} onChange={(ev) => pickSeason(+ev.target.value)}>
                  <option value={0}>不加入</option>
                  {(targets?.seasons || []).map((s) => (
                    <option key={s.season_id} value={s.season_id}>
                      {s.title}（{s.ep_total} 集）
                    </option>
                  ))}
                </select>
                {curSeason && curSeason.sections.length > 1 && (
                  <select value={sectionId}
                          onChange={(ev) => setSectionId(+ev.target.value)}>
                    {curSeason.sections.map((x) => (
                      <option key={x.id} value={x.id}>
                        {x.title}（{x.ep_count}）
                      </option>
                    ))}
                  </select>
                )}
                {targets && targets.seasons.length === 0 && (
                  <span className="sub-hint">账号暂无合集（在 B 站创作中心创建后此处可选择）</span>
                )}
              </div>
            </div>
          </>
        ) : (
          <div className="sub-form">
            <div className="sub-season">
              <span className="lab">目标稿件</span>
              <input type="text" placeholder="搜索标题 / BV 号"
                     value={arcFilter} onChange={(ev) => setArcFilter(ev.target.value)} />
              <select value={arcAid} onChange={(ev) => setArcAid(+ev.target.value)}>
                <option value={0}>选择要追加分P的稿件…</option>
                {arcs.map((a) => (
                  <option key={a.aid} value={a.aid}>
                    {a.title} · {a.bvid}
                  </option>
                ))}
              </select>
              {targets && targets.arcs.length === 0 && (
                <span className="sub-hint">账号还没有投稿稿件</span>
              )}
            </div>
            {arcAid > 0 && (
              <div className="sub-parts">
                <div className="cap">
                  现有分P {parts === null ? "加载中…" : `（${parts.length} 个）`}
                </div>
                {parts?.map((pt) => (
                  <div className={"pt-row" + (pt.title === partTitle.trim() ? " dup" : "")}
                       key={pt.cid}>
                    <span className="idx">#{pt.index}</span>
                    <span className="t">{pt.title}</span>
                  </div>
                ))}
                {dupPart && (
                  <div className="sub-warn">已有同名分P，继续提交将出现重复分P</div>
                )}
              </div>
            )}
            <div className="sub-part-name">
              <span className="lab">分P名</span>
              <input type="text" maxLength={80} value={partTitle}
                     placeholder="分P名（默认切片名）"
                     onChange={(ev) => setPartTitle(ev.target.value)} />
              <span className="sub-hint">= 上传文件名；切片名改动后此处同步修改即可</span>
            </div>
          </div>
        )}

        <div className="ops">
          <button className="btn sm primary" disabled={busy} onClick={submit}>
            {busy ? "提交中…" : mode === "part" ? "追加分P" : "投稿"}
          </button>
          <button className="btn sm" onClick={p.onClose}>取消</button>
        </div>
        <div className="note-line">
          {mode === "part"
            ? "分P标题取上传文件名（后端已按此命名）；原稿标题/简介/封面保持不变。"
            : "弹幕不随投稿（B 站不吃外挂字幕）；投稿账号由管理员配置。"}
        </div>
        <div className="sub-tip">
          <span className="cap">使用提示</span>
          提交后由本机投稿器接管上传（家庭宽带快路径）；投稿器未运行时，
          25 秒后服务器兜底上传（3Mbps，大文件约 10 分钟/个）。本机启动
          方式：双击项目根目录 submit_worker.bat（首次填一次管理员账号，
          之后自动记忆）。
        </div>
      </div>
    </aside>
  );
}

// ---------------------------------------------------------------------------
// 打点切片抽屉
// ---------------------------------------------------------------------------
function ClipScrim(p: { open: boolean; onClose: () => void }) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => e.key === "Escape" && p.onClose();
    window.addEventListener("keydown", h);
    return () => window.removeEventListener("keydown", h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  return <div className={"rp-scrim" + (p.open ? " open" : "")} onClick={p.onClose} />;
}

function ClipDrawer(p: {
  gkey: string;
  open: boolean;
  onClose: () => void;
  rec: RecRow | null;
  entry: Entry | null;
  ROOM: number;
  ME: string;
  CAN_WRITE: boolean;
  marks: Record<number, { inV: number | null; outV: number | null }>;
  setMarks: React.Dispatch<React.SetStateAction<Record<number, { inV: number | null; outV: number | null }>>>;
  nameTouched: React.RefObject<Record<number, boolean>>;
  nameDraft: React.RefObject<Record<number, string>>;
  refresh: () => void;
}) {
  // 抽屉目标是「当前选中 = 主录播」的组
  const fullEntry = p.entry && p.entry.isFullRec ? p.entry : null;
  const recId = fullEntry?.id ?? 0;
  const m = p.marks[recId] || { inV: null, outV: null };
  const [inTxt, setInTxt] = useState("");
  const [outTxt, setOutTxt] = useState("");
  const [name, setName] = useState("");

  // ---- song-detector 联动（本地识别）：下载 low → 本地识别 → 加载结果 ----
  // 段落来自用户本机 song-detector 导出的 .analysis.json（纯前端解析，不上传）
  interface SdSeg { start: number; end: number; start_ts: string; end_ts: string; duration_ts: string }
  const [sd, setSd] = useState<{ recId: number; segs: SdSeg[]; warn: string } | null>(null);
  const sdFileRef = useRef<HTMLInputElement | null>(null);

  async function downloadLowForDetect() {
    if (!fullEntry) return;
    if (!fullEntry.onOss || !fullEntry.ossLowKey) {
      toast("该录播暂无云端 low 文件（未上云或转码未完成）", "err");
      return;
    }
    const date = (fullEntry.createdAt || "").slice(0, 10) || "未知日期";
    const fname = `${date} ${fullEntry.title || "full_" + fullEntry.id} low.mp4`;
    try {
      const url = await getOssDownloadUrl(fullEntry.ossLowKey, fname);
      triggerDownload(url, fname);
      toast("开始下载 low 版，完成后用本地 song-detector 识别", "busy");
    } catch (e: any) {
      toast(e.message || "下载失败", "err");
    }
  }

  async function loadSdFile(f: File) {
    let data: any;
    try {
      data = JSON.parse(await f.text());
    } catch {
      toast("文件不是合法的 JSON", "err");
      return;
    }
    const raw = data?.segments?.singing;
    if (!Array.isArray(raw)) {
      toast("不是 song-detector 识别结果（缺少 segments.singing）", "err");
      return;
    }
    const segs: SdSeg[] = raw
      .map((s: any) => ({
        start: Number(s.start) || 0,
        end: Number(s.end) || 0,
        start_ts: String(s.start_ts ?? ""),
        end_ts: String(s.end_ts ?? ""),
        duration_ts: String(s.duration_ts ?? ""),
      }))
      .filter((s: SdSeg) => s.end > s.start);
    // 软校验：识别文件的源时长与当前录播差太多 → 提示可能拿错了场
    let warn = "";
    const srcDur = Number(data?.source?.duration_sec) || 0;
    if (dur && srcDur && Math.abs(srcDur - dur) > 5)
      warn = "⚠ 该文件时长与当前录播不一致，请确认是同一场";
    setSd({ recId, segs, warn });
  }

  // 点击起/止时间 → 播放器跳到该时间点（与打点同源的本组 <video>）
  function sdSeek(t: number) {
    const vid = document.querySelector<HTMLVideoElement>(
      `#gcard-${CSS.escape(p.gkey)} video`);
    if (!vid) {
      toast("播放器尚未加载当前录播，请先在组卡中点开本段视频", "err");
      return;
    }
    vid.currentTime = Math.max(0, Math.min(t, (vid.duration || t) - 0.2));
    vid.play().catch(() => { /* 自动播放被拦时用户手动点播放即可 */ });
  }

  // 同一录播重开不重置（打点/名字草稿常驻，重开回填）
  useEffect(() => {
    if (!p.open || !fullEntry) return;
    setInTxt(m.inV != null ? fmtDur(m.inV) : "");
    setOutTxt(m.outV != null ? fmtDur(m.outV) : "");
    if (!p.nameTouched.current![recId])
      setName("");
    else
      setName(p.nameDraft.current![recId] || "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.open, recId]);

  const dur = fullEntry?.durationSec || 0;

  function setMark(which: "in" | "out") {
    // 本组播放器（组卡内当前挂载的 <video>；打点对象=主录播选中态）
    const vid = document.querySelector<HTMLVideoElement>(
      `#gcard-${CSS.escape(p.gkey)} video`);
    const cur = vid ? vid.currentTime || 0 : 0;
    const d = dur || cur;
    const next = { ...m };
    if (which === "in") {
      next.inV = Math.min(cur, Math.max(0, d - 10));
      if (next.outV != null && next.outV - next.inV < 10)
        next.outV = Math.min(d, next.inV + 10);
    } else {
      next.outV = Math.max(cur, 10);
      if (next.inV != null && next.outV - next.inV < 10)
        next.inV = Math.max(0, next.outV - 10);
    }
    p.setMarks((mm) => ({ ...mm, [recId]: next }));
    setInTxt(next.inV != null ? fmtDur(next.inV) : "");
    setOutTxt(next.outV != null ? fmtDur(next.outV) : "");
  }

  function editMarks() {
    const a = parseT(inTxt);
    const b = parseT(outTxt);
    const next = { ...m };
    if (a !== null) next.inV = a;
    if (b !== null) next.outV = b;
    p.setMarks((mm) => ({ ...mm, [recId]: next }));
    if (next.inV != null && next.outV != null &&
        (next.inV >= next.outV || next.outV > dur + 0.5 || next.outV - next.inV < 10))
      toast("区间不合法：需起点 < 终点、段长 ≥ 10 秒且不超成片时长");
  }

  async function cut() {
    if (!p.CAN_WRITE) return;
    const mm = p.marks[recId] || {};
    if (mm.inV == null || mm.outV == null) {
      toast("请先用「设为起点 / 设为终点」框选一段（或直接输入时间）");
      return;
    }
    if (!p.ME) {
      toast("当前登录未绑定昵称：退出后重新登录（登录时填写昵称）即可切片");
      return;
    }
    try {
      await postJson(`/api/rooms/${p.ROOM}/recordings/${recId}/clips`, {
        start_sec: Math.round(mm.inV * 10) / 10,
        end_sec: Math.round(mm.outV * 10) / 10,
        title: name.trim(),
      });
      toast("已提交切片，正在切片…", "busy");
      setTimeout(p.refresh, 400);
      p.onClose();
    } catch (e: any) {
      toast(e.message, "err");
    }
  }

  if (!fullEntry) return null;
  const L = m.inV != null && dur ? (m.inV / dur) * 100 : null;
  const R = m.outV != null && dur ? (m.outV / dur) * 100 : null;

  return (
    <aside className={"rp-drawer" + (p.open ? " open" : "")} data-rec={recId}>
      <div className="dw-head">
        <div className="tt">
          <div className="dw-cap">打点切片</div>
          <div className="dw-sub">
            本场录播 {(fullEntry.createdAt || "").slice(11, 16)}
            {fullEntry.title ? " · " + fullEntry.title : ""}
          </div>
        </div>
        <button className="btn sm" onClick={p.onClose}>收起 ‹</button>
      </div>
      <div className="dw-body">
        <div className="af-clipbar">
          <div className="cap">
            打点切片 · 当前 <span className="now mono-num">
              {(() => {
                const v = document.querySelector<HTMLVideoElement>(
                  `#gcard-${CSS.escape(p.gkey)} video`);
                return v ? fmtDur(v.currentTime) : "0:00";
              })()}
            </span>
          </div>
          <div className="marks">
            <div className="mk-row">
              <button className={"mk-btn" + (m.inV != null ? " set" : "")}
                      onClick={() => setMark("in")}>
                <span className="dot" />设为起点
              </button>
              <input className={"mk-time" + (m.inV != null ? " set" : "")}
                     value={inTxt} placeholder="mm:ss" spellCheck={false}
                     aria-label="起点时间"
                     onChange={(e) => setInTxt(e.target.value)}
                     onBlur={editMarks} />
            </div>
            <div className="mk-row">
              <button className={"mk-btn out" + (m.outV != null ? " set" : "")}
                      onClick={() => setMark("out")}>
                <span className="dot" />设为终点
              </button>
              <input className={"mk-time" + (m.outV != null ? " set" : "")}
                     value={outTxt} placeholder="mm:ss" spellCheck={false}
                     aria-label="终点时间"
                     onChange={(e) => setOutTxt(e.target.value)}
                     onBlur={editMarks} />
              <span className="len">
                {m.inV != null && m.outV != null
                  ? "段长 " + fmtDur(m.outV - m.inV)
                  : m.inV != null || m.outV != null ? "再打另一端标记" : ""}
              </span>
            </div>
          </div>
          <div className="af-range">
            <span className="rail" />
            {L != null && R != null && (
              <>
                <span className="sel" style={{ left: L + "%", width: R - L + "%" }} />
                <span className="pin l" style={{ left: L + "%" }}>
                  <i>{fmtShort(m.inV!)}</i>
                </span>
                <span className="pin r" style={{ left: R + "%" }}>
                  <i className={R - L < 30 ? "flip" : ""}>{fmtShort(m.outV!)}</i>
                </span>
              </>
            )}
            <span className="dur">全程 {fmtDur(dur)}</span>
          </div>
          <div className="af-submit">
            <input type="text" maxLength={60}
                   placeholder={
                     !p.nameTouched.current![recId] && m.inV != null && m.outV != null
                       ? "默认名：" + fmtShort(m.inV) + " – " + fmtShort(m.outV)
                       : "切片名字（默认按起止时间命名）"}
                   value={name}
                   onChange={(e) => {
                     setName(e.target.value);
                     p.nameTouched.current![recId] = true;
                     p.nameDraft.current![recId] = e.target.value;
                   }} />
            <button className="btn sm primary" onClick={cut}>切片</button>
          </div>
          <div className="caveat">快切按视频关键帧对齐，实际起止点可能有 ±2 秒左右偏差</div>

          {/* ---- song-detector 联动：分隔线 + 下载/加载双按钮，加载后只列段落 ---- */}
          {sd && sd.recId === recId ? (
            <div className="sd-area">
              <div className="sd-cap">
                本地识别 · 唱歌片段 {sd.segs.length} 段
                {sd.warn ? <span className="sd-warn">{sd.warn}</span> : null}
              </div>
              {sd.segs.length === 0 ? (
                <div className="sd-hint">该文件未检出唱歌片段</div>
              ) : (
                <div className="sd-list">
                  {sd.segs.map((s, i) => (
                    <div className="sd-row" key={i}>
                      <span className="idx">{i + 1}</span>
                      <button className="ts" title="跳到该段起点"
                              onClick={() => sdSeek(s.start)}>{s.start_ts}</button>
                      <span className="dash">–</span>
                      <button className="ts out" title="跳到该段终点"
                              onClick={() => sdSeek(s.end)}>{s.end_ts}</button>
                      <span className="len">{s.duration_ts}</span>
                    </div>
                  ))}
                </div>
              )}
              <div className="sd-hint">点击起/止时间跳转播放器到对应时间点</div>
            </div>
          ) : (
            <div className="sd-area">
              <div className="sd-divider" />
              <div className="sd-btns">
                <button className="btn sm" onClick={downloadLowForDetect}>
                  下载low以进行识别</button>
                <button className="btn sm" onClick={() => sdFileRef.current?.click()}>
                  加载本地识别文件</button>
              </div>
              <div className="sd-hint">
                配合本地 song-detector：先下载 low 版 → 本地识别（结果默认导出在其
                results/ 目录，文件名 = 录播日期 + 录播名）→ 回到这里加载 .analysis.json
              </div>
              <input ref={sdFileRef} type="file" accept=".json,application/json" hidden
                     onChange={(e) => {
                       const f = e.target.files?.[0];
                       if (f) loadSdFile(f);
                       e.target.value = "";
                     }} />
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
