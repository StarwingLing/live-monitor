import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 部署形态：Nginx 前缀反代 /bili-monitor/ → 127.0.0.1:8001（保留前缀，
// 配合 FastAPI root_path=/bili-monitor）——生产 base 固定。
export default defineConfig({
  base: "/bili-monitor/",
  plugins: [react()],
  server: {
    port: 5174,
    proxy: {
      // base 为 /bili-monitor/ 时页面发的 API 请求带前缀，须单列（rewrite 剥
      // 前缀——与生产 Nginx 前缀剥离的 proxy_pass 等价）；裸 /api 键保留
      // 供无 base 直连场景，两者互不冲突
      "/bili-monitor/api": {
        target: "http://127.0.0.1:8001",
        rewrite: (p) => p.replace(/^\/bili-monitor/, ""),
      },
      "/bili-monitor/recordings": {
        target: "http://127.0.0.1:8001",
        rewrite: (p) => p.replace(/^\/bili-monitor/, ""),
      },
      "/api": "http://127.0.0.1:8001",
      "/recordings": "http://127.0.0.1:8001",
    },
  },
  build: {
    // 3 Mbps 出向对策：echarts 按需注册（见 src/echarts.ts）+ 路由懒加载；
    // ThreeUI 背景组件只进总览 hero chunk
    rollupOptions: {
      output: {
        manualChunks: {
          vendor: ["react", "react-dom", "react-router"],
        },
      },
    },
  },
});
