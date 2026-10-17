// 直播间详情 / 历史场次详情（同视图双模式）。
// 行为对齐：自适应轮询 20/(1+新增弹幕/10)s 夹 1~20s + document.hidden 暂停；
// 弹幕增量游标 dm_since_id、分批/全量加载（上限 15000）、滚动增量渲染 + 头插锚定
// （v2.3.2）、搜索过滤、日期分组；
// 4 组 ECharts（粉丝量/增长 connect 联动、同接曲线、观众构成）；
// 场次模式数据只读单次加载 + 收益/人气双轴曲线板块（v2.2 替代原高能时刻）。
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router";
import { api, BASE } from "../api";
import {
  toast, fmtNum, avatarGradient, cmpLabel, medalHtml,
  guardBadge, scColor, dmContentHtml,
} from "../ui";
import { useChart, TOOLTIP_THEME } from "../chart";
import type { EChartsOption } from "../echarts";

interface DmRow {
  id: number; uname: string; text: string; emots: string;
  medal_name: string; medal_level: number; medal_color: number;
  privilege_type: number; ts: string;
}
interface GiftRow {
  ts: string; uname: string; type: string; name: string;
  blind_box: string; count: number; value_yuan: number; orders?: number; cnt?: number;
}
interface SessionRow {
  session_id: number; seq: number; titles: string; start_time: string;
  end_time: string | null; hours: number | null; peak_viewers: number;
  danmaku_count: number; gift_value: number;
}
interface Dashboard {
  session_mode: boolean;
  room: { room_id: number; anchor_name: string; avatar_url: string };
  live_now: boolean;
  live: { seq: number; start_time: string };
  session_info?: { seq: number; titles: string; start_time: string; end_time: string | null };
  attention: number; guard_users: number; guards_real: number | null;
  sessions: SessionRow[];
  fan_daily: { day: string; total: number; growth: number }[];
  viewer_curve: { ts: string; viewers: number }[];
  active_curve: { minute: string; cnt: number }[];
  audience: { label: string; all_viewers: number; active_users: number; paying_users: number;
              avg_viewers: number; viewer_samples: number };
  today: { gift_value: number; danmaku: number; gift_rows: number; peak_viewers: number };
  yesterday: { gift_value: number; danmaku: number; gift_rows: number; peak_viewers: number };
  recent_danmaku: DmRow[];
  recent_gifts: GiftRow[];
  danmaku_total: number;
  dm_last_id: number;
  top_users: {
    uname: string; uid: number; gift_value: number; danmaku_count: number;
    tier: number; guard_level: number; medal_name: string;
    medal_level: number; medal_color: number;
  }[];
  sc_list: { ts: string; uname: string; value_yuan: number; text: string }[];
  sc_total: { v: number; c: number };
  gift_agg: {
    img: string; name: string; type: string; blind_box: string;
    orders?: number; cnt?: number; value: number;
  }[];
  gift_total: { v: number; c: number };
}
interface CurvesData {
  session_id: number;
  total_rev: number;
  peak_pop: number;
  points: { t: string; rev: number | null; pop: number | null }[];
}

function uptimeSince(startTs: string): string {
  const ms = Date.now() - new Date(startTs.replace("T", " ") + "+08:00").getTime();
  if (isNaN(ms) || ms < 0) return "";
  const h = Math.floor(ms / 3.6e6), m = Math.floor((ms % 3.6e6) / 6e4),
        s = Math.floor((ms % 6e4) / 1e3);
  const p = (n: number) => String(n).padStart(2, "0");
  return `开播 ${p(h)}:${p(m)}:${p(s)}`;
}

// 弹幕日期分组头文案：YYYY-MM-DD + 周几 + 今天/昨天标记。
// ts 为服务器本地时间的 "YYYY-MM-DDTHH:MM:SS" 串；拆段手工构造 Date 取
// 周几——直接 new Date("YYYY-MM-DD") 会按 UTC 解析，非零时区下漂一天
function dmDateLabel(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  const wd = "日一二三四五六"[new Date(y, m - 1, d).getDay()];
  const now = new Date();
  const isSame = (t: Date) =>
    y === t.getFullYear() && m === t.getMonth() + 1 && d === t.getDate();
  const tag = isSame(now)
    ? " · 今天"
    : isSame(new Date(now.getFullYear(), now.getMonth(), now.getDate() - 1))
      ? " · 昨天" : "";
  return `${day} 周${wd}${tag}`;
}

