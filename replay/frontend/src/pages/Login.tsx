// 统一登录页（三角色 + 滑块 tab + 星座连线背景·自托管无外部 CDN）。
// 页面语义：到达即「已退出所有角色」（服务端 /login 路由清 Cookie + 抑制
// SSO 续登），SPA 恒为选择身份登录表单——与 v0.1.2 定稿一致。
import { useEffect, useRef, useState } from "react";
import { BASE, postJson } from "../api";
import { toast } from "../ui";
import ConstellationBg from "../fx/ConstellationBg";
type Mode = "admin" | "clipper" | "guest";
const MODES: { key: Mode; label: string }[] = [
  { key: "clipper", label: "切片员" },
  { key: "guest", label: "游客" },
  { key: "admin", label: "管理员" },
];

export default function Login() {
  const [mode, setMode] = useState<Mode>("admin");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [nickname, setNickname] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const thumbRef = useRef<HTMLDivElement>(null);
  const tabsRef = useRef<HTMLDivElement>(null);
  const [Bg, setBg] = useState<React.ComponentType<{ className: string }> | null>(null);
  const [bgOn, setBgOn] = useState(false);

  // 背景仅桌面 + 非 reduced-motion + WebGL 可用时加载
  useEffect(() => {
    const mq = matchMedia("(prefers-reduced-motion: reduce)");
    const narrow = matchMedia("(max-width: 560px)");
    const canGl = (() => {
      try {
        const c = document.createElement("canvas");
        return !!(c.getContext("webgl") || c.getContext("experimental-webgl"));
      } catch {
        return false;
      }
    })();
    if (mq.matches || narrow.matches || !canGl) return;
    // 星座背景（v1.3：threeui 组件外链 tailwind/iconify/gsap 境内不稳，
    // 裁剪为无外部依赖的自托管版）；降级链不变
    setBg(() => ConstellationBg);
    setBgOn(true);
  }, []);

  // 滑块定位（首次无过渡）
  const placed = useRef(false);
  useEffect(() => {
    const tabs = tabsRef.current;
    const thumb = thumbRef.current;
    if (!tabs || !thumb) return;
    const btn = tabs.querySelector<HTMLButtonElement>(
      `button[data-mode="${mode}"]`);
    if (!btn) return;
    if (!placed.current) {
      thumb.style.transition = "none";
      thumb.style.left = btn.offsetLeft + "px";
      thumb.style.width = btn.offsetWidth + "px";
      void thumb.offsetWidth;
      thumb.style.transition = "";
      placed.current = true;
      return;
    }
    thumb.style.left = btn.offsetLeft + "px";
    thumb.style.width = btn.offsetWidth + "px";
  }, [mode]);
  useEffect(() => {
    const mv = () => {
      window.dispatchEvent(new Event("resize"));
    };
    window.addEventListener("resize", mv);
    return () => window.removeEventListener("resize", mv);
  }, []);

  async function submit(ev: React.FormEvent) {
    ev.preventDefault();
    if (busy) return;
    const body: Record<string, string> = { mode };
    if (mode !== "admin") {
      if (!code.trim()) {
        toast("请输入监控码");
        return;
      }
      body.code = code.trim();
    }
    if (mode === "admin") {
      if (!username.trim() || !password) {
        toast("请填写管理员账号与密码");
        return;
      }
      body.username = username.trim();
      body.password = password;
    } else if (mode === "clipper") {
      if (!password) {
        toast("请填写切片员密码");
        return;
      }
      if (!nickname.trim()) {
        toast("请填写昵称");
        return;
      }
      body.password = password;
      body.nickname = nickname.trim();
    }
    setBusy(true);
    try {
      const r = await postJson<{ redirect?: string }>("/api/auth/login", body);
      location.href = r.redirect || BASE;
    } catch (e: any) {
      toast(e.message || "登录失败");
      setBusy(false);
    }
  }

  return (
    <div className="lg-wrap">
      {Bg && bgOn ? <Bg className="lg-bg" /> : <div className="lg-bg-fallback" />}
      <div className="lg-card">
        <div className="lg-title">直播回放</div>
        <div className="lg-sub">选择身份登录 · 输入监控码进入对应主播的回放</div>
        <div className="lg-tabs" ref={tabsRef}>
          <div className="lg-thumb" ref={thumbRef} />
          {MODES.map((m) => (
            <button
              key={m.key}
              data-mode={m.key}
              className={mode === m.key ? "on" : ""}
              onClick={() => setMode(m.key)}
              type="button"
            >
              {m.label}
            </button>
          ))}
        </div>
        <form onSubmit={submit}>
          <div id="lgBody" key={mode} className="lg-enter">
            {mode === "admin" && (
              <>
                <input
                  className="lg-input" placeholder="管理员账号"
                  autoComplete="username" value={username}
                  onChange={(e) => setUsername(e.target.value)} />
                <input
                  className="lg-input" type="password" placeholder="密码"
                  autoComplete="current-password" value={password}
                  onChange={(e) => setPassword(e.target.value)} />
              </>
            )}
            {mode === "clipper" && (
              <>
                <input
                  className="lg-input" type="password" placeholder="切片员密码"
                  autoComplete="current-password" value={password}
                  onChange={(e) => setPassword(e.target.value)} />
                <input
                  className="lg-input" placeholder="你的昵称（切片/注释署名）"
                  maxLength={16} autoComplete="off" value={nickname}
                  style={{ marginTop: 10 }}
                  onChange={(e) => setNickname(e.target.value)} />
              </>
            )}
            {mode !== "admin" && (
              <>
                <div className="lg-code-cap">
                  <span className="rule" /><span className="tt">监控码</span>
                  <span className="rule" />
                </div>
                <input
                  className="lg-input lg-code" placeholder="r1234567-xxxxxxxxxx"
                  autoComplete="off" autoCapitalize="off" spellCheck={false}
                  value={code} onChange={(e) => setCode(e.target.value)} />
              </>
            )}
          </div>
          <button type="submit" className="lg-btn" disabled={busy}>
            {busy ? "进入中…" : "进入回放"}
          </button>
        </form>
      </div>
    </div>
  );
}
