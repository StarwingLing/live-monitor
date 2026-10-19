// 管理页（仅管理员）：v2.3.0 起分两个同级 tab——
// - 回放管理（v1.0.0 原页）：切片员密码 / 监控码 / 自动全程录制开关 /
//   房间名单增删（v2.0.0：项目独立后自管，短号自动解析、添加即生成监控码）；
// - 账号管理（v2.3.0 新增，v2.4.0 简化）：投稿账号凭据与状态——登录操作
//   仅保留扫码登录（粘贴/文件读取/renew/清除/合集预览等入口均已移除，
//   扫码确认后由后端自动 renew 补 access_token）。
// 回放管理 30s 轮询刷新；复制走 Clipboard API + execCommand 降级（公网
// HTTP 场景）。
import { useCallback, useEffect, useRef, useState } from "react";
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

// ---- 投稿账号管理数据形状（v2.3.0；v2.4.0 暴露配置清单）----
interface AcctStatus {
  bin_configured: boolean;
  bin_path: string;            // [bili].biliup_bin 实际值（空 = 未配置）
  cookies_path: string;        // 凭据文件路径
  cookies_present: boolean;
  submit_enabled: boolean;     // 二进制 + 凭据文件双就位
  upload_ready: boolean;       // false = 缺 access_token，新视频投稿不可用
  cookie_ok: boolean | null;
  account: { ok: boolean; uname: string; mid: number; face: string } | null;
  error: string;
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
  const [tab, setTab] = useState<"rooms" | "account">("rooms");
  return (
    <div className="ad-page">
      <div className="ad-head">
        <div className="t1">回放管理</div>
        <div className="ad-tabs">
          <button className={tab === "rooms" ? "on" : ""}
                  onClick={() => setTab("rooms")}>回放管理</button>
          <button className={tab === "account" ? "on" : ""}
                  onClick={() => setTab("account")}>账号管理</button>
        </div>
        <div className="ops">
          <button className="btn sm" onClick={() => void logout()}>退出登录</button>
        </div>
      </div>
      {tab === "rooms" ? <RoomsPanel /> : <AccountPanel />}
    </div>
  );
}

// ===========================================================================
// 回放管理 tab（v1.0.0 原管理页内容原样迁入）
// ===========================================================================
function RoomsPanel() {
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
      toast(e.message);
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
    <>
      <div className="ad-subhead">
        房间名单 · 切片员密码 · 监控码 · 自动全程录制（开关约 30 秒内生效）
      </div>
      <div className="ad-add-row">
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
        <div className="empty">暂无监控房间，在上方输入房间号添加</div>
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
    </>
  );
}

