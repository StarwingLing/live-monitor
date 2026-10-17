// 管理页：直播间管理（增删/暂停/恢复 + 描述/标签行内编辑，30s 轮询）
// + 开播账号管理（v2.3 新增同级板块：QQ 通知账号/全局通知配置，仅管理员；
// 含扫码登录 QQ——二维码直接嵌入本板块自动加载轮询，无需另开 NapCat WebUI。
// NapCat/WebUI 连接项不展示：与通知器同机部署，库中默认值(127.0.0.1)即
// 生产值，后端 PATCH 接口仍支持改这些字段，需要时经 SQL/接口调整）。
// v2.3.1 双通道：「发送通道」下拉 napcat/official 切换——official 为官方
// 机器人 API(填 AppID/AppSecret，无扫码/NapCat 概念，NapCat 专属控件
// 随通道条件隐藏；扫码轮询在切离 napcat 时自动停止)。
import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import QRCode from "qrcode";
import { api, fetchMe, patchJson, postJson } from "../api";
import { toast } from "../ui";

interface Room {
  room_id: number; anchor_name: string; enabled: number;
  cur_session: unknown; description: string; tags: string[]; created_at: string;
}

interface GlobalCfg {
  qq_account: string; onebot_url: string; onebot_token: string;
  webui_url: string; webui_token: string;
  channel: string;                      // v2.3.1 'napcat' | 'official'
  official_app_id: string;              // v2.3.1 官方机器人 AppID
  official_app_secret: string;          // v2.3.1 官方机器人 AppSecret
  command_replies: Record<string, string>;  // v2.3.1 群指令自动回复表(空=默认)
  notify_on: boolean; notify_off: boolean;
  template_on: string; template_off: string; debounce_offline: number;
}

// 扫码登录聚合端点的返回(后端代理 NapCat WebUI)
interface QrState {
  phase: "online" | "qr" | "scanned" | "error";
  qrcode: string; message: string;
}

export default function Manage() {
  const [rooms, setRooms] = useState<Room[] | null>(null);
  const [ridInput, setRidInput] = useState("");
  const [editDesc, setEditDesc] = useState<number | null>(null);
  const [descVal, setDescVal] = useState("");
  const [admin, setAdmin] = useState(false);

  useEffect(() => {
    fetchMe().then((m) => setAdmin(m.role === "admin")).catch(() => {});
  }, []);

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
        <h2>管理</h2>
        <span className="sub">监控房间与开播通知账号（改动 30 秒内热生效，无需重启服务）</span>
      </div>
      <div className="card card-pad manage-block">
        <h3 className="block-title">直播间管理</h3>
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
      {admin && <NotifyAccountCard />}
    </>
  );
}

