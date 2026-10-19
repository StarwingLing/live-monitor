// 数据分析页：主播筛选条（头像瓦片网格，v2.5.1 替换原下拉）+ 概览指标 +
// 综合数据（问答）+ 留存（对照窗口 3/7/30/90 可切）+ 活跃度 + 营收构成
// （径向双层饼图）+ 弹幕趋势占位（后续版本接入）。
// v2.5.1 骨架屏：切换主播/统计窗口时五板块立即全量置骨架，加载链按页面
// 自上而下顺序串行请求、逐块即时替换（慢服务器下顶部数据优先呈现，且
// 串行不打满 2 核小机的带宽与 CPU；原实现 Promise.all 并发五查、全部
// 到齐才一次性换装，页面长时间停在旧主播数据上）。
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { avatarGradient, esc, fmtNum, toast } from "../ui";
import { useChart, TOOLTIP_THEME } from "../chart";
import type { EChartsOption } from "../echarts";

interface Room { room_id: number; anchor_name: string; avatar_url: string }
interface Overview {
  session_count: number; total_hours: number; danmaku_count: number;
  gift_value: number; danmaku_per_session: number; gift_per_session: number;
  peak_viewers: number; active_users: number;
}
interface QaItem { q: string; a: string; sub: string }
interface RetSess {
  titles: string; start_time: string; retention: number | null;
  overlap: number; prev_total: number; cur_total: number;
}
interface RetDay { date: string; sessions: RetSess[] }
interface RetResp { ref_days: number; days_list: RetDay[] }
interface ActDay {
  date: string; avg_activity: number | null; max_activity: number | null;
  sessions: { start_time: string; avg_activity: number }[];
}
interface ActResp { days_list: ActDay[] }
// 营收构成：items = 前 10 用户（总营收降序）；others = 其余用户合并
// （uid 后端给 0、前端置 -1 标记灰系配色）；guard=舰队 / gift=礼物（含 SC）
interface RevUser {
  uid: number; uname: string; guard: number; gift: number; value: number;
}
interface RevResp {
  total: { guard: number; gift: number; value: number };
  items: RevUser[];
  others: RevUser | null;
}

// ---- 营收构成配色：名次固定色相（i×360/n 均分错开——最简且刷新不变）；
// 每个用户两圈同色系：礼物=基准亮度，舰队=提亮 10 个亮度点（暗底卡片上
// 更亮 ≈ 对比度高约 10%）；「其他用户」固定灰系，同样保持 +10 关系 ----
const revHue = (i: number, n: number) => (i * 360) / n + 15;
const revGift = (i: number, n: number) => `hsl(${revHue(i, n)}, 58%, 50%)`;
const revGuard = (i: number, n: number) => `hsl(${revHue(i, n)}, 58%, 60%)`;
const OTHER_GIFT = "hsl(220, 10%, 40%)";
const OTHER_GUARD = "hsl(220, 10%, 50%)";
// 舰/礼交界的径向留空（px，两侧各半）：卡片底色透出一条细缝，内外两圈
// 视觉分离，呈「爆炸图」分层观感
const REV_GAP = 3;
// 爆炸图外探（px）：每片沿各自角平分线向外平移，片间因此分离出缝；
// 平移后两圈半径同步缩回同值，使每片最外点仍压在原圆周上（外缘对齐）
const EXPLODE = 3;

const yuan = (v: number) => `¥${v.toLocaleString()}`;

