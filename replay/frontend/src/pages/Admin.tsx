// 管理页（仅管理员）：切片员密码 / 监控码 / 自动全程录制开关 /
// 房间名单增删（v2.0.0：项目独立后自管，短号自动解析、添加即生成监控码）。
// 30s 轮询刷新；复制走 Clipboard API + execCommand 降级（公网 HTTP 场景）。
import { useCallback, useEffect, useState } from "react";
import { api, postJson, BASE, logout } from "../api";
import { Switch, toast } from "../ui";

interface Room {
  room_id: number;
  anchor_name: string;
  avatar_url: string;
  enabled: number;
  auto_full: boolean;
  replay_code: string;
  live: boolean;
  monitored: boolean;   // 是否同时在 monitor 监控名单且监控中（录制的前提）
}

const AVATAR_PH = (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z" />
  </svg>
);

function legacyCopy(text: string): boolean {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  let ok = false;
  try {
    ok = document.execCommand("copy");
  } catch {
    ok = false;
  }
  ta.remove();
  return ok;
}

function copyCode(ev: React.MouseEvent, code: string) {
  ev.stopPropagation();
  const done = () => toast("已复制：" + code);
  const fail = () => toast("复制失败，请长按/拖选：" + code);
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(code).then(done).catch(() => {
      legacyCopy(code) ? done() : fail();
    });
    return;
  }
  legacyCopy(code) ? done() : fail();
}

