// 总览页：房间表（60s 轮询、直播置顶、营收降序）+ 窗口切换
// + 云场 hero 背景（自托管版，定稿 CloudField 同款视觉，2026-10-09 用户经
// 临时选择页选定）。v1.6：原「录制」跳转列已按用户要求移除（回放动线
// 归 replay 项目，两站独立）。
// v2.3：管理员新增「通知」列——开播 QQ 群通知开关 + 设置入口（齿轮弹窗
// NotifyPop：房间级群映射/专属文案；配置写共享库 qq_notify，通知器热加载）。
import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { api, fetchMe, patchJson } from "../api";
import { fmtNum, toast, avatarGradient } from "../ui";
import CloudBg from "../fx/CloudBg";
import NotifyPop from "./NotifyPop";

// WebGL 不可用 / 动效降级 / 窄屏时的 CSS 静态光晕（与云场的色调呼应）
const GLOW_BG =
  "radial-gradient(60% 90% at 25% 10%, rgba(251,114,153,.13), transparent 70%)," +
  "radial-gradient(50% 80% at 80% 90%, rgba(79,168,232,.10), transparent 70%)";

interface RoomRow {
  room_id: number; anchor_name: string; avatar_url: string;
  description: string; tags: string[]; live_now: number;
  session_count: number; last_start: string; hours: number;
  fan_growth: number | null; viewer_peak: number; danmaku_count: number;
  attention: number; guard_users: number; guards_is_real: number;
  gift_value: number; offline_value: number; interact_count: number;
  notify_enabled?: boolean; notify_group_count?: number; notify_custom?: boolean;
}