// 营收构成图（custom series 手绘扇环）：每片角度 ∝ 用户总营收占比，径向按
// 该用户舰队占比分内/外两圈——饼系列不支持按数据项变半径，故用 renderItem
// 逐片画两个 sector（zrender 图元 shape：r0/r/startAngle/endAngle，弧度制）
function revOption(rev: RevResp, rows: RevUser[]): EChartsOption {
  const total = rev.total.value;
  // 角度占比累计：第一片从正上方起、顺时针排（与常规饼图一致）
  let cum = 0;
  const geo = rows.map((r) => {
    const a0 = cum / total;
    cum += r.value;
    return { a0, a1: cum / total, gf: r.value > 0 ? r.guard / r.value : 0 };
  });
  return {
    tooltip: {
      ...TOOLTIP_THEME,
      trigger: "item", confine: true,
      formatter: (p: any) => {
        const r = rows[p.dataIndex];
        return `<b>${esc(r.uname)}</b> · 占总营收 ${((r.value / total) * 100).toFixed(1)}%` +
          `<br>总营收 ${yuan(r.value)}` +
          `<br>舰队（内圈）${yuan(r.guard)} · 礼物（外圈）${yuan(r.gift)}` +
          `<br>舰队占该用户 ${((r.guard / r.value) * 100).toFixed(1)}%`;
      },
    },
    series: [{
      type: "custom",
      coordinateSystem: "none",
      data: rows.map((r) => r.value),
      renderItem: (params: any, api: any) => {
        const cx = api.getWidth() / 2;
        const cy = api.getHeight() / 2;
        const R0 = Math.min(api.getWidth(), api.getHeight()) / 2 - 12;
        const i = params.dataIndex;
        // canvas 角度约定：-π/2 = 正上方，角度增大 = 顺时针（y 轴向下）
        const t0b = -Math.PI / 2 + geo[i].a0 * 2 * Math.PI;
        const t1b = -Math.PI / 2 + geo[i].a1 * 2 * Math.PI;
        // 片间角缝：每侧内缩约 1.2px 弧长（极窄片按片宽 30% 封顶，防吃光；
        // 仅靠外探平移时窄片间缝 <0.5px 几乎不可见，故补这道角缝）
        const inset = Math.min(1.2 / R0, (t1b - t0b) * 0.3);
        const t0 = t0b + inset;
        const t1 = t1b - inset;
        // 沿本片中轴外探 EXPLODE 像素（爆炸图）；半径缩 EXPLODE 使每片
        // 最外点仍落在原 R0 圆周 → 各片外缘对齐、整体轮廓不外扩
        const mid = (t0 + t1) / 2;
        const cx2 = cx + EXPLODE * Math.cos(mid);
        const cy2 = cy + EXPLODE * Math.sin(mid);
        const R = R0 - EXPLODE;
        const rG = R * geo[i].gf;
        const [cIn, cOut] = rows[i].uid < 0
          ? [OTHER_GUARD, OTHER_GIFT]
          : [revGuard(i, rows.length), revGift(i, rows.length)];
        const sector = (r0: number, r: number, fill: string) => ({
          type: "sector",
          shape: { cx: cx2, cy: cy2, r0, r, startAngle: t0, endAngle: t1 },
          style: { fill },
        });
        // 舰队=内圈（圆心→rG）、礼物=外圈（rG→R），交界各退 REV_GAP/2 留缝；
        // 留空把端点吃剩 <0.5px 的圈不画（guard=0 时内圈自隐、极端占比同理）
        const rIn = rG - REV_GAP / 2;
        const rOut0 = rG + REV_GAP / 2;
        const children: any[] = [];
        if (rIn > 0.5) children.push(sector(0, rIn, cIn));
        if (R - rOut0 > 0.5) children.push(sector(rOut0, R, cOut));
        return { type: "group", children };
      },
    }],
  } as EChartsOption;
}