export default function Room() {
  const { roomId, sessionId } = useParams();
  const ROOM = Number(roomId);
  const SESSION_ID = sessionId ? Number(sessionId) : null;

  const [d, setD] = useState<Dashboard | null>(null);
  const [kw, setKw] = useState("");
  const [sortKey, setSortKey] = useState<"gift_value" | "danmaku_count">("gift_value");
  const [curves, setCurves] = useState<CurvesData | null>(null);

  // 弹幕分批状态（v0.8 口径）
  const [allDanmaku, setAllDanmaku] = useState<DmRow[]>([]);
  const dmLastId = useRef(0);
  const dmTotal = useRef(0);
  const [dmMeta, setDmMeta] = useState({ loaded: 0, total: 0 });
  const lastDmTs = useRef("");
  const fetching = useRef(false);
  const fetchingAll = useRef(false);
  const dmRef = useRef<DmRow[]>([]);
  dmRef.current = allDanmaku;

  // 渲染上限随滚动扩展（v2.3.2）：数据可全量进 state（加载更多/搜索全量），
  // 但 DOM 只渲染排序后前 renderCap 行、滚动接近底部再追加一批——修掉
  // 固定 slice(0,800) 截断导致的"加载更多后更早弹幕永远不可见"，同时
  // 保住万级行数的 DOM 性能（首屏 800 与后端 DM_FIRST_LIMIT 对齐）
  const [renderCap, setRenderCap] = useState(800);
  const streamRef = useRef<HTMLDivElement>(null);
  // 轮询头插锚定（v2.3.2）：增量弹幕前置到列表顶部前记录内容总高，DOM
  // 提交后把 scrollTop 补上高度差——用户正在看的行不随新弹幕涌入而移动
  const anchorH = useRef(0);

  // ---- 轮询循环（场次模式只读单次；直播模式自适应 + hidden 暂停）----
  const load = useCallback(async (): Promise<number> => {
    const params = new URLSearchParams();
    if (SESSION_ID) params.set("session_id", String(SESSION_ID));
    // 增量与否以"发送时"为准：并发双载（dev StrictMode 会双触发首载）时，
    // 第二个响应回来 dmLastId 已置位，若在 updater 里才判断会把整批首载
    // 误当增量做 reverse 前插，去重丢弃后数组残留正序——游标取尾端就会
    // 取到最新 id，翻页拉回与已有行重叠的页
    const incremental = !!dmLastId.current;
    if (incremental) params.set("dm_since_id", String(dmLastId.current));
    const qs = params.toString();
    const data = await api<Dashboard>(
      `/api/rooms/${ROOM}/dashboard${qs ? "?" + qs : ""}`);
    // 头插前记录内容高度（滚动锚定用，见 useLayoutEffect）；非增量首载
    // 整批替换无需锚定
    if (incremental) anchorH.current = streamRef.current?.scrollHeight ?? 0;
    setD(data);
    setAllDanmaku((prev) => {
      if (incremental) {
        // 增量前置：按 id 去重——dev 环境 StrictMode 会并发触发两次
        // 首载（第二次时 dmLastId 已置位，会把同一批 recent 整体再前置
        // 一遍，产生重复行与重复 key）
        const seen = new Set(prev.map((x) => x.id));
        const fresh = data.recent_danmaku.slice().reverse()
          .filter((x) => !seen.has(x.id));
        return [...fresh, ...prev];
      }
      return data.recent_danmaku;
    });
    dmLastId.current = data.dm_last_id;
    dmTotal.current = data.danmaku_total;
    setDmMeta({ loaded: dmRef.current.length + (incremental ? data.recent_danmaku.length : 0),
                total: data.danmaku_total });
    let newCount = 0;
    if (lastDmTs.current) {
      newCount = data.recent_danmaku.filter((x) => x.ts > lastDmTs.current).length;
    }
    if (data.recent_danmaku.length) lastDmTs.current = data.recent_danmaku[0].ts;
    return newCount;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ROOM, SESSION_ID]);

  useEffect(() => {
    if (SESSION_ID) {
      load().catch((e) => toast("加载失败: " + e.message));
      return;
    }
    let stop = false;
    (async function loop() {
      while (!stop) {
        let newCount = 0;
        if (!document.hidden) {
          try {
            newCount = (await load()) || 0;
          } catch { /* 网络抖动下轮重试 */ }
        }
        const sec = Math.max(1, Math.min(20, Math.round(20 / (1 + newCount / 10))));
        await new Promise((r) => setTimeout(r, sec * 1000));
      }
    })();
    return () => {
      stop = true;
    };
  }, [load, SESSION_ID]);

  // ---- 收益/人气曲线（v2.2：原高能板块位置，仅场次页）----
  useEffect(() => {
    if (!SESSION_ID) return;
    api<CurvesData>(`/api/rooms/${ROOM}/sessions/${SESSION_ID}/curves`)
      .then(setCurves)
      .catch(() => setCurves(null));
  }, [ROOM, SESSION_ID]);

  // ---- 弹幕翻页 / 全量 ----
  const dmCap = () => Math.min(dmTotal.current || dmMeta.total, 15000);
  const allLoaded = allDanmaku.length >= (dmTotal.current || dmMeta.total) ||
                    allDanmaku.length >= 15000;

  // 当前已加载弹幕的最小 id（翻页游标）。扫描而非取尾元素：不依赖数组
  // 排序方向，防御任何路径把顺序搞乱后游标取错端
  function oldestDmId(): number {
    let m = 0;
    for (const x of dmRef.current) if (!m || x.id < m) m = x.id;
    return m;
  }

  // beforeId：翻页游标。ensureAll 循环内必须显式传入（本地推进）；
  // 缺省从 dmRef.current 推导——不能读闭包里的 allDanmaku，异步循环跨
  // 渲染重入时它仍是旧快照，游标不动会把同一页反复抓回来（v2.2.1
  // 弹幕重复根因）
  async function fetchDmPage(limit: number, beforeId?: number) {
    const params = new URLSearchParams({ limit: String(limit) });
    if (SESSION_ID) params.set("session_id", String(SESSION_ID));
    const oldest = beforeId ?? oldestDmId();
    if (oldest) params.set("before_id", String(oldest));
    const r = await api<{ rows: DmRow[]; total: number }>(
      `/api/rooms/${ROOM}/danmaku?${params}`);
    dmTotal.current = r.total;
    return r;
  }

  // 追加一批翻页结果（按 id 去重）：重复 id 会让弹幕行的 key={x.id} 冲突，
  // React 对重复 key 的协调行为未定义——直播轮询不断重排列表时 DOM 节点
  // 泄漏累积，搜索过滤后列表仍残留未筛选旧行（v2.2.1 症状）
  function appendDmPage(rows: DmRow[]) {
    setAllDanmaku((prev) => {
      const seen = new Set(prev.map((x) => x.id));
      return [...prev, ...rows.filter((x) => !seen.has(x.id))];
    });
  }

  async function fetchMore() {
    if (allLoaded || fetching.current) return;
    fetching.current = true;
    try {
      const r = await fetchDmPage(2000);
      appendDmPage(r.rows);
      setDmMeta({ loaded: allDanmaku.length + r.rows.length, total: r.total });
    } catch (e: any) {
      toast("弹幕加载失败: " + e.message);
    }
    fetching.current = false;
  }

  async function ensureAll() {
    if (allLoaded || fetchingAll.current) return;
    // 首载未完成（dm_last_id 未置位）时翻页游标无意义，且首载的整批
    // 替换会清掉并发追加的翻页结果——直接返回，用户再聚焦即可
    if (!dmLastId.current) return;
    fetchingAll.current = true;
    try {
      // 游标与计数本地推进：不依赖渲染时机把 dmRef 刷新到位（await 之后
      // React 未必已重渲染）。轮询只往头部前置新行，已加载的最小 id 不受
      // 影响，游标安全
      let loaded = dmRef.current.length;
      let cursor = oldestDmId();
      for (let i = 0; i < 100; i++) {
        if (loaded >= dmTotal.current || loaded >= 15000) break;
        const need = Math.min(dmTotal.current, 15000) - loaded;
        const r = await fetchDmPage(Math.min(6000, need), cursor);
        if (!r.rows.length) break;
        appendDmPage(r.rows);
        loaded += r.rows.length;
        cursor = r.rows[r.rows.length - 1].id;
        setDmMeta({ loaded, total: r.total });
      }
    } catch (e: any) {
      toast("弹幕全量加载失败: " + e.message);
    }
    fetchingAll.current = false;
  }

  // 滚动锚定：DOM 提交后若本次更新是头插（内容高度增长且用户不在顶部），
  // 把 scrollTop 补上高度差，视口停留的行保持不动；追加式增长（加载更多
  // 的旧行在列表尾）天然无需补偿。非增量轮询 anchorH 恒为 0，本效应空转
  useLayoutEffect(() => {
    const el = streamRef.current;
    if (el && anchorH.current) {
      const delta = el.scrollHeight - anchorH.current;
      if (delta > 0 && el.scrollTop > 0) el.scrollTop += delta;
      anchorH.current = 0;
    }
  }, [allDanmaku, d?.recent_gifts]);

  // ---- 图表 ----
  const fanDays = useMemo(
    () => (d?.fan_daily || []).map((p) => p.day), [d]);
  const darkAxis = { axisLabel: { color: "#8b96a8" } };
  const fanTotalRef = useChart(d ? {
    title: { text: "粉丝量", left: 0, top: 0, textStyle: { fontSize: 11, color: "#8b96a8" } },
    grid: { left: 52, right: 12, top: 22, bottom: 6 },
    tooltip: { ...TOOLTIP_THEME, trigger: "axis" },
    xAxis: { type: "category", data: fanDays, show: false },
    yAxis: { type: "value", scale: true, ...darkAxis },
    series: [{ type: "line", data: d.fan_daily.map((p) => p.total), smooth: true,
               symbol: "none", lineStyle: { color: "#4fa8e8", width: 2 },
               areaStyle: { color: "rgba(79,168,232,.10)" } }],
  } as EChartsOption : null);
  const fanGrowRef = useChart(d ? {
    title: { text: "每日粉丝增长", left: 0, top: 0, textStyle: { fontSize: 11, color: "#8b96a8" } },
    grid: { left: 52, right: 12, top: 22, bottom: 20 },
    tooltip: { ...TOOLTIP_THEME, trigger: "axis" },
    xAxis: { type: "category", data: fanDays, ...darkAxis },
    yAxis: { type: "value", ...darkAxis },
    series: [{ type: "bar", data: d.fan_daily.map((p) => p.growth), barMaxWidth: 18,
               itemStyle: { color: (p: any) =>
                 (p.value == null || p.value >= 0) ? "#fb7299" : "#5c6779" } }],
  } as EChartsOption : null);
  // echarts.connect 联动（两图共用 canvas 实例组）
  useEffect(() => { /* connect 通过同 group 名实现 */ }, []);
  const popRef = useChart(d ? (() => {
    const actMap = Object.fromEntries((d.active_curve || []).map((a) => [a.minute, a.cnt]));
    return {
      grid: { left: 60, right: 20, top: 34, bottom: 28 },
      legend: { data: ["房间观众", "活跃用户"], top: 5, icon: "circle", itemWidth: 8 },
      tooltip: { ...TOOLTIP_THEME, trigger: "axis", confine: true },
      xAxis: { type: "category", data: d.viewer_curve.map((p) => p.ts.slice(11, 16)), ...darkAxis },
      yAxis: { type: "value", scale: true,
               splitLine: { lineStyle: { color: "rgba(226,234,244,.07)" } }, ...darkAxis },
      series: [
        { name: "房间观众", type: "line", smooth: true, symbol: "none",
          lineStyle: { color: "#4fa8e8", width: 2 },
          areaStyle: { color: "rgba(79,168,232,.10)" },
          data: d.viewer_curve.map((p) => p.viewers) },
        { name: "活跃用户", type: "line", smooth: true, symbol: "none",
          lineStyle: { color: "#f2558c", width: 2 },
          data: d.viewer_curve.map((p) => actMap[p.ts.slice(11, 16)] || 0) },
      ],
    } as EChartsOption;
  })() : null);
  // 观众构成三项（v2.0.0 改版）：平均同接 / 活跃人员 / 核心用户，同一
  // 场次口径。平均同接=该场分钟采样观众数均值（基准项），悬浮在人数项
  // 上时给出「占整场平均同接」的比例。
  // 活跃人员口径说明：人气增长类操作（点赞等）在采集链路不可完整按人
  // 检测（blivedm 不解析 LIKE_INFO_V3_*，观看行为无逐人事件），故维持
  // 弹幕活跃口径=本场发弹幕超过 3 条的去重用户。
  const tierRef = useChart(d ? (() => {
    const aud = d.audience;
    const items = [
      { name: "平均同接", v: aud.avg_viewers, color: "#3d475c",
        tip: "本场分钟采样观众数的均值（基准项）" },
      { name: "活跃人员", v: aud.active_users, color: "#4fa8e8",
        tip: "本场发弹幕超过 3 条或送过礼物（不限金额）的去重用户" },
      { name: "核心用户", v: aud.paying_users, color: "#d9a845",
        tip: "本场付费过的去重用户" },
    ];
    const pct = (n: number) => aud.avg_viewers > 0
      ? ((n / aud.avg_viewers) * 100).toFixed(1) + "%"
      : "—（本场无同接采样）";
    return {
      grid: { left: 70, right: 20, top: 20, bottom: 30 },
      tooltip: { ...TOOLTIP_THEME, trigger: "item", confine: true,
        formatter: (p: any) => {
          const it = items[p.dataIndex];
          const head = `<b>${it.name}</b><br>${it.tip}<br>数值：${fmtNum(it.v)}`;
          if (p.dataIndex === 0)
            return head + `<br>采样：${aud.viewer_samples} 个分钟点`;
          return head + `<br>占整场平均同接：${pct(it.v)}` +
            (aud.avg_viewers > 0
              ? `（平均同接 ${fmtNum(aud.avg_viewers)} 人）` : "");
        } },
      xAxis: { type: "category", data: items.map((x) => x.name),
               axisLabel: { interval: 0, color: "#8b96a8" } },
      yAxis: { type: "value", splitLine: { lineStyle: { color: "rgba(226,234,244,.07)" } }, ...darkAxis },
      series: [{
        type: "bar", barWidth: "42%",
        label: { show: true, position: "top", fontWeight: 600, color: "#e8edf4" },
        itemStyle: { borderRadius: [6, 6, 0, 0] },
        data: items.map((x) => ({ value: x.v, itemStyle: { color: x.color } })),
      }],
    } as EChartsOption;
  })() : null);

  // ---- 弹幕渲染（合并礼物 + 过滤）----
  // 返回 { rows, total }：rows=过滤后的合并流，total=过滤前条数——计数条
  // 需要「匹配数/已加载总数」双值（v2.0 React 重建时丢过总数，只剩 N/N）
  const dmView = useMemo(() => {
    const gifts = (d?.recent_gifts || []).map((g) => ({ ...g, __gift: true })) as any[];
    const dms = allDanmaku.map((x) => ({ ...x, __gift: false })) as any[];
    const all = [...dms, ...gifts];
    all.sort((a, b) => (a.ts < b.ts ? 1 : -1));
    const lq = kw.trim().toLowerCase();
    const rows = lq ? all.filter((x) =>
      (x.uname || "").toLowerCase().includes(lq) ||
      (x.text || "").toLowerCase().includes(lq) ||
      (x.name || "").toLowerCase().includes(lq)) : all;
    return { rows, total: all.length };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [allDanmaku, d?.recent_gifts, kw]);

  const usersSorted = useMemo(
    () => [...(d?.top_users || [])].sort((a, b) => (b[sortKey] || 0) - (a[sortKey] || 0)),
    [d, sortKey]);

  if (!d) return <div className="empty">加载中…</div>;

  const firstTitle = (d.session_info?.titles || "").split("\n").filter(Boolean)[0] || "";
  const isSess = !!d.session_mode && !!d.session_info;
  const st = isSess ? d.session_info!.start_time.match(
    /^(\d+)-(\d+)-(\d+)T(\d+)/) || [] : [];
  const pageTitle = isSess
    ? `${+st[2]}月${+st[3]}日${+st[4]}时直播场次详情`
    : "直播间详情";
  const metricsLabels = isSess
    ? ["本场营收", "本场弹幕", "本场礼物", "本场同接峰值"]
    : ["今日营收", "今日弹幕", "今日礼物", "今日同接峰值"];

  return (
    <>
      <div className="page-head">
        <h2 title={firstTitle}>{pageTitle}</h2>
        <span className="sub">
          {isSess
            ? `${d.room.anchor_name || d.room.room_id} · 第 ${d.session_info!.seq} 场` +
              (firstTitle
                ? ` · ${firstTitle.length > 24 ? firstTitle.slice(0, 24) + "…" : firstTitle}`
                : "") + " · 数据只读"
            : `${d.room.anchor_name || d.room.room_id} · 数据实时刷新`}
        </span>
        <div className="spacer" />
        {sessionId && (
          <Link to={`/room/${ROOM}`} className="btn ghost"
                style={{ textDecoration: "none" }}>← 返回直播间</Link>
        )}
      </div>

      <div className="detail-grid">
        <div>
          <div className="card card-pad anchor-card">
            <div className="head">
              <div className="avatar" style={{ background: avatarGradient(ROOM), cursor: "pointer" }}
                   title="在新标签页打开直播间"
                   onClick={() => window.open(`https://live.bilibili.com/${ROOM}`, "_blank", "noopener")}>
                {d.room.avatar_url && (
                  <img src={d.room.avatar_url} alt=""
                       onError={(e) => (e.target as HTMLImageElement).remove()} />
                )}
                {(d.room.anchor_name || String(d.room.room_id))[0]}
              </div>
              <div>
                <div className="name" style={{ fontWeight: 600, display: "flex", gap: 8, alignItems: "center" }}>
                  {d.room.anchor_name || String(d.room.room_id)}
                  {d.live_now
                    ? <span className="badge live pulse">直播中</span>
                    : <span className="badge off">未开播</span>}
                </div>
                <div className="rid" style={{ fontSize: 11.5, color: "var(--faint)" }}>
                  房间号 {d.room.room_id}
                </div>
              </div>
            </div>
            <div className="uptime">
              {isSess
                ? `${d.session_info!.start_time.slice(5, 16).replace("T", " ")} — ` +
                  (d.session_info!.end_time
                    ? d.session_info!.end_time.slice(5, 16).replace("T", " ")
                    : "直播中")
                : d.live_now ? uptimeSince(d.live.start_time) : "当前未开播"}
            </div>
            <div className="stat3">
              <div className="item">
                <div className="v">{fmtNum(d.attention)}</div>
                <div className="k">粉丝</div>
              </div>
              <div className="item">
                <div className="v">
                  {fmtNum(d.sessions.reduce((a, s) => a + (s.danmaku_count || 0), 0))}
                </div>
                <div className="k">监控期弹幕</div>
              </div>
              <div className="item">
                <div className="v">{d.guard_users}</div>
                <div className="k">
                  大航海{d.guards_real != null ? "" : "(监控期)"}
                </div>
              </div>
            </div>
            <div style={{ marginTop: 8 }}>
              <div ref={fanTotalRef} className="chart-box" style={{ height: 237, marginTop: 8 }} />
              <div ref={fanGrowRef} className="chart-box" style={{ height: 237, marginTop: 4 }} />
            </div>
          </div>
        </div>

        <div>
          <div className="card card-pad">
            <h3>
              {isSess ? "本场数据" : "今日数据"}
              {!isSess && <span className="more">环比昨日</span>}
            </h3>
            <div className="metrics">
              {[
                { v: <>¥{(d.today.gift_value).toLocaleString()}</>, cmp: cmpLabel(d.today.gift_value, d.yesterday.gift_value) },
                { v: fmtNum(d.today.danmaku), cmp: cmpLabel(d.today.danmaku, d.yesterday.danmaku) },
                { v: d.today.gift_rows, cmp: cmpLabel(d.today.gift_rows, d.yesterday.gift_rows) },
                { v: fmtNum(d.today.peak_viewers), cmp: cmpLabel(d.today.peak_viewers, d.yesterday.peak_viewers) },
              ].map((m, i) => (
                <div className="metric" key={i}>
                  <div className="k">{metricsLabels[i]}</div>
                  <div className="v">{m.v}</div>
                  <div className="cmp">{isSess ? "—" : m.cmp}</div>
                </div>
              ))}
            </div>
          </div>
          <div className="card card-pad" style={{ marginTop: 16 }}>
            <h3>
              {isSess
                ? `本场同接曲线（第 ${d.live.seq} 场）`
                : d.live_now
                  ? `当前场次同接曲线（第 ${d.live.seq} 场）`
                  : "未开播（近3小时）"}
            </h3>
            <div ref={popRef} className="chart-box" style={{ height: 300 }} />
            <div style={{ color: "var(--faint)", fontSize: 12, marginTop: 6 }}>
              「房间观众」为实时同接人数，与直播间页面右上角显示一致。
            </div>
          </div>
        </div>
      </div>

      <div className="detail-bottom">
        <div className="card card-pad">
          <h3>
            所有弹幕
            <span className="more">
              {(d.recent_gifts || []).length
                ? `最近礼物：${d.recent_gifts[0].uname} ${d.recent_gifts[0].name}`
                : ""}
            </span>
          </h3>
          <div className="dm-search">
            <input placeholder="搜索用户名或弹幕内容…"
                   value={kw}
                   onChange={(e) => setKw(e.target.value)}
                   onFocus={ensureAll} />
            <span className="cnt">
              {kw
                ? `${dmView.rows.length}/${dmView.total} 条`
                : dmMeta.loaded >= dmCap() && dmMeta.loaded > 0
                  ? `共 ${dmView.rows.length} 条`
                  : `已加载 ${dmMeta.loaded || allDanmaku.length} / ${dmCap()} 条`}
            </span>
          </div>
          <div className="stream" ref={streamRef}
               onScroll={() => {
                 // 滚动接近底部时扩展渲染上限，让已加载的更早弹幕逐批可见
                 const el = streamRef.current;
                 if (el && el.scrollTop + el.clientHeight >= el.scrollHeight - 60) {
                   setRenderCap((c) => c < dmView.rows.length
                     ? Math.min(c + 800, dmView.rows.length) : c);
                 }
               }}>
            {dmView.rows.length === 0 && (
              <div className="empty">{kw ? "没有匹配的记录" : "暂无弹幕记录"}</div>
            )}
            {/* 日期分组（v2.2.1）：行按 ts 倒序，日期切换点插入分组头
                （含该日在当前过滤口径下的条数；只统计实际渲染的行切片，
                头与计数所见即所得）。flatMap 展开保持每行原 key */}
            {(() => {
              const shown = dmView.rows.slice(0, renderCap);
              const dayCount = new Map<string, number>();
              for (const x of shown) {
                const d = String(x.ts).slice(0, 10);
                dayCount.set(d, (dayCount.get(d) || 0) + 1);
              }
              let prevDay = "";
              return shown.flatMap((x: any, i: number) => {
                const day = String(x.ts).slice(0, 10);
                const out = day === prevDay ? [] : [
                  <div className="dm-date" key={"d" + day}>
                    {dmDateLabel(day)} · {dayCount.get(day)} 条
                  </div>,
                ];
                prevDay = day;
                out.push(x.__gift ? (
                  <div className="row dm-gift" key={"g" + i}>
                    <span className="t">{x.ts.slice(11, 19)}</span>
                    <span className="u">{x.uname}</span>
                    <span>
                      <span className="dm-gift-tag">
                        {x.type === "sc" ? "SC" : x.type === "guard" ? "上舰" : "送礼"}
                      </span>
                      {x.name}
                      {x.type !== "guard" && x.count > 1 ? ` ×${x.count}` : ""}
                      {x.value_yuan > 0 ? <span className="price"> ¥{x.value_yuan}</span> : null}
                      {x.blind_box ? <span className="blind-tag" title={`来自${x.blind_box}`}>盲盒</span> : null}
                    </span>
                  </div>
                ) : (
                  <div className="row" key={x.id}>
                    <span className="t">{x.ts.slice(11, 19)}</span>
                    <span className="u" title={x.uname}>{x.uname}</span>
                    {guardBadge(x.privilege_type)}
                    {medalHtml(x.medal_name, x.medal_level, x.medal_color)}
                    <span className="c">{dmContentHtml(x)}</span>
                  </div>
                ));
                return out;
              });
            })()}
          </div>
          {!allLoaded && (
            <div className="dm-more">
              <button onClick={fetchMore}>加载更多弹幕</button>
              <span className="note">
                已加载 {allDanmaku.length} / {dmCap()} 条
              </span>
            </div>
          )}
        </div>
        <div>
          <div className="card card-pad">
            <h3>观众构成（{d.audience.label || "本场"}）</h3>
            <div ref={tierRef} className="chart-box" style={{ height: 240 }} />
          </div>
          <div className="card card-pad" style={{ marginTop: 16 }}>
            <h3>
              用户榜{isSess ? "（本场）" : ""}
              <span className="more">共 {usersSorted.length} 人</span>
            </h3>
            <div className="stream" style={{ height: 500, padding: 0 }}>
              <table className="plain">
                <thead>
                  <tr>
                    <th style={{ width: 204 }}>用户</th>
                    <th className="sortable" style={{ width: 88 }}
                        onClick={() => setSortKey("gift_value")}>
                      礼物 {sortKey === "gift_value" ? "↓" : ""}
                    </th>
                    <th className="sortable" style={{ width: 88 }}
                        onClick={() => setSortKey("danmaku_count")}>
                      弹幕 {sortKey === "danmaku_count" ? "↓" : ""}
                    </th>
                    <th style={{ width: 92 }}>分层</th>
                  </tr>
                </thead>
                <tbody>
                  {usersSorted.length === 0 && (
                    <tr><td colSpan={4} className="empty">暂无数据</td></tr>
                  )}
                  {usersSorted.map((u, i) => (
                    <tr key={i}>
                      <td className="u-cell" title={u.uname || String(u.uid)}>
                        {u.uname || String(u.uid)}
                        {guardBadge(u.guard_level)}
                        {medalHtml(u.medal_name, u.medal_level, u.medal_color)}
                      </td>
                      <td className="price">¥{u.gift_value}</td>
                      <td>{u.danmaku_count}</td>
                      <td><span className={"tier-pill tier-" + u.tier}>{u.tier}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>

      <div className="detail-extras">
        <div className="card card-pad">
          <h3>SC 列表 <span className="more">{d.audience.label || "本场"}</span></h3>
          <div className="stream sc-stream">
            {d.sc_list.length === 0 && <div className="empty">本场暂无 SC</div>}
            {d.sc_list.map((g, i) => (
              <div className="sc-row" key={i} style={{ background: scColor(g.value_yuan) }}>
                <span className="t">{g.ts.slice(11, 19)}</span>
                <span className="u">{g.uname}</span>
                <span className="sc-price">¥{g.value_yuan}</span>
                <div className="sc-text">{g.text}</div>
              </div>
            ))}
          </div>
          <div className="board-footer">
            SC 营收总计：¥{(d.sc_total.v || 0).toLocaleString()}（{d.sc_total.c || 0} 条）
          </div>
        </div>
        <div className="card card-pad">
          <h3>礼物列表 <span className="more">{d.audience.label || "本场"}</span></h3>
          <div className="stream" style={{ padding: 0, maxHeight: 380 }}>
            <table className="plain">
              <thead>
                <tr><th>礼物名称</th><th style={{ textAlign: "right" }}>礼物价值</th></tr>
              </thead>
              <tbody>
                {d.gift_agg.length === 0 && (
                  <tr><td colSpan={2} className="empty">本场暂无礼物</td></tr>
                )}
                {d.gift_agg.map((g, i) => (
                  <tr key={i}>
                    <td className="gift-name">
                      {g.img
                        ? <img className="gift-thumb" src={g.img} alt="" loading="lazy"
                               onError={(e) => (e.target as HTMLImageElement).remove()} />
                        : <span className="gift-thumb" />}
                      <span>
                        {g.name}
                        {(g.type === "guard" ? (g.orders || 0) : (g.cnt || 0)) > 1
                          ? ` ×${g.type === "guard" ? g.orders : g.cnt}` : ""}
                        {g.blind_box ? <span className="blind-tag" title={`来自${g.blind_box}`}>盲盒</span> : null}
                      </span>
                    </td>
                    <td style={{ textAlign: "right" }} className="price">¥{g.value}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="board-footer">
            礼物营收总计：¥{(d.gift_total.v || 0).toLocaleString()}（{d.gift_total.c || 0} 笔）
          </div>
        </div>
      </div>

      {SESSION_ID && (
        <SessionCurves ROOM={ROOM} SESSION_ID={SESSION_ID} data={curves} />
      )}

      <div className="card card-pad" style={{ marginTop: 16 }}>
        <h3>历史场次 <span className="more">点击行查看场次详情</span></h3>
        <div className="table-scroll">
          <table className="plain">
            <thead>
              <tr>
                <th>场次</th><th>开播时间</th><th>下播时间</th><th>时长(小时)</th>
                <th>同接峰值</th><th>弹幕</th><th>营收</th>
              </tr>
            </thead>
            <tbody>
              {d.sessions.length === 0 && (
                <tr><td colSpan={7} className="empty">暂无场次</td></tr>
              )}
              {d.sessions.map((s) => (
                <tr key={s.session_id} style={{ cursor: "pointer" }}
                    onClick={() => {
                      location.href = `${BASE}session/${ROOM}/${s.session_id}`;
                    }}>
                  <td>
                    {(s.titles || "").split("\n").filter(Boolean).map((t, i) => (
                      <div key={i} className={i === 0 ? "sess-title" : "sess-title sub"}
                           title={t}>{t}</div>
                    )) || `第 ${s.seq} 场`}
                    {!(s.titles || "").trim() && `第 ${s.seq} 场`}
                  </td>
                  <td>{s.start_time.slice(5, 16).replace("T", " ")}</td>
                  <td>{s.end_time
                    ? s.end_time.slice(5, 16).replace("T", " ")
                    : <span className="up">直播中</span>}</td>
                  <td>{s.hours ?? "—"}</td>
                  <td>{fmtNum(s.peak_viewers)}</td>
                  <td>{s.danmaku_count}</td>
                  <td className="money">¥{s.gift_value}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------
// 收益 / 人气曲线板块（v2.2：原高能时刻板块位置）——双 y 轴折线：
// 左轴收益（元/分，gifts 全口径按分钟桶）、右轴人气（加权），分钟网格
// 对齐、单侧无值的分钟给 null（connectNulls 断点续画）。
// ---------------------------------------------------------------------------
function SessionCurves(p: { ROOM: number; SESSION_ID: number; data: CurvesData | null }) {
  const darkAxis = { axisLabel: { color: "#8b96a8" } };
  const ref = useChart(p.data ? {
    grid: { left: 60, right: 60, top: 34, bottom: 28 },
    legend: { data: ["收益(元/分)", "人气"], top: 5, icon: "circle", itemWidth: 8 },
    tooltip: { ...TOOLTIP_THEME, trigger: "axis", confine: true },
    xAxis: {
      type: "category",
      data: p.data.points.map((x) => x.t.slice(11, 16)),
      ...darkAxis,
    },
    yAxis: [
      { type: "value", name: "元/分", nameTextStyle: { color: "#8b96a8", fontSize: 11 },
        splitLine: { lineStyle: { color: "rgba(226,234,244,.07)" } }, ...darkAxis },
      { type: "value", name: "人气", scale: true,
        nameTextStyle: { color: "#8b96a8", fontSize: 11 },
        splitLine: { show: false }, ...darkAxis },
    ],
    series: [
      { name: "收益(元/分)", type: "line", smooth: true, symbol: "none",
        connectNulls: true, lineStyle: { color: "#fb7299", width: 2 },
        areaStyle: { color: "rgba(251,114,153,.08)" },
        data: p.data.points.map((x) => x.rev) },
      { name: "人气", type: "line", smooth: true, symbol: "none",
        connectNulls: true, yAxisIndex: 1,
        lineStyle: { color: "#4fa8e8", width: 2 },
        data: p.data.points.map((x) => x.pop) },
    ],
  } as EChartsOption : null);
  return (
    <div className="card card-pad" style={{ marginTop: 16 }}>
      <h3>收益 / 人气曲线 <span className="more">本场</span></h3>
      {/* 容器无条件渲染：useChart 在挂载期完成 echarts.init（与本页其余
          四图同模式）——若按数据到达与否条件挂载，init 时机依赖后续渲染
          的 option 变化补救，dev/StrictMode 下会出现空容器（无 canvas） */}
      <div ref={ref} className="chart-box" style={{ height: 300 }} />
      {p.data
        ? <div style={{ color: "var(--faint)", fontSize: 12, marginTop: 6 }}>
            本场营收合计 ¥{(p.data.total_rev || 0).toLocaleString()} ·
            人气峰值 {(p.data.peak_pop || 0).toLocaleString()}（左轴为每分钟礼物+SC+上舰折算
            额，右轴为加权人气，与直播间页面人气一致）
          </div>
        : <div className="empty">本场暂无收益/人气数据</div>}
    </div>
  );
}
