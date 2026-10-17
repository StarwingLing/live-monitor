// 路由 + 导航布局。登录页独立布局（无导航）；其余页面套 <Nav>。
import { Suspense, lazy, useEffect, useState } from "react";
import { Link, NavLink, Outlet, Route, Routes, useLocation } from "react-router";
import { logout, fetchMe, type Me } from "./api";

const Login = lazy(() => import("./pages/Login"));
const Index = lazy(() => import("./pages/Index"));
const Room = lazy(() => import("./pages/Room"));
const Analysis = lazy(() => import("./pages/Analysis"));
const Manage = lazy(() => import("./pages/Manage"));

function Loading() {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center",
                  minHeight: "60vh", color: "var(--sub)", fontSize: 13 }}>
      加载中…
    </div>
  );
}

function Nav() {
  const [me, setMe] = useState<Me | null>(null);
  const [open, setOpen] = useState(false);
  const loc = useLocation();
  useEffect(() => {
    fetchMe().then(setMe).catch(() => {});
  }, []);
  useEffect(() => {
    setOpen(false);
  }, [loc.pathname]);
  const admin = me?.role === "admin";
  return (
    <>
      <nav className={"nav" + (open ? " open" : "")}>
        <div className="brand">
          <div className="logo" />
          <div>
            <div className="t1">直播间数据监控</div>
            <div className="t2">LIVE DATA MONITOR</div>
          </div>
        </div>
        <div className="nav-panel">
          <div className="menu">
            <NavLink to="/" end className={({ isActive }) => isActive ? "active" : ""}>总览</NavLink>
            <NavLink to="/analysis" className={({ isActive }) => isActive ? "active" : ""}>数据分析</NavLink>
            {admin && (
              <NavLink to="/manage" className={({ isActive }) => isActive ? "active" : ""}>直播间管理</NavLink>
            )}
          </div>
          <div className="right">
            <div className="status"><span className="dot" />运行中</div>
            <div className="whoami" title="当前登录身份">
              {admin ? `👤 ${me?.username || "admin"}` : "👤 游客"}
            </div>
            <a href="javascript:void(0)" className="logout-link"
               onClick={() => void logout()}>登出</a>
          </div>
        </div>
        <button className="nav-toggle" aria-label="打开菜单" aria-expanded={open}
                onClick={() => setOpen(!open)}>
          <span /><span /><span />
        </button>
      </nav>
      <div className="page-wrap"><Outlet /></div>
    </>
  );
}

export default function App() {
  return (
    <Suspense fallback={<Loading />}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route element={<Nav />}>
          <Route path="/" element={<Index />} />
          <Route path="/room/:roomId" element={<Room />} />
          <Route path="/session/:roomId/:sessionId" element={<Room />} />
          <Route path="/analysis" element={<Analysis />} />
          <Route path="/manage" element={<Manage />} />
          <Route path="*" element={<div className="empty">页面不存在 · <Link to="/">回总览</Link></div>} />
        </Route>
      </Routes>
    </Suspense>
  );
}