export default function Analysis() {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [room, setRoom] = useState<number | null>(null);
  const [days, setDays] = useState(30);
  const [retRefDays, setRetRefDays] = useState(30);
  const [ov, setOv] = useState<Overview | null>(null);
  // qa 用 null/[]/有值 三态：null=加载中（骨架）、[]=已加载但无数据
  const [qa, setQa] = useState<QaItem[] | null>(null);
  const [ret, setRet] = useState<RetResp | null>(null);
  const [act, setAct] = useState<ActResp | null>(null);
  const [rev, setRev] = useState<RevResp | null>(null);
  // 主链中加载失败的板块 key（渲染「加载失败」而非永久骨架；下次切换自动重试）
  const [failed, setFailed] = useState<Set<string>>(new Set());
  // 加载链代号：快速连续切换主播/窗口时，旧链的迟到响应按代号丢弃
  const genRef = useRef(0);
  // 「本次 retRefDays 变化由天数窗口联动引起」标记：天数 tab 同时改
  // days+retRefDays，主链会带新值重拉留存，留存单拉效应跳过本次
  const daysChangedRef = useRef(true);

  useEffect(() => {
    api<Room[]>("/api/rooms").then((rs) => {
      setRooms(rs);
      if (rs.length) setRoom(rs[0].room_id);
      else toast("暂无监控房间，先去「直播间管理」添加");
    }).catch((e) => toast("加载失败: " + e.message));
  }, []);

  // 主加载链（v2.5.1 骨架屏配套）：切换主播/统计窗口 → 五板块全部置加载态
  // （渲染骨架屏）→ 按【页面自上而下】顺序逐个请求、逐个即时替换。串行而非
  // 并发：顶部数据优先落位，且小规格服务器（2C/1.7G、3Mbps 出向）不被五个
  // 重查询同时打满。任一环节失败 toast 并标记该板块（显示「加载失败」），
  // 继续下一环节，不阻塞其余板块。
  useEffect(() => {
    if (!room) return;
    const g = ++genRef.current;
    daysChangedRef.current = true; // 本轮 days 可能联动改了 retRefDays，留存单拉跳过
    setOv(null); setRet(null); setAct(null); setQa(null); setRev(null);
    setFailed(new Set());
    const base = `/api/rooms/${room}/analysis`;
    const steps: [string, () => Promise<void>][] = [
      ["ov", async () =>
        setOv(await api<Overview>(`${base}/overview?days=${days}`))],
      ["qa", async () =>
        setQa((await api<{ items: QaItem[] }>(`${base}/qa?days=${days}`)).items)],
      ["ret", async () =>
        setRet(await api<RetResp>(`${base}/retention?days=${days}&ref_days=${retRefDays}`))],
      ["act", async () =>
        setAct(await api<ActResp>(`${base}/activity?days=${days}`))],
      ["rev", async () =>
        setRev(await api<RevResp>(`${base}/revenue?days=${days}`))],
    ];
    (async () => {
      for (const [key, step] of steps) {
        if (genRef.current !== g) return; // 已切走：旧链静默终止
        try {
          await step();
        } catch (e: any) {
          if (genRef.current !== g) return;
          toast("加载失败: " + e.message);
          setFailed((f) => new Set(f).add(key));
        }
      }
    })();
    // retRefDays 不入依赖：单独切换对照窗口由下方独立效应处理（只换留存
    // 一块，其余板块保留真实数据，不整页回骨架）
  }, [room, days]);

  // 留存对照窗口单独切换（3/7/30/90日）：仅重拉留存一块，旧图保留到新数据
  // 到达（快查不闪骨架）；随天数 tab 联动的变化已被 daysChangedRef 跳过
  useEffect(() => {
    if (daysChangedRef.current) { daysChangedRef.current = false; return; }
    if (!room) return;
    const g = genRef.current;
    api<RetResp>(`/api/rooms/${room}/analysis/retention?days=${days}&ref_days=${retRefDays}`)
      .then((r) => { if (genRef.current === g) setRet(r); })
      .catch((e) => toast("加载失败: " + e.message));
  }, [retRefDays]);

  // 留存图（嵌套类目轴 [日期, 时刻]）
  // retTips 与 cats 逐位对齐：无直播的日期同样占一位（null），否则空日期
  // 之后全部索引错位，悬停有数据的柱子会误报「当日无直播」
  const retTips: (RetSess | null)[] = ret
    ? ret.days_list.flatMap((day) =>
        day.sessions.length ? day.sessions : [null])
    : [];
  const retRef = useChart(ret ? (() => {
    const cats: [string, string][] = [];
    const vals: (number | null)[] = [];
    for (const day of ret.days_list) {
      const ds = day.date.slice(5).replace("-", "/");
      if (!day.sessions.length) {
        cats.push([ds, "—"]);
        vals.push(null);
        continue;
      }
      for (const s of day.sessions) {
        cats.push([ds, s.start_time.slice(11, 16)]);
        vals.push(s.retention == null ? null : +(s.retention * 100).toFixed(1));
      }
    }
    return {
      grid: { left: 44, right: 12, top: 24, bottom: 44 },
      tooltip: {
        ...TOOLTIP_THEME,
        trigger: "item", confine: true,
        formatter: (p: any) => {
          const s = retTips[p.dataIndex];
          if (!s) return `${cats[p.dataIndex][0]} 当日无直播`;
          const t = (s.titles || "").split("\n").filter(Boolean)[0] || "";
          return `<b>${s.start_time.slice(5, 16).replace("T", " ")}</b>` +
            (t ? `<br>${t}` : "") +
            `<br>留存率：${s.retention == null
              ? `—（前 ${ret!.ref_days} 天无直播）`
              : (s.retention * 100).toFixed(1) + "%"}` +
            `<br>与前 ${ret!.ref_days} 天观众重合 ${s.overlap} 人 / 前 ${ret!.ref_days} 天共 ${s.prev_total} 人` +
            `<br>本场观众 ${s.cur_total} 人`;
        },
      },
      xAxis: { type: "category", data: cats as any,
               axisLabel: { fontSize: 10, rotate: 45, color: "#8b96a8" } },
      yAxis: { type: "value", axisLabel: { formatter: (v: number) => v + "%", color: "#8b96a8" },
               splitLine: { lineStyle: { color: "rgba(226,234,244,.07)" } } },
      series: [{ type: "bar", data: vals, barMaxWidth: 26,
                 itemStyle: { color: "#f2558c", borderRadius: [4, 4, 0, 0] } }],
    } as EChartsOption;
  })() : null);

  const actRef = useChart(act ? (() => {
    const cats = act.days_list.map((x) => x.date.slice(5).replace("-", "/"));
    const pct = (v: number | null) => v == null ? null : +(v * 100).toFixed(1);
    return {
      grid: { left: 44, right: 12, top: 40, bottom: 28 },
      legend: { data: ["平均活跃度", "最高活跃度"], top: 5, icon: "circle", itemWidth: 8,
                textStyle: { color: "#8b96a8" } },
      tooltip: {
        ...TOOLTIP_THEME,
        trigger: "axis", confine: true,
        formatter: (ps: any) => {
          const day = act.days_list[ps[0].dataIndex];
          let h = `<b>${day.date}</b>`;
          if (!day.sessions.length) return h + "<br>当日无直播";
          for (const s of day.sessions)
            h += `<br>${s.start_time.slice(11, 16)} 场：平均 ${(s.avg_activity * 100).toFixed(1)}%`;
          return h + `<br>日平均 ${ps[0].value}% · 最高 ${ps[1].value}%`;
        },
      },
      xAxis: { type: "category", data: cats, axisLabel: { fontSize: 10, color: "#8b96a8" } },
      yAxis: { type: "value", axisLabel: { formatter: (v: number) => v + "%", color: "#8b96a8" },
               splitLine: { lineStyle: { color: "rgba(226,234,244,.07)" } } },
      series: [
        { name: "平均活跃度", type: "bar", barMaxWidth: 18,
          data: act.days_list.map((x) => pct(x.avg_activity)),
          itemStyle: { color: "#4fa8e8", borderRadius: [4, 4, 0, 0] } },
        { name: "最高活跃度", type: "bar", barMaxWidth: 18,
          data: act.days_list.map((x) => pct(x.max_activity)),
          itemStyle: { color: "#f2558c", borderRadius: [4, 4, 0, 0] } },
      ],
    } as EChartsOption;
  })() : null);

  // 营收构成行序：前 10 + 其余合并（有剩余才追加；uid 置 -1 标灰系配色）
  const revRows: RevUser[] = rev
    ? (rev.others ? [...rev.items, { ...rev.others, uid: -1 }] : [...rev.items])
    : [];
  // 营收饼图（v2.5.1 悬浮动画）：命中的扇形 sector 沿本片角平分线把所属
  // group 再外滑 5px、离开滑回，cubicInOut 缓入缓出约 200ms。zrender 层
  // 直接动画（custom series 无声明式 emphasis 位移，context7 已核对
  // zr.on 命中 e.target / animate().when().start('cubicInOut') 用法）；
  // 爆炸基座位移已烘焙在扇形 cx/cy 里，group 自身 x/y 静息恒为 0，
  // 动画即在 0↔5px 间往返，无需记录回弹目标。悬浮命中「其他」灰片等
  // uid<0 片同样生效（同为普通 group）。
  const revRef = useChart(
    rev && rev.total.value > 0 ? revOption(rev, revRows) : null,
    (chart) => {
      const zr = chart.getZr();
      // 位移上限说明：扇形外缘画在 R0=min(w,h)/2-12 的圆周上（画布边距
      // 12px，基座爆炸 3px 已在半径里补偿），悬浮位移再往外推——15px 会
      // 越出边距被裁切（服务器实测），5px 留有余量
      const HOVER = 5, DUR = 200;
      let last: any = null;   // 最近外滑的 group（globalout 兜底复位）
      const slide = (g: any, out: boolean, mid: number) => {
        g.stopAnimation?.();
        g.animate("", false)
          .when(DUR, out ? { x: HOVER * Math.cos(mid), y: HOVER * Math.sin(mid) }
                         : { x: 0, y: 0 })
          .start("cubicInOut");
      };
      // 命中的必须是扇形（shape 带 startAngle/endAngle），其 parent 为
      // renderItem 返回的每片 group；tooltip 文本等其他图元直接忽略
      const groupOf = (t: any) =>
        t && t.parent && t.parent.type === "group" &&
        t.shape && typeof t.shape.startAngle === "number" ? t.parent : null;
      zr.on("mouseover", (e: any) => {
        const g = groupOf(e.target);
        if (g) {
          const s = e.target.shape;
          slide(g, true, (s.startAngle + s.endAngle) / 2);
          last = g;
        }
      });
      zr.on("mouseout", (e: any) => {
        const g = groupOf(e.target);
        if (g) slide(g, false, 0);
      });
      zr.on("globalout", () => {
        if (last) { slide(last, false, 0); last = null; }
      });
    });

  // 五板块是否全部就位（aria-busy：加载中告知辅助技术页面仍在更新）
  const busy = !(ov && qa !== null && ret && act && rev);
  return (
    <div aria-busy={busy}>
      <div className="page-head">
        <h2>数据分析</h2>
        <span className="sub">统计窗口：近 {days} 天</span>
        <div className="spacer" />
        <div className="tabbar">
          {[7, 30, 90].map((d) => (
            <button key={d} className={days === d ? "active" : ""}
                    onClick={() => { setDays(d); setRetRefDays(d); }}>{`近${d}天`}</button>
          ))}
        </div>
      </div>

      {/* 主播筛选条：头像瓦片网格（参考 VLivePulse streamer-strip 布局）——
          头像在上/名字省略号在下，选中=粉边+光环；替代原 <select> 下拉 */}
      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="streamer-strip" aria-label="主播筛选">
          {rooms.length === 0 && (
            <div className="empty" style={{ padding: "18px 0", gridColumn: "1/-1" }}>
              暂无监控房间，先去「直播间管理」添加
            </div>
          )}
          {rooms.map((r) => (
            <button key={r.room_id}
                    className={"streamer-tile" + (room === r.room_id ? " active" : "")}
                    aria-pressed={room === r.room_id}
                    title={`${r.anchor_name || "未知名"}（${r.room_id}）`}
                    onClick={() => setRoom(r.room_id)}>
              <span className="ava" style={{ background: avatarGradient(r.room_id) }}>
                {r.avatar_url && (
                  <img src={r.avatar_url} alt="" loading="lazy"
                       onError={(e) => (e.target as HTMLImageElement).remove()} />
                )}
                {(r.anchor_name || "B")[0]}
              </span>
              <small>{r.anchor_name || "未知名"}</small>
            </button>
          ))}
        </div>
      </div>

      <div className="card card-pad">
        {ov ? (
          <div className="metrics">
            {[
              { k: "场次数", v: <>{ov.session_count}<small>场</small></> },
              { k: "直播总时长", v: <>{ov.total_hours}<small>小时</small></> },
              { k: "总弹幕", v: fmtNum(ov.danmaku_count) },
              { k: "总营收", v: <>¥{ov.gift_value.toLocaleString()}</> },
              { k: "场均弹幕", v: ov.danmaku_per_session },
              { k: "场均营收", v: <>¥{ov.gift_per_session}</> },
              { k: "最高同接", v: fmtNum(ov.peak_viewers) },
              { k: "活跃观众", v: fmtNum(ov.active_users) },
            ].map((m, i) => (
              <div className="metric" key={i}>
                <div className="k">{m.k}</div>
                <div className="v">{m.v}</div>
              </div>
            ))}
          </div>
        ) : failed.has("ov") ? (
          <div className="empty" style={{ padding: "24px 0" }}>概览数据加载失败</div>
        ) : (
          <div className="metrics">
            {Array.from({ length: 8 }, (_, i) => (
              <div className="metric" key={i}>
                <div className="sk" style={{ width: "44%", height: 11 }} />
                <div className="sk" style={{ width: "62%", height: 20, marginTop: 8 }} />
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="card card-pad" style={{ marginTop: 16 }}>
        <h3>综合数据 {qa !== null && <span className="more">共 {qa.length} 项</span>}</h3>
        {qa === null ? (
          failed.has("qa") ? (
            <div className="empty" style={{ padding: "24px 0" }}>综合数据加载失败</div>
          ) : (
            <div className="qa-grid">
              {Array.from({ length: 6 }, (_, i) => (
                <div className="qa-item" key={i}>
                  <div className="sk" style={{ width: "58%", height: 11 }} />
                  <div className="sk" style={{ width: "38%", height: 20, marginTop: 8 }} />
                  <div className="sk" style={{ width: "30%", height: 10, marginTop: 6 }} />
                </div>
              ))}
            </div>
          )
        ) : qa.length === 0 ? (
          <div className="empty" style={{ padding: "24px 0", gridColumn: "1/-1" }}>
            该窗口暂无数据
          </div>
        ) : (
          <div className="qa-grid">
            {qa.map((it, i) => (
              <div className="qa-item" key={i}>
                <div className="qa-q">{it.q}</div>
                <div className="qa-a">{it.a}</div>
                <div className="qa-sub">{it.sub}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="detail-extras">
        <div className="card card-pad">
          <h3>
            观众留存
            <span className="ret-tabs">
              {[3, 7, 30, 90].map((r) => (
                <button key={r} className={retRefDays === r ? "active" : ""}
                        onClick={() => setRetRefDays(r)}>{r}日</button>
              ))}
            </span>
          </h3>
          {ret === null ? (
            failed.has("ret") ? (
              <div className="empty" style={{ padding: "40px 0" }}>留存数据加载失败</div>
            ) : (
              <>
                <div className="sk" style={{ height: 300, borderRadius: 10 }} />
                <div className="sk" style={{ width: "72%", height: 11, marginTop: 9 }} />
              </>
            )
          ) : (
            <>
              <div ref={retRef} className="chart-box" style={{ height: 300 }} />
              <div className="chart-note">
                每柱 = 一场直播（同日多场并列；无直播日留空）；留存率 = 与本场开播前
                {retRefDays} 天内所有已结束场次观众并集的重合人数 ÷ 并集人数，
                前 {retRefDays} 天无直播的场次不显示。
              </div>
            </>
          )}
        </div>
        <div className="card card-pad">
          <h3>直播活跃度 <span className="more">任一互动人数 ÷ 同接</span></h3>
          {act === null ? (
            failed.has("act") ? (
              <div className="empty" style={{ padding: "40px 0" }}>活跃度数据加载失败</div>
            ) : (
              <>
                <div className="sk" style={{ height: 300, borderRadius: 10 }} />
                <div className="sk" style={{ width: "58%", height: 11, marginTop: 9 }} />
              </>
            )
          ) : (
            <>
              <div ref={actRef} className="chart-box" style={{ height: 300 }} />
              <div className="chart-note">
                按分钟统计"发弹幕/送礼/互动的去重人数 ÷ 同接人数"，取每场平均后再按日聚合（平均 / 最高）。
              </div>
            </>
          )}
        </div>
      </div>

      <div className="detail-extras">
        <div className="card card-pad chart-placeholder">
          <h3>弹幕趋势</h3><div className="ph-body">后续版本接入</div>
        </div>
        <div className="card card-pad">
          <h3>营收构成 <span className="more">前 10 用户 + 其他</span></h3>
          {rev === null ? (
            failed.has("rev") ? (
              <div className="empty" style={{ padding: "40px 0" }}>营收数据加载失败</div>
            ) : (
              <div className="rev-wrap">
                <div className="sk" style={{ flex: "1 1 56%", height: 264, borderRadius: 10 }} />
                <div className="rev-legend">
                  {Array.from({ length: 6 }, (_, i) => (
                    <div className="rev-row" key={i}>
                      <div className="sk" style={{ width: 9, height: 9, borderRadius: "50%", flex: "none" }} />
                      <div className="sk" style={{ flex: 1, height: 12 }} />
                      <div className="sk" style={{ width: 42, height: 12 }} />
                    </div>
                  ))}
                </div>
              </div>
            )
          ) : revRows.length === 0 ? (
            <div className="empty" style={{ padding: "24px 0" }}>该窗口暂无营收数据</div>
          ) : (
            <div className="rev-wrap">
              <div ref={revRef} className="chart-box" style={{ height: 264 }} />
              <div className="rev-legend">
                {revRows.map((r, i) => (
                  <div className="rev-row" key={r.uid}>
                    <span className="dot" style={{
                      background: r.uid < 0 ? OTHER_GIFT : revGift(i, revRows.length),
                    }} />
                    <span className="name" title={r.uname}>{r.uname}</span>
                    <span className="val">{yuan(r.value)}</span>
                    <span className="gp">
                      {((r.value / (rev?.total.value || 1)) * 100).toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
          <div className="chart-note">
            每片角度 = 该用户总营收占比（礼物 + SC + 舰队，含场外）；径向分层：
            内圈舰队收益、外圈礼物收益（交界留数像素空隙），内圈半径 ≈ 该用户
            舰队占比；各片沿中轴外探 3px 分离（外缘仍对齐同一圆周）；同一用户
            同色系，舰队色提亮约 10%（对比度更高）。
          </div>
        </div>
      </div>
    </div>
  );
}