export default function Admin() {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [pw, setPw] = useState("…");
  const [err, setErr] = useState("");
  const [newRid, setNewRid] = useState("");
  const [adding, setAdding] = useState(false);

  const refresh = useCallback(async () => {
    const [d, p] = await Promise.all([
      api<{ rooms: Room[] }>("/api/admin/rooms"),
      api<{ clipper_password: string }>("/api/admin/clipper-password"),
    ]);
    setPw(p.clipper_password || "（未设置）");
    setRooms(d.rooms || []);
    setErr("");
  }, []);

  useEffect(() => {
    refresh().catch((e) => setErr(e.message));
    const t = window.setInterval(
      () => refresh().catch(() => {}), 30000);
    return () => window.clearInterval(t);
  }, [refresh]);

  async function resetPw() {
    if (!confirm("重置切片员共用密码？所有切片员的旧密码将立即失效"
        + "（已登录的会话不受影响）。")) return;
    try {
      const r = await postJson<{ clipper_password: string }>(
        "/api/admin/clipper-password/reset", {});
      toast("切片员密码已重置：" + r.clipper_password);
      setPw(r.clipper_password);
    } catch (e: any) {
      toast("操作失败：" + e.message);
    }
  }

  async function resetCode(rid: number) {
    if (!confirm(`重置房间 ${rid} 的监控码？旧码将立即失效。`)) return;
    try {
      const r = await postJson<{ replay_code: string }>(
        `/api/rooms/${rid}/replay-code/reset`, {});
      toast(`新监控码：${r.replay_code}`);
      refresh().catch(() => {});
    } catch (e: any) {
      toast("操作失败：" + e.message);
    }
  }

  async function toggleAf(rid: number, enable: boolean) {
    try {
      await postJson(`/api/rooms/${rid}/auto-full`, { enabled: enable });
      toast(enable ? "自动全程录制已开启（在播时约 30 秒内开始）"
                   : "自动全程录制已关闭（进行中的录制不受影响）");
      refresh().catch(() => {});
    } catch (e: any) {
      toast(e.message);
    }
  }

  // ---- 房间名单自管（v2.0.0）----
  async function addRoom() {
    const rid = parseInt(newRid.trim(), 10);
    if (!rid || rid <= 0) {
      toast("请输入正确的房间号");
      return;
    }
    setAdding(true);
    try {
      const r = await postJson<{
        room_id: number; anchor_name: string; replay_code: string;
      }>("/api/admin/rooms", { room_id: rid });
      toast(`已添加：${r.anchor_name || r.room_id} · 监控码 ${r.replay_code}`);
      setNewRid("");
      refresh().catch(() => {});
    } catch (e: any) {
      toast("添加失败：" + e.message);
    } finally {
      setAdding(false);
    }
  }

  async function pauseRoom(rid: number, name: string) {
    if (!confirm(`停用「${name}」的回放站入口？历史录像与监控码保留，` +
        "仅本站隐藏该房间；monitor 侧的数据采集不受影响。")) return;
    try {
      await api(`/api/admin/rooms/${rid}`, { method: "DELETE" });
      toast(`已在回放站停用 ${name}（monitor 采集不受影响）`);
      refresh().catch(() => {});
    } catch (e: any) {
      toast("操作失败：" + e.message);
    }
  }

  async function enableRoom(rid: number) {
    try {
      await postJson(`/api/admin/rooms/${rid}/enable`, {});
      toast("已在回放站恢复该房间");
      refresh().catch(() => {});
    } catch (e: any) {
      toast("操作失败：" + e.message);
    }
  }

  async function hardDeleteRoom(rid: number, name: string) {
    if (!confirm(`彻底删除「${name}」的回放站名单行？` +
        "监控码与回放页入口将失效（历史录像/注释数据仍保留在库中，" +
        "monitor 侧监控不受影响）。")) return;
    if (!confirm("再次确认：彻底删除后该房间需重新添加才能恢复回放站入口。")) return;
    try {
      await api(`/api/admin/rooms/${rid}?hard=true`, { method: "DELETE" });
      toast(`已彻底删除 ${name}`);
      refresh().catch(() => {});
    } catch (e: any) {
      toast("操作失败：" + e.message);
    }
  }

  return (
    <div className="ad-page">
      <div className="ad-head">
        <div className="t1">回放管理</div>
        <span className="sub">房间名单 · 切片员密码 · 监控码 · 自动全程录制（开关约 30 秒内生效）</span>
        <div className="ops">
          <input
            className="ad-add-input"
            value={newRid}
            placeholder="房间号"
            inputMode="numeric"
            spellCheck={false}
            onChange={(e) => setNewRid(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && !adding && addRoom()} />
          <button className="btn sm" disabled={adding}
                  onClick={() => void addRoom()}>
            {adding ? "添加中…" : "+ 添加房间"}
          </button>
          <button className="btn sm" onClick={() => void logout()}>退出登录</button>
        </div>
      </div>
      <div className="card pw-card">
        <div className="rc-cell">
          <span className="cap">切片员密码</span>
          <span className="rcode" title="点击复制"
                onClick={(e) => copyCode(e, pw)}>{pw}</span>
          <span className="pw-note">全员共用 · 仅作身份验证 · 重置后旧密码立即失效</span>
          <button className="btn sm" style={{ marginLeft: "auto" }}
                  onClick={resetPw}>重置密码</button>
        </div>
      </div>
      {err && <div className="empty">加载失败：{err}</div>}
      {!rooms && !err && <div className="empty">加载中…</div>}
      {rooms && rooms.length === 0 && (
        <div className="empty">暂无监控房间，在右上方输入房间号添加</div>
      )}
      {rooms?.map((r) => (
        <div className="card room-card" key={r.room_id}>
          <div className="rc-top">
            <div className="rc-avatar">
              {AVATAR_PH}
              {r.avatar_url && (
                <img src={r.avatar_url} alt="" loading="lazy"
                     onError={(e) => (e.target as HTMLImageElement).remove()} />
              )}
            </div>
            <div>
              <div className="rc-name">
                {r.anchor_name || "房间 " + r.room_id}
                {r.live
                  ? <span className="badge live pulse">直播中</span>
                  : <span className="badge off">未开播</span>}
                {!r.enabled && <span className="badge">已停用</span>}
                {!r.monitored && (
                  <span className="badge" title="该房间不在 monitor 监控名单（或监控已停用），无法自动/手动录制；需在 bili-monitor 管理页添加监控">
                    未监控
                  </span>
                )}
              </div>
              <div className="rc-id">房间号 {r.room_id}</div>
            </div>
            <a className="btn sm rc-enter" style={{ marginLeft: "auto", textDecoration: "none" }}
               href={`${BASE}${r.room_id}/replay`}>进入回放</a>
          </div>
          <div className="rc-manage">
            <div className="rc-cell">
              <span className="cap">监控码</span>
              {r.replay_code ? (
                <>
                  <span className="rcode" title="点击复制"
                        onClick={(e) => copyCode(e, r.replay_code)}>
                    {r.replay_code}
                  </span>
                  <button className="btn sm ghost"
                          onClick={() => resetCode(r.room_id)}>重置</button>
                </>
              ) : (
                <button className="btn sm ghost"
                        onClick={() => resetCode(r.room_id)}>生成监控码</button>
              )}
            </div>
            <div className="rc-switch-wrap">
              <span className="lab">自动全程录制</span>
              {r.monitored ? (
                <Switch on={r.auto_full} title="开播自动录制，下播自动保存（capture 侧执行，约 30 秒内生效）"
                        onClick={() => toggleAf(r.room_id, !r.auto_full)} />
              ) : (
                <span className="rc-unmonitored" title="未监控的房间无法录制（capture 只为 monitor 监控名单建房）">—</span>
              )}
            </div>
            <div className="rc-switch-wrap">
              <span className="lab" title="仅控制本回放站的名单与入口；monitor 侧监控名单独立维护">回放站</span>
              {r.enabled ? (
                <button className="btn sm ghost"
                        onClick={() => pauseRoom(r.room_id, r.anchor_name || String(r.room_id))}>
                  停用
                </button>
              ) : (
                <>
                  <button className="btn sm"
                          onClick={() => enableRoom(r.room_id)}>启用</button>
                  <button className="btn sm ghost danger"
                          style={{ marginLeft: 6 }}
                          onClick={() => hardDeleteRoom(r.room_id, r.anchor_name || String(r.room_id))}>
                    彻底删除
                  </button>
                </>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}
