// 直播间管理页：监控房间增删/暂停/恢复 + 描述/标签行内编辑（30s 轮询）。
// v2.4.0 起 QQ 机器人相关配置（原「开播账号管理」板块）独立为「机器人管理」
// 页（/bot，BotManage.tsx）；本页只管监控房间名单本身。
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router";
import { api, patchJson, postJson } from "../api";
import { toast } from "../ui";

interface Room {
  room_id: number; anchor_name: string; enabled: number;
  cur_session: unknown; description: string; tags: string[]; created_at: string;
}

export default function Manage() {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [ridInput, setRidInput] = useState("");
  const [editDesc, setEditDesc] = useState<number | null>(null);
  const [descVal, setDescVal] = useState("");

  const refresh = useCallback(async () => {
    setRooms(await api<Room[]>("/api/rooms"));
  }, []);
  useEffect(() => {
    refresh().catch((e) => toast("加载失败: " + e.message));
    const t = window.setInterval(() => refresh().catch(() => {}), 30000);
    return () => window.clearInterval(t);
  }, [refresh]);

  async function addRoom() {
    const rid = +ridInput;
    if (!rid) {
      toast("请输入房间号");
      return;
    }
    try {
      const r = await postJson<{ anchor_name: string; room_id: number }>("/api/rooms", {
        room_id: rid,
      });
      toast(`已添加：${r.anchor_name || r.room_id}`);
      setRidInput("");
      refresh().catch(() => {});
    } catch (e: any) {
      toast("添加失败：" + e.message);
    }
  }

  async function pause(rid: number) {
    try {
      await api(`/api/rooms/${rid}?keep_history=true`, { method: "DELETE" });
      toast("已暂停监控（历史保留）");
      refresh().catch(() => {});
    } catch (e: any) {
      toast("操作失败: " + e.message);
    }
  }

  async function removeRoom(rid: number) {
    if (!confirm(`彻底移除房间 ${rid}？名单记录将删除，明细数据保留。`)) return;
    try {
      await api(`/api/rooms/${rid}?keep_history=false`, { method: "DELETE" });
      toast("已移除");
      refresh().catch(() => {});
    } catch (e: any) {
      toast("操作失败: " + e.message);
    }
  }

  async function enable(rid: number) {
    try {
      await postJson(`/api/rooms/${rid}/enable`, {});
      toast("已恢复监控");
      refresh().catch(() => {});
    } catch (e: any) {
      toast("操作失败: " + e.message);
    }
  }

  async function saveDesc(rid: number, cancel = false) {
    if (cancel) {
      setEditDesc(null);
      return;
    }
    try {
      await patchJson(`/api/rooms/${rid}/description`, {
        description: descVal.trim(),
      });
      setEditDesc(null);
      refresh().catch(() => {});
    } catch (e: any) {
      toast("保存失败: " + e.message);
      setEditDesc(null);
    }
  }

  async function addTag(rid: number) {
    const tag = (prompt("输入标签（1~12 个字）") || "").trim();
    if (!tag) return;
    try {
      await postJson(`/api/rooms/${rid}/tags`, { tag });
      refresh().catch(() => {});
    } catch (e: any) {
      toast("添加失败: " + e.message);
    }
  }

  async function delTag(rid: number, tag: string) {
    try {
      await api(`/api/rooms/${rid}/tags/${encodeURIComponent(tag)}`,
                { method: "DELETE" });
      refresh().catch(() => {});
    } catch (e: any) {
      toast("删除失败: " + e.message);
    }
  }

  return (
    <>
      <div className="page-head">
        <h2>直播间管理</h2>
        <span className="sub">监控房间的增删与暂停（改动 30 秒内热生效，无需重启服务）</span>
      </div>
      <div className="card card-pad manage-block">
        <h3 className="block-title">监控房间</h3>
        <div className="manage-form">
          <input type="number" placeholder="房间号（短号/长号均可）"
                 value={ridInput} onChange={(e) => setRidInput(e.target.value)} />
          <button className="btn primary" onClick={addRoom}>添加监控</button>
        </div>
        <div className="table-scroll">
          <table className="plain">
            <thead>
              <tr>
                <th>房间号</th><th>主播</th><th>状态</th>
                <th style={{ width: 220 }}>描述</th><th style={{ width: 190 }}>标签</th>
                <th>加入时间</th><th style={{ width: 240 }}>操作</th>
              </tr>
            </thead>
            <tbody>
              {!rooms && <tr><td colSpan={7} className="empty">加载中…</td></tr>}
              {rooms?.length === 0 && (
                <tr><td colSpan={7} className="empty">暂无房间</td></tr>
              )}
              {rooms?.map((r) => (
                <tr key={r.room_id}>
                  <td>
                    <Link to={`/room/${r.room_id}`}
                          style={{ color: "var(--blue)", textDecoration: "none" }}>
                      {r.room_id}
                    </Link>
                  </td>
                  <td>{r.anchor_name || "—"}</td>
                  <td>
                    {r.enabled
                      ? (r.cur_session
                          ? <span className="badge live pulse">直播中</span>
                          : <span className="badge">监控中</span>)
                      : <span className="badge off">已暂停</span>}
                  </td>
                  <td>
                    {editDesc === r.room_id ? (
                      <input className="desc-input" maxLength={100}
                             autoFocus value={descVal}
                             onChange={(e) => setDescVal(e.target.value)}
                             onKeyDown={(e) => {
                               if (e.key === "Enter") saveDesc(r.room_id);
                               if (e.key === "Escape") saveDesc(r.room_id, true);
                             }}
                             onBlur={() => saveDesc(r.room_id)} />
                    ) : (
                      <div className="rid desc" style={{ fontSize: 12.5 }}
                           title="点击编辑描述"
                           onClick={() => {
                             setEditDesc(r.room_id);
                             setDescVal(r.description || "");
                           }}>
                        {r.description ||
                          <span className="desc-empty">＋ 设置描述</span>}
                      </div>
                    )}
                  </td>
                  <td>
                    {(r.tags || []).map((t) => (
                      <span className="tag-pill" key={t}>
                        {t}
                        <span className="tag-x"
                              onClick={() => delTag(r.room_id, t)}>×</span>
                      </span>
                    ))}
                    <span className="tag-add" title="添加标签"
                          onClick={() => addTag(r.room_id)}>＋</span>
                  </td>
                  <td>{(r.created_at || "").slice(0, 16).replace("T", " ")}</td>
                  <td>
                    {r.enabled ? (
                      <>
                        <button className="btn sm ghost"
                                onClick={() => pause(r.room_id)}>暂停</button>{" "}
                        <button className="btn sm danger"
                                onClick={() => removeRoom(r.room_id)}>彻底移除</button>
                      </>
                    ) : (
                      <>
                        <button className="btn sm"
                                onClick={() => enable(r.room_id)}>恢复监控</button>{" "}
                        <button className="btn sm danger"
                                onClick={() => removeRoom(r.room_id)}>彻底移除</button>
                      </>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  );
}