// ===========================================================================
// 账号管理 tab（v2.3.0 新增；v2.4.0 简化：登录操作仅保留扫码登录）
// ===========================================================================
function AccountPanel() {
  const [st, setSt] = useState<AcctStatus | null>(null);
  // v2.4.0 扫码登录：二维码会话 + 轮询状态
  const [qr, setQr] = useState<{ key: string; svg: string } | null>(null);
  const [qrState, setQrState] = useState<"waiting" | "scanned" | "expired"
    | "confirmed">("waiting");
  const [qrCookie, setQrCookie] = useState("");
  const qrTimer = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      setSt(await api<AcctStatus>("/api/admin/submit/account"));
    } catch (e: any) {
      toast("账号状态加载失败：" + e.message, "err");
    }
  }, []);

  useEffect(() => { refresh(); }, [refresh]);

  // ---- v2.4.0 扫码登录：生成二维码 → 2s 轮询 → 确认落盘 ----
  const stopQrPoll = useCallback(() => {
    if (qrTimer.current !== null) {
      window.clearInterval(qrTimer.current);
      qrTimer.current = null;
    }
  }, []);

  useEffect(() => () => stopQrPoll(), [stopQrPoll]);

  async function pollQr(key: string) {
    try {
      const r = await api<{
        state: string; account: { uname: string } | null;
        cookie_str: string; upload_ready: boolean; error: string;
        renew_note: string;
      }>(`/api/admin/submit/qr/poll?qrcode_key=${encodeURIComponent(key)}`);
      if (r.state === "waiting") { setQrState("waiting"); return; }
      if (r.state === "scanned") { setQrState("scanned"); return; }
      if (r.state === "expired") { setQrState("expired"); stopQrPoll(); return; }
      if (r.state === "confirmed") {
        stopQrPoll();
        setQrState("confirmed");
        setQrCookie(r.cookie_str || "");
        // v2.4.0：确认后后端已自动 renew 补 access_token——完整凭据即
        // 可投稿新视频；renew 失败才提示受限
        toast(r.account?.uname
          ? `扫码登录成功：${r.account.uname}` +
              (r.upload_ready ? "" : "（access_token 未获得，新视频投稿受限"
                + (r.renew_note ? "：" + r.renew_note : "") + "）")
          : "登录凭据已保存" + (r.error ? "，验证失败：" + r.error : ""),
          r.account ? "ok" : "err");
        refresh();
        return;
      }
    } catch (e: any) {
      stopQrPoll();
      setQrState("expired");
      toast("扫码轮询失败：" + e.message, "err");
    }
  }

  async function startQr() {
    stopQrPoll();
    setQrCookie("");
    try {
      const r = await postJson<{ qrcode_key: string; svg: string }>(
        "/api/admin/submit/qr/start", {});
      setQr({ key: r.qrcode_key, svg: r.svg });
      setQrState("waiting");
      qrTimer.current = window.setInterval(() => { void pollQr(r.qrcode_key); },
                                           2000);
    } catch (e: any) {
      toast("生成二维码失败：" + e.message, "err");
    }
  }

  const acct = st?.account;

  return (
    <>
      <div className="ad-subhead">
        投稿账号 · 凭据与状态（回放页「投稿」按钮使用的 B 站账号）
      </div>

      {/* ---- 状态卡：投稿配置清单（v2.4.0 逐项状态 + 未就绪说明） ---- */}
      <div className="card pw-card acct-card">
        <div className="acct-cap">投稿配置</div>

        <div className="acct-row">
          <span className="cap">① 上传程序 biliup</span>
          {st === null ? "加载中…" : st.bin_configured
            ? <span className="badge cut">已配置</span>
            : <span className="badge off">未配置</span>}
          <span className="pw-note cfg-path">
            {st?.bin_path || "config.toml [bili].biliup_bin 未设置"}
          </span>
        </div>
        {st !== null && !st.bin_configured && (
          <div className="acct-bad">
            服务器一次性部署项：安装 biliup 二进制并把路径写入服务器
            config.toml 的 [bili].biliup_bin（步骤见项目 deploy/README
            第 8 章「投稿环境搭建」），改完重启 replay-web。
          </div>
        )}

        <div className="acct-row">
          <span className="cap">② 凭据文件</span>
          {st === null ? "加载中…" : st.cookies_present
            ? <span className="badge cut">已就位</span>
            : <span className="badge off">缺失</span>}
          <span className="pw-note cfg-path">{st?.cookies_path || "…"}</span>
        </div>

        <div className="acct-row">
          <span className="cap">③ 投稿账号</span>
          {!st?.cookies_present ? (
            <span className="pw-note">未登录（用下方二维码扫码登录）</span>
          ) : acct && acct.ok ? (
            <>
              <span className="acct-face">
                {AVATAR_PH}
                {acct.face && (
                  <img src={acct.face} alt="" loading="lazy"
                       onError={(e) => (e.target as HTMLImageElement).remove()} />
                )}
              </span>
              <span className="acct-name">{acct.uname}</span>
              <span className="rc-id">UID {acct.mid}</span>
            </>
          ) : (
            <span className="acct-bad">
              凭据已失效{st?.error ? "：" + st.error : ""}（重新扫码登录）
            </span>
          )}
        </div>

        <div className="acct-row">
          <span className="cap">④ access_token</span>
          {st === null ? "加载中…" : st.upload_ready
            ? <span className="badge cut">完整</span>
            : <span className="badge off">缺失</span>}
          <span className="pw-note">
            {st?.upload_ready
              ? "可投稿新视频 / 追加分P"
              : "仅追加分P可用——扫码登录确认后自动续期+兑换补齐"}
          </span>
        </div>

        <div className="acct-ops">
          <button className="btn sm" onClick={() => void refresh()}>刷新状态</button>
        </div>
      </div>

      {/* ---- 投稿配置说明卡（v2.4.0） ---- */}
      <div className="card pw-card acct-card">
        <div className="acct-cap">说明</div>
        <div className="pw-note" style={{ display: "block", lineHeight: 1.8 }}>
          投稿需要三件事就绪：<br />
          ① <b>biliup 上传程序</b>——服务器一次性安装并把路径写入
          config.toml 的 [bili].biliup_bin（详见项目 deploy/README 第 8 章
          「投稿环境搭建」，改完重启 replay-web）；<br />
          ② <b>投稿账号登录</b>——用下方二维码扫码登录，凭据自动保存，确认后
          自动续期并兑换补齐 access_token（新视频投稿所需凭据）；
          凭据失效时重新扫码即可；<br />
          ③ <b>上传执行</b>——网页提交后由「本地投稿器」认领上传（家庭宽带
          快路径，见 deploy/README 第 9 章）；投稿器未开时服务器 3Mbps
          慢路径自动兜底，无需额外配置。
        </div>
      </div>

      {/* ---- v2.4.0 扫码登录卡（简化后唯一登录操作） ---- */}
      <div className="card pw-card acct-card">
        <div className="acct-cap">扫码登录（B 站手机客户端）</div>
        <div className="pw-note" style={{ display: "block", marginBottom: 8 }}>
          生成二维码 → 手机扫码确认 → 凭据自动保存并补全（含新视频投稿所需
          凭据，确认后自动续期）。
        </div>
        {!qr ? (
          <div className="acct-ops">
            <button className="btn sm primary" onClick={() => void startQr()}>
              生成二维码登录
            </button>
          </div>
        ) : (
          <div className="qr-box">
            {qr.svg ? (
              <img className="qr-img" src={qr.svg} alt="B 站登录二维码"
                   onClick={() => void startQr()} title="点击重新生成" />
            ) : (
              <div className="pw-note">（二维码图生成失败，请点击按钮重试）</div>
            )}
            <div className="acct-row">
              <span className="pw-note">
                {qrState === "waiting" && "请用 B 站手机客户端扫码"}
                {qrState === "scanned" && "已扫码，请在手机上确认"}
                {qrState === "expired" && "二维码已过期"}
                {qrState === "confirmed" && "登录完成"}
              </span>
              {(qrState === "expired" || qrState === "confirmed") && (
                <button className="btn sm" onClick={() => void startQr()}>
                  {qrState === "confirmed" ? "重新登录" : "刷新二维码"}
                </button>
              )}
            </div>
            {qrState === "confirmed" && qrCookie && (
              <div className="qr-cookie">
                <button className="btn sm"
                        onClick={() => {
                          navigator.clipboard.writeText(qrCookie)
                            .then(() => toast("已复制：可粘贴到服务器 config.toml 的"
                                + " [capture].cookie（采集账号）后重启 replay-capture", "ok"))
                            .catch(() => toast("复制失败（需 HTTPS），请手动复制", "err"));
                        }}>
                  复制 Cookie 串（采集账号配置用）
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </>
  );
}
