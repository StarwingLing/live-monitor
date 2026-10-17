import { Suspense, lazy } from "react";
import { Navigate, Route, Routes } from "react-router";

// 路由级懒加载（3 Mbps 出向对策）：登录页含 ThreeUI 背景组件，独立 chunk
const Login = lazy(() => import("./pages/Login"));
const Admin = lazy(() => import("./pages/Admin"));
const Replay = lazy(() => import("./pages/Replay"));

function Loading() {
  return (
    <div style={{
      display: "flex", alignItems: "center", justifyContent: "center",
      minHeight: "60vh", color: "var(--sub)", fontSize: 13,
    }}>
      加载中…
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<Loading />}>
      <Routes>
        <Route path="/login" element={<Login />} />
        <Route path="/manage" element={<Admin />} />
        <Route path="/:roomId/replay" element={<Replay />} />
        <Route path="*" element={<Navigate to="/login" replace />} />
      </Routes>
    </Suspense>
  );
}
