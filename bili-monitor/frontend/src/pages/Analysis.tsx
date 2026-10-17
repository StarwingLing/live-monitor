// 数据分析页：主播下拉 + 概览指标 + 综合数据（问答）+ 留存（对照窗口
// 3/7/30/90 可切）+ 活跃度 + 营收构成（径向双层饼图）+ 弹幕趋势占位（后续
// 版本接入）。
import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { esc, fmtNum, toast } from "../ui";
import { useChart, TOOLTIP_THEME } from "../chart";
import type { EChartsOption } from "../echarts";

interface Room { room_id: number; anchor_name: string }
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
  const [qa, setQa] = useState<QaItem[]>([]);
  const [ret, setRet] = useState<RetResp | null>(null);
  const [act, setAct] = useState<ActResp | null>(null);
  const [rev, setRev] = useState<RevResp | null>(null);

  useEffect(() => {
    api<Room[]>("/api/rooms").then((rs) => {
      setRooms(rs);
      if (rs.length) setRoom(rs[0].room_id);
      else toast("暂无监控房间，先去「直播间管理」添加");
    }).catch((e) => toast("加载失败: " + e.message));
  }, []);

  const loadAll = useCallback(async () => {
    if (!room) return;
    const base = `/api/rooms/${room}/analysis`;
    const [o, r, a, q, v] = await Promise.all([
      api<Overview>(`${base}/overview?days=${days}`),
      api<RetResp>(`${base}/retention?days=${days}&ref_days=${retRefDays}`),
      api<ActResp>(`${base}/activity?days=${days}`),
      api<{ items: QaItem[] }>(`${base}/qa?days=${days}`),
      api<RevResp>(`${base}/revenue?days=${days}`),
    ]);
    setOv(o);
    setRet(r);
    setAct(a);
    setQa(q.items);
    setRev(v);
  }, [room, days, retRefDays]);

  useEffect(() => {
    loadAll().catch((e) => toast("加载失败: " + e.message));
  }, [loadAll]);

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
  const revRef = useChart(
    rev && rev.total.value > 0 ? revOption(rev, revRows) : null);

  return (
    <>
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

      <div className="card card-pad" style={{ marginBottom: 16 }}>
        <div className="manage-form">
          <select style={{ flex: 1, maxWidth: 320 }} value={room ?? ""}
                  onChange={(e) => setRoom(+e.target.value || null)}>
            {rooms.length === 0 && <option value="">暂无监控房间</option>}
            {rooms.map((r) => (
              <option key={r.room_id} value={r.room_id}>
                {r.anchor_name || "未知名"}（{r.room_id}）
              </option>
            ))}
          </select>
        </div>
        <div className="metrics">
          {[
            { k: "场次数", v: ov ? <>{ov.session_count}<small>场</small></> : "—" },
            { k: "直播总时长", v: ov ? <>{ov.total_hours}<small>小时</small></> : "—" },
            { k: "总弹幕", v: ov ? fmtNum(ov.danmaku_count) : "—" },
            { k: "总营收", v: ov ? <>¥{ov.gift_value.toLocaleString()}</> : "—" },
            { k: "场均弹幕", v: ov?.danmaku_per_session ?? "—" },
            { k: "场均营收", v: ov ? <>¥{ov.gift_per_session}</> : "—" },
            { k: "最高同接", v: ov ? fmtNum(ov.peak_viewers) : "—" },
            { k: "活跃观众", v: ov ? fmtNum(ov.active_users) : "—" },
          ].map((m, i) => (
            <div className="metric" key={i}>
              <div className="k">{m.k}</div>
              <div className="v">{m.v}</div>
            </div>
          ))}
        </div>
      </div>

      <div className="card card-pad" style={{ marginTop: 16 }}>
        <h3>综合数据 <span className="more">共 {qa.length} 项</span></h3>
        <div className="qa-grid">
          {qa.length === 0 && (
            <div className="empty" style={{ padding: "24px 0", gridColumn: "1/-1" }}>
              该窗口暂无数据
            </div>
          )}
          {qa.map((it, i) => (
            <div className="qa-item" key={i}>
              <div className="qa-q">{it.q}</div>
              <div className="qa-a">{it.a}</div>
              <div className="qa-sub">{it.sub}</div>
            </div>
          ))}
        </div>
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
          <div ref={retRef} className="chart-box" style={{ height: 300 }} />
          <div className="chart-note">
            每柱 = 一场直播（同日多场并列；无直播日留空）；留存率 = 与本场开播前
            {retRefDays} 天内所有已结束场次观众并集的重合人数 ÷ 并集人数，
            前 {retRefDays} 天无直播的场次不显示。
          </div>
        </div>
        <div className="card card-pad">
          <h3>直播活跃度 <span className="more">任一互动人数 ÷ 同接</span></h3>
          <div ref={actRef} className="chart-box" style={{ height: 300 }} />
          <div className="chart-note">
            按分钟统计"发弹幕/送礼/互动的去重人数 ÷ 同接人数"，取每场平均后再按日聚合（平均 / 最高）。
          </div>
        </div>
      </div>

      <div className="detail-extras">
        <div className="card card-pad chart-placeholder">
          <h3>弹幕趋势</h3><div className="ph-body">后续版本接入</div>
        </div>
        <div className="card card-pad">
          <h3>营收构成 <span className="more">前 10 用户 + 其他</span></h3>
          {revRows.length === 0 ? (
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
    </>
  );
}