export default function Index() {
  const nav = useNavigate();
  const [days, setDays] = useState(7);
  const [rooms, setRooms] = useState<RoomRow[] | null>(null);
  const [subtitle, setSubtitle] = useState("加载中…");
  const [bgOk, setBgOk] = useState(false);
  const [admin, setAdmin] = useState(false);
  const [popRoom, setPopRoom] = useState<RoomRow | null>(null);

  useEffect(() => {
    fetchMe().then((m) => setAdmin(m.role === "admin")).catch(() => {});
  }, []);

  // hero 背景：云场（自托管版）。降级链：reduced-motion / 窄屏 /
  // 无 WebGL 一律回落 CSS 静态光晕。
  useEffect(() => {
    const mq = matchMedia("(prefers-reduced-motion: reduce)");
    const narrow = matchMedia("(max-width: 960px)");
    try {
      const c = document.createElement("canvas");
      if (mq.matches || narrow.matches ||
          !(c.getContext("webgl") || c.getContext("experimental-webgl"))) return;
    } catch { return; }
    setBgOk(true);
  }, []);

  const load = useCallback(async () => {
    const data = await api<{ rooms: RoomRow[] }>(`/api/summary?days=${days}`);
    setRooms(data.rooms);
    setSubtitle(`统计窗口：近 ${days} 天 · 共 ${data.rooms.length} 个直播间`);
  }, [days]);

  useEffect(() => {
    load().catch((e) => toast("加载失败: " + e.message));
    const t = window.setInterval(() => load().catch(() => {}), 60000);
    return () => window.clearInterval(t);
  }, [load]);

  // 通知开关：乐观更新 + 失败回滚（v2.3）
  async function toggleNotify(r: RoomRow) {
    const next = !r.notify_enabled;
    setRooms((rs) => rs ? rs.map((x) =>
      x.room_id === r.room_id ? { ...x, notify_enabled: next } : x) : rs);
    try {
      await patchJson(`/api/rooms/${r.room_id}/qq-notify`, { enabled: next });
      toast(next ? `已开启 ${r.anchor_name || r.room_id} 的开播通知`
                 : `已关闭 ${r.anchor_name || r.room_id} 的开播通知`);
    } catch (e: any) {
      setRooms((rs) => rs ? rs.map((x) =>
        x.room_id === r.room_id ? { ...x, notify_enabled: !next } : x) : rs);
      toast("操作失败：" + e.message);
    }
  }

  const sorted = useMemo(
    () => rooms ? [...rooms].sort((a, b) =>
      (b.live_now - a.live_now) || (b.gift_value - a.gift_value)) : null,
    [rooms]);

  const colCount = admin ? 10 : 9;

  return (
    <>
      <div className="hero">
        {bgOk ? <CloudBg className="hero-bg" /> : (
          <div className="hero-bg" style={{ background: GLOW_BG }} />
        )}
        <div className="inner">
          <div className="ht">直播间数据监控</div>
          <div className="hs">LIVE DATA MONITOR · {subtitle}</div>
        </div>
      </div>
      <div className="page-head">
        <h2>总览</h2>
        <span className="sub">{subtitle}</span>
        <div className="spacer" />
        <div className="tabbar">
          {[1, 7, 30].map((d) => (
            <button key={d} className={days === d ? "active" : ""}
                    onClick={() => setDays(d)}>
              {d === 1 ? "今天" : `近${d}天`}
            </button>
          ))}
        </div>
      </div>
      <div className="card" style={{ padding: 0 }}>
        <table className="room-table">
          <thead>
            <tr>
              <th style={{ width: 300 }}>主播</th>
              <th style={{ width: 150 }}>标签</th>
              <th>直播场次 / 最近开播</th>
              <th>时长 / 粉丝增长</th>
              <th>同接峰值 / 弹幕</th>
              <th>粉丝 / 大航海</th>
              <th>营收</th>
              <th>互动数</th>
              {admin && <th style={{ width: 96 }}>通知</th>}
            </tr>
          </thead>
          <tbody>
            {!sorted && (
              <tr><td colSpan={colCount} className="empty">加载中…</td></tr>
            )}
            {sorted?.length === 0 && (
              <tr><td colSpan={colCount} className="empty">暂无监控房间，去「直播间管理」添加</td></tr>
            )}
            {sorted?.map((r) => (
              <tr key={r.room_id} className={r.live_now ? "live-row" : ""}
                  onClick={() => nav(`/room/${r.room_id}`)}>
                <td>
                  <div className="anchor-cell" title={`房间号 ${r.room_id}`}>
                    <div className="avatar" style={{ background: avatarGradient(r.room_id) }}>
                      {r.avatar_url && (
                        <img src={r.avatar_url} alt="" loading="lazy"
                             onError={(e) => (e.target as HTMLImageElement).remove()} />
                      )}
                      {(r.anchor_name || "B")[0]}
                    </div>
                    <div>
                      <div className="name">
                        {r.anchor_name}
                        {r.live_now
                          ? <span className="badge live pulse">直播中</span>
                          : <span className="badge off">未开播</span>}
                      </div>
                      <div className="rid">
                        {r.description || `房间号 ${r.room_id}`}
                      </div>
                    </div>
                  </div>
                </td>
                <td>
                  {(r.tags || []).length
                    ? <span title={r.tags.join("、")}>
                        {r.tags.slice(0, 3).map((t) => (
                          <span className="tag-pill" key={t}>{t}</span>))}
                        {r.tags.length > 3 && (
                          <span className="tag-pill more">+{r.tags.length - 3}</span>
                        )}
                      </span>
                    : <span style={{ color: "var(--faint)" }}>—</span>}
                </td>
                <td>
                  <div className="num">{r.session_count}<small>场</small></div>
                  <div className="cell-sub">
                    {r.last_start ? r.last_start.slice(5, 16).replace("T", " ") : "—"}
                  </div>
                </td>
                <td>
                  <div className="num">{r.hours}<small>小时</small></div>
                  <div className={"cell-sub " + ((r.fan_growth || 0) > 0 ? "up" : (r.fan_growth || 0) < 0 ? "down" : "")}>
                    粉丝 {r.fan_growth == null ? "—"
                      : (r.fan_growth >= 0 ? "+" : "") + r.fan_growth}
                  </div>
                </td>
                <td>
                  <div className="num">{fmtNum(r.viewer_peak)}</div>
                  <div className="cell-sub">{fmtNum(r.danmaku_count)} 弹幕</div>
                </td>
                <td>
                  <div className="num">{fmtNum(r.attention)}</div>
                  <div className="cell-sub">
                    大航海 {r.guard_users}{r.guards_is_real ? "" : "（监控期）"}
                  </div>
                </td>
                <td>
                  <div className={"num" + (r.gift_value > 0 ? " money" : "")}>
                    ¥{r.gift_value.toLocaleString()}
                  </div>
                  {r.offline_value > 0 && (
                    <div className="cell-sub">场外 ¥{r.offline_value}</div>
                  )}
                </td>
                <td><div className="num">{fmtNum(r.interact_count)}</div></td>
                {admin && (
                  <td onClick={(e) => e.stopPropagation()}>
                    <div className="nf-cell">
                      <button className={"nf-sw" + (r.notify_enabled ? " on" : "")}
                              role="switch" aria-checked={!!r.notify_enabled}
                              title={r.notify_enabled ? "关闭开播通知" : "开启开播通知"}
                              onClick={() => toggleNotify(r)}>
                        <span className="nf-knob" />
                      </button>
                      <button className={"nf-gear" + (r.notify_custom ? " custom" : "")}
                              title={r.notify_group_count
                                ? `已配 ${r.notify_group_count} 个群${r.notify_custom ? " · 专属文案" : ""},点击设置`
                                : "未配置目标群,点击设置"}
                              onClick={() => setPopRoom(r)}>
                        <svg viewBox="0 0 24 24" width="15" height="15" fill="none"
                             stroke="currentColor" strokeWidth="1.8">
                          <circle cx="12" cy="12" r="3.2" />
                          <path d="M19.4 15a1.7 1.7 0 0 0 .34 1.87l.06.06a2 2 0 1 1-2.83 2.83
                                   l-.06-.06a1.7 1.7 0 0 0-1.87-.34 1.7 1.7 0 0 0-1.03 1.56V21a2 2 0 1 1-4
                                   0v-.09a1.7 1.7 0 0 0-1.03-1.56 1.7 1.7 0 0 0-1.87.34l-.06.06a2
                                   2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.87 1.7 1.7 0 0
                                   0-1.56-1.03H3a2 2 0 1 1 0-4h.09a1.7 1.7 0 0 0 1.56-1.03 1.7
                                   1.7 0 0 0-.34-1.87l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7
                                   1.7 0 0 0 1.87.34h.09a1.7 1.7 0 0 0 1.03-1.56V3a2 2 0 1 1 4
                                   0v.09a1.7 1.7 0 0 0 1.03 1.56 1.7 1.7 0 0 0 1.87-.34l.06-.06a2
                                   2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.87v.09a1.7
                                   1.7 0 0 0 1.56 1.03H21a2 2 0 1 1 0 4h-.09a1.7 1.7 0 0
                                   0-1.56 1.03z" />
                        </svg>
                        {(r.notify_group_count || 0) > 0 && (
                          <span className="nf-gcount">{r.notify_group_count}</span>
                        )}
                      </button>
                    </div>
                  </td>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {popRoom && (
        <NotifyPop room={popRoom} onClose={() => setPopRoom(null)}
                   onSaved={() => load().catch(() => {})} />
      )}
    </>
  );
}
