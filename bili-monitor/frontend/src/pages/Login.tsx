// 登录页（管理员/游客双模式）+ 星座连线背景（自托管，无外部 CDN）。
// next 安全回跳同旧版。
import { useEffect, useState } from "react";
import { BASE } from "../api";
import { toast } from "../ui";
import ConstellationBg from "../fx/ConstellationBg";

export default function Login() {
  const [mode, setMode] = useState<"admin" | "guest">("admin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [Bg, setBg] = useState<React.ComponentType<{ className: string }> | null>(null);

  useEffect(() => {
    const mq = matchMedia("(prefers-reduced-motion: reduce)");
    const narrow = matchMedia("(max-width: 560px)");
    try {
      const c = document.createElement("canvas");
      if (mq.matches || narrow.matches ||
          !(c.getContext("webgl") || c.getContext("experimental-webgl"))) return;
    } catch { return; }
    // 自托管星座背景（v1.6：threeui 组件外链 tailwind/iconify/gsap 等
    // 境内不稳，裁剪为无外部依赖版）；降级链不变
    setBg(() => ConstellationBg);
  }, []);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (busy) return;
    const body = mode === "admin"
      ? { mode, username: username.trim(), password }
      : { mode, code: code.trim() };
    if (mode === "admin" && (!body.username || !password)) {
      toast("请填写管理员账号与密码");
      return;
    }
    if (mode === "guest" && !code.trim()) {
      toast("请输入游客码");
      return;
    }
    setBusy(true);
    try {
      const r = await fetch(BASE.endsWith("/") ? BASE + "api/auth/login" : BASE + "/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (!r.ok) {
        const j = await r.json().catch(() => ({}) as any);
        throw new Error(j.detail || "登录失败");
      }
      // 优先回跳来源页（middleware 302 时带 ?next=；仅站内路径，防开放跳转）
      const next = new URLSearchParams(location.search).get("next") || "/";
      const safe = next.startsWith("/") && !next.startsWith("//") ? next : "/";
      location.href = BASE.replace(/\/$/, "") + safe;
    } catch (e: any) {
      toast(e.message);
      setBusy(false);
    }
  }

  return (
    <div className="lg-wrap">
      {Bg ? <Bg className="lg-bg" /> : <div className="lg-bg-fallback" />}
      <div className="lg-card">
        <div className="lg-title">直播间数据监控</div>
        <div className="lg-sub">LIVE DATA MONITOR · 管理员或游客码登录</div>
        <div className="lg-tabs">
          <div className="lg-thumb" style={{ left: 4, width: "calc(50% - 7px)" }} />
          <button className={mode === "admin" ? "on" : ""} type="button"
                  onClick={() => setMode("admin")}>管理员登录</button>
          <button className={mode === "guest" ? "on" : ""} type="button"
                  onClick={() => setMode("guest")}>游客访问</button>
        </div>
        <form onSubmit={submit}>
          <div id="lgBody" key={mode} className="lg-enter">
            {mode === "admin" ? (
              <>
                <input className="lg-input" placeholder="管理员账号"
                       autoComplete="username" value={username}
                       onChange={(e) => setUsername(e.target.value)} />
                <input className="lg-input" type="password" placeholder="密码"
                       autoComplete="current-password" value={password}
                       onChange={(e) => setPassword(e.target.value)} />
              </>
            ) : (
              <>
                <input className="lg-input" placeholder="游客码" autoComplete="off"
                       value={code} onChange={(e) => setCode(e.target.value)} />
                <div style={{ fontSize: 11.5, color: "var(--faint)", marginTop: 10 }}>
                  游客码由管理员提供，登录后仅可浏览数据
                </div>
              </>
            )}
          </div>
          <button type="submit" className="lg-btn" disabled={busy}>
            {busy ? "登 录 中…" : "登 录"}
          </button>
        </form>
      </div>
    </div>
  );
}
