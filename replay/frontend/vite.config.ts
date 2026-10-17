import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 部署形态（SERVER-INFO/AGENTS）：Nginx 前缀反代 /replay/ → 127.0.0.1:8003，
// proxy_pass 不带尾斜杠保留前缀，配合 FastAPI root_path=/replay——因此
// 生产构建 base 固定 '/replay/'（本地 dev 由 vite dev server 直出，base
// 同样生效，API 经 proxy 转发到本地 uvicorn，root_path 为空串）。
export default defineConfig({
  base: "/replay/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 本地开发：页面由 vite 直出（base /replay/），页面发的 API/录像请求
      // 带 /replay 前缀——带前缀键 rewrite 剥前缀转发（与 bili 前端同款
      // 方案；后端 root_path 可为空串）；裸键保留供无 base 直连场景
      "/replay/api": {
        target: "http://127.0.0.1:8003",
        rewrite: (p) => p.replace(/^\/replay/, ""),
      },
      "/replay/recordings": {
        target: "http://127.0.0.1:8003",
        rewrite: (p) => p.replace(/^\/replay/, ""),
      },
      "/api": "http://127.0.0.1:8003",
      "/recordings": "http://127.0.0.1:8003",
    },
  },
  build: {
    // 服务器出向 3 Mbps：非登录页首屏 gzip 目标 ≤200KB——路由级 lazy
    // 分包（见 App.tsx），threeui 背景组件只进登录 chunk
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "react-router"],
        },
      },
    },
  },
});