// ---------------------------------------------------------------------------
// 开播账号管理（v2.3）：QQ 通知小号与全局通知配置。
// 数据落共享库 qq_notify_global，live-qq-notify 通知器每轮热加载；
// 房间级开关/群映射在总览页「通知」列（NotifyPop）。
// ---------------------------------------------------------------------------
function NotifyAccountCard() {
  const [g, setG] = useState<GlobalCfg | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testMsg, setTestMsg] = useState("");
  // 扫码登录区:qrOn=轮询中;qrData=二维码图片 dataURL
  const [qrOn, setQrOn] = useState(false);
  const [qrState, setQrState] = useState<QrState | null>(null);
  const [qrData, setQrData] = useState("");
  const qrTimer = useRef<number | null>(null);
  const qrFreshTimer = useRef<number | null>(null);   // 二维码保鲜定时器
  // 群映射获取(v2.3.1 official):输入群号 → 绑定会话轮询 → 映射列表
  const [bindGid, setBindGid] = useState("");
  const [binding, setBinding] = useState(false);
  const [bindMsg, setBindMsg] = useState("");
  const [mapItems, setMapItems] = useState<
    { group_id: number; group_openid: string; updated_at: string }[]>([]);
  const bindTimer = useRef<number | null>(null);
  // 指令自动回复行编辑器(v2.3.1):{k:指令, v:回复},随「保存配置」提交
  const [cmdRows, setCmdRows] = useState<{ k: string; v: string }[]>([]);

  useEffect(() => {
    api<GlobalCfg>("/api/qq-notify/global").then((r) => {
      setG(r);
      // 指令表初始行 = 后端生效口径(库值,空时后端已回填默认)
      setCmdRows(Object.entries(r.command_replies || {})
        .map(([k, v]) => ({ k, v })));
    }).catch((e) => toast("通知配置加载失败: " + e.message));
  }, []);

  // 卸载清理轮询
  useEffect(() => () => {
    if (qrTimer.current) window.clearInterval(qrTimer.current);
    if (qrFreshTimer.current) window.clearTimeout(qrFreshTimer.current);
    if (bindTimer.current) window.clearInterval(bindTimer.current);
  }, []);

  // 二维码内容串 → 图片(内容变化才重渲)
  useEffect(() => {
    if (!qrState?.qrcode) { setQrData(""); return; }
    QRCode.toDataURL(qrState.qrcode, { width: 220, margin: 1 })
      .then(setQrData).catch(() => setQrData(""));
  }, [qrState?.qrcode]);

  function set<K extends keyof GlobalCfg>(key: K, val: GlobalCfg[K]) {
    setG((cur) => cur ? { ...cur, [key]: val } : cur);
  }

  async function pollQr(refresh = false) {
    try {
      const r = await api<QrState>(`/api/qq-notify/qrlogin${refresh ? "?refresh=1" : ""}`);
      setQrState(r);
      if (r.phase === "online") {
        stopQr();
        toast("✅ QQ 扫码登录成功");
        testConn();   // 自动刷新连接状态(登录后 OneBot 接口即可用)
      }
    } catch (e: any) {
      setQrState({ phase: "error", qrcode: "", message: e.message });
    }
  }

  // 二维码保鲜(2026-10-14):QQ 扫码码有效期约 2 分钟,每 90 秒强制换新码,
  // 防止页面展示的码过期(扫了提示"二维码已过期")
  function scheduleFresh() {
    if (qrFreshTimer.current) window.clearTimeout(qrFreshTimer.current);
    qrFreshTimer.current = window.setTimeout(() => {
      if (qrTimer.current) pollQr(true).then(scheduleFresh);
    }, 90_000);
  }

  function startQr() {
    if (qrTimer.current) return;
    setQrOn(true);
    pollQr().then(scheduleFresh);
    qrTimer.current = window.setInterval(() => pollQr(), 2500);
  }

  function stopQr() {
    if (qrTimer.current) window.clearInterval(qrTimer.current);
    qrTimer.current = null;
    if (qrFreshTimer.current) window.clearTimeout(qrFreshTimer.current);
    qrFreshTimer.current = null;
    setQrOn(false);
  }

  async function save() {
    if (!g) return;
    // 指令表行 → dict(空行跳过;不成对/重复拒绝保存)
    const cr: Record<string, string> = {};
    for (const row of cmdRows) {
      const k = row.k.trim(), v = row.v.trim();
      if (!k && !v) continue;
      if (!k || !v) {
        toast("指令自动回复:指令与回复须成对填写(空行可留)");
        return;
      }
      if (cr[k] !== undefined) {
        toast(`指令重复:${k}`);
        return;
      }
      cr[k] = v;
    }
    setSaving(true);
    try {
      // 连接类字段(onebot_url/token、webui_url/token)不提交:页面已不
      // 展示输入项,PATCH 未传字段不动库中既有值(默认即生产值)
      const saved = await patchJson<GlobalCfg>("/api/qq-notify/global", {
        channel: g.channel,
        official_app_id: g.official_app_id,
        official_app_secret: g.official_app_secret,
        command_replies: cr,
        qq_account: g.qq_account,
        notify_on: g.notify_on,
        notify_off: g.notify_off,
        template_on: g.template_on,
        template_off: g.template_off,
        debounce_offline: Number(g.debounce_offline) || 0,
      });
      setG(saved);
      setCmdRows(Object.entries(saved.command_replies || {})
        .map(([k, v]) => ({ k, v })));
      toast("通知配置已保存(通知器 60 秒内热生效;指令回复即时生效)");
    } catch (e: any) {
      toast("保存失败: " + e.message);
    } finally {
      setSaving(false);
    }
  }

  async function testConn() {
    setTesting(true);
    setTestMsg("");
    try {
      const r = await api<{ online: boolean; account: number | string | null;
                            message: string }>(
        "/api/qq-notify/status");
      setTestMsg((r.online ? "✅ " : "⚠️ ") + r.message);
    } catch (e: any) {
      setTestMsg("⚠️ 探测失败: " + e.message);
    } finally {
      setTesting(false);
    }
  }

  const official = g?.channel === "official";

  // 切到官方通道即载入已绑定映射列表
  useEffect(() => {
    if (official) loadMap();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [official]);

  // ---- 群映射获取(v2.3.1 official) ----
  async function loadMap() {
    try {
      const r = await api<{ items: { group_id: number; group_openid: string;
                                     updated_at: string }[] }>(
        "/api/qq-notify/group-map");
      setMapItems(r.items);
    } catch { /* 列表加载失败不打扰(绑定结果另有提示) */ }
  }

  async function startBind() {
    const gid = +bindGid;
    if (!gid || gid <= 0) {
      toast("请输入目标 QQ 群号");
      return;
    }
    setBinding(true);
    setBindMsg("等待群事件…请在目标群 @机器人 发任意消息,或由群主把机器人拉进群(2 分钟内有效)");
    try {
      await api("/api/qq-notify/bind-group", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ group_id: gid }),
      });
      // 2.5s 轮询绑定状态直到终态
      if (bindTimer.current) window.clearInterval(bindTimer.current);
      bindTimer.current = window.setInterval(async () => {
        try {
          const s = await api<{ state: string; openid: string; message: string }>(
            "/api/qq-notify/bind-status");
          if (s.state === "waiting") return;
          window.clearInterval(bindTimer.current!);
          bindTimer.current = null;
          setBinding(false);
          if (s.state === "captured") {
            setBindMsg(`✅ 群 ${gid} 绑定成功(${s.openid.slice(0, 10)}…),房间配置填群号即可直达`);
            loadMap();
          } else {
            setBindMsg("❌ " + (s.message || `绑定未完成(${s.state})`));
          }
        } catch { /* 单次状态查询失败继续轮询 */ }
      }, 2500);
    } catch (e: any) {
      setBinding(false);
      setBindMsg("❌ 发起绑定失败: " + e.message);
    }
  }

  async function unbind(gid: number) {
    try {
      await api(`/api/qq-notify/group-map/${gid}`, { method: "DELETE" });
      toast(`群 ${gid} 已解绑(该群号的房间通知将无法发送,直至重新绑定)`);
      loadMap();
    } catch (e: any) {
      toast("解绑失败: " + e.message);
    }
  }

  return (
    <div className="card card-pad manage-block">
      <h3 className="block-title">开播账号管理</h3>
      <div className="nf-hint-line">
        供 live-qq-notify 通知服务读取(每 60 秒热加载)。
        {official
          ? <>官方通道:凭据在 <a className="nf-link" href="https://q.qq.com"
              target="_blank" rel="noreferrer">QQ 开放平台</a> 管理端「开发设置」
             查看;额度单群 1000 条/天,无协议端被风控踢线问题。</>
          : <>NapCat/WebUI 连接参数使用服务器本地默认值(127.0.0.1),无需配置;
             QQ 号为<strong>预留项</strong>:用于校验实际登录账号、防止登错号
             发错群,可留空。</>}
      </div>
      {!g ? <div className="empty" style={{ padding: "20px 0" }}>加载中…</div> : (
        <div className="nf-admin-form">
          <div className="nf-row3">
            <label className="nf-field">
              <span className="nf-label">发送通道</span>
              <select value={g.channel}
                      onChange={(e) => {
                        set("channel", e.target.value);
                        // 离开 NapCat 通道即停扫码轮询(official 无扫码概念)
                        if (e.target.value !== "napcat") stopQr();
                      }}>
                <option value="napcat">NapCat 协议端(扫码)</option>
                <option value="official">官方机器人 API(推荐)</option>
              </select>
            </label>
            {official ? (
              <label className="nf-field">
                <span className="nf-label">AppID</span>
                <input value={g.official_app_id}
                       onChange={(e) => set("official_app_id", e.target.value)}
                       placeholder="机器人 AppID" />
              </label>
            ) : (
              <label className="nf-field">
                <span className="nf-label">QQ 号(预留)</span>
                <input value={g.qq_account}
                       onChange={(e) => set("qq_account", e.target.value)}
                       placeholder="通知小号 QQ 号,可留空" />
              </label>
            )}
            <div className="nf-field">
              <span className="nf-label">连接状态</span>
              <button className="btn" disabled={testing} onClick={testConn}>
                {testing ? "探测中…" : "测试连接"}
              </button>
            </div>
          </div>
          {official ? (
            <>
              <label className="nf-field">
                <span className="nf-label">AppSecret</span>
                <input value={g.official_app_secret} type="password"
                       onChange={(e) => set("official_app_secret", e.target.value)}
                       placeholder="机器人密钥(管理端「开发设置」)" />
              </label>
              <div className="nf-probe">
                <div className="nf-label">群映射获取</div>
                <div className="nf-hint-line" style={{ margin: 0 }}>
                  官方通道下房间通知<strong>仍填 QQ 群号</strong>,发送时经这里的映射表
                  自动换算 openid。输入群号并点「开始绑定」后,按提示在目标群做一个
                  动作即可自动完成——<strong>这是平台机制要求的唯一步骤</strong>
                  (openid 只能从机器人事件获取):机器人已在群里就<strong> @机器人
                  发任意消息</strong>;还没进群就由<strong>群主把机器人拉进群</strong>。
                  绑定一次永久有效;获取期间请勿在其他群操作机器人。
                </div>
                <div className="nf-probe-bar">
                  <input type="number" value={bindGid}
                         onChange={(e) => setBindGid(e.target.value)}
                         placeholder="目标 QQ 群号" />
                  <button className="btn primary" disabled={binding}
                          onClick={startBind}>
                    {binding ? "绑定中…" : "开始绑定"}
                  </button>
                </div>
                {bindMsg && (
                  <div className={"nf-qrmsg " +
                    (bindMsg.startsWith("✅") ? "ok" : "err")}>{bindMsg}</div>
                )}
                {mapItems.length > 0 && (
                  <div className="nf-probe-results">
                    {mapItems.map((m) => (
                      <div key={m.group_id} className="nf-maprow">
                        <span>群 {m.group_id}</span>
                        <span className="nf-mapoid" title={m.group_openid}>
                          {m.group_openid.slice(0, 10)}…({m.updated_at.slice(0, 16)})
                        </span>
                        <button className="btn sm danger"
                                onClick={() => unbind(m.group_id)}>解绑</button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
              <div className="nf-probe">
                <div className="nf-label">指令自动回复</div>
                <div className="nf-hint-line" style={{ margin: 0 }}>
                  群内 @机器人 + 左侧指令 → 自动回复右侧内容(指令面板的斜杠指令
                  同样生效);每群每分钟限回 5 次。编辑后点下方「保存配置」即时生效;
                  <strong>清空全部并保存 = 恢复默认</strong>(默认仅 /连接)。
                </div>
                {cmdRows.map((row, i) => (
                  <div className="nf-cmdrow" key={i}>
                    <input className="nf-cmd-k" value={row.k}
                           placeholder="/指令"
                           onChange={(e) => setCmdRows(cmdRows.map((r, j) =>
                             j === i ? { ...r, k: e.target.value } : r))} />
                    <input className="nf-cmd-v" value={row.v}
                           placeholder="回复的消息"
                           onChange={(e) => setCmdRows(cmdRows.map((r, j) =>
                             j === i ? { ...r, v: e.target.value } : r))} />
                    <button className="btn sm danger nf-cmd-x"
                            title="移除此行"
                            onClick={() => setCmdRows(cmdRows.filter((_, j) => j !== i))}>
                      ×
                    </button>
                  </div>
                ))}
                <div>
                  <button className="btn"
                          onClick={() => setCmdRows([...cmdRows, { k: "", v: "" }])}>
                    ＋ 添加指令
                  </button>
                </div>
              </div>
            </>
          ) : (
            <>
              <div className="nf-row3">
                <div className="nf-field">
                  <span className="nf-label">QQ 扫码登录</span>
                  {!qrOn ? (
                    <button className="btn" onClick={startQr}>开始扫码登录</button>
                  ) : (
                    <button className="btn ghost" onClick={stopQr}>停止</button>
                  )}
                </div>
              </div>
              {(qrOn || qrState) && (
                <div className="nf-qrbox">
                  {qrState?.phase === "online" ? (
                    <div className="nf-qrmsg ok">✅ QQ 已登录(可点「测试连接」核对账号)</div>
                  ) : qrState?.phase === "error" ? (
                    <div className="nf-qrmsg err">⚠️ {qrState.message}</div>
                  ) : qrData ? (
                    <>
                      <img className="nf-qrimg" src={qrData} alt="QQ 登录二维码" />
                      <div className="nf-qrmsg">
                        {qrState?.phase === "scanned"
                          ? "已扫码,请在手机上确认登录…"
                          : "请用小号手机 QQ 扫码登录(码每 90 秒自动刷新,请尽快扫)"}
                      </div>
                    </>
                  ) : (
                    <div className="nf-qrmsg">二维码加载中…</div>
                  )}
                </div>
              )}
            </>
          )}
          <div className="nf-row3">
            <div className="nf-field">
              <span className="nf-label">开播通知(全局)</span>
              <button className={"nf-sw" + (g.notify_on ? " on" : "")}
                      role="switch" aria-checked={g.notify_on}
                      onClick={() => set("notify_on", !g.notify_on)}>
                <span className="nf-knob" />
              </button>
            </div>
            <div className="nf-field">
              <span className="nf-label">下播通知(全局)</span>
              <button className={"nf-sw" + (g.notify_off ? " on" : "")}
                      role="switch" aria-checked={g.notify_off}
                      onClick={() => set("notify_off", !g.notify_off)}>
                <span className="nf-knob" />
              </button>
            </div>
            <label className="nf-field">
              <span className="nf-label">下播防抖(秒)</span>
              <input type="number" min={0} max={86400} value={g.debounce_offline}
                     onChange={(e) => set("debounce_offline", +e.target.value)} />
            </label>
          </div>
          <label className="nf-field">
            <span className="nf-label nf-tip"
                  title={"可用变量(悬浮查看):\n{uname} = 主播名\n{title} = 直播标题\n{url} = 直播间链接\n{cover} = 封面图(写在文案里才会附图;删掉则纯文本)"}>
              全局开播文案 ⓘ
            </span>
            <textarea rows={3} value={g.template_on}
                      onChange={(e) => set("template_on", e.target.value)} />
          </label>
          <label className="nf-field">
            <span className="nf-label nf-tip"
                  title={"可用变量(悬浮查看):\n{uname} = 主播名\n{title} = 直播标题(下播时可能为空)\n{url} = 直播间链接\n{duration} = 本次直播时长(写在文案里才会显示;删掉则不带)"}>
              全局下播文案 ⓘ
            </span>
            <textarea rows={3} value={g.template_off}
                      onChange={(e) => set("template_off", e.target.value)} />
          </label>
          <div className="nf-admin-actions">
            <button className="btn primary" disabled={saving} onClick={save}>
              {saving ? "保存中…" : "保存配置"}
            </button>
            {testMsg && <span className="nf-testmsg">{testMsg}</span>}
          </div>
        </div>
      )}
    </div>
  );
}
