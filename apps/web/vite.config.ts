import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(import.meta.dirname, "src"),
      "@shared": path.resolve(import.meta.dirname, "../../packages/shared/src"),
    },
  },
  server: {
    port: 8888,
    proxy: {
      // 智能体对话与证据由 TS 服务（apps/agent :8809）负责
      "/api/agent": {
        target: "http://127.0.0.1:8809",
        changeOrigin: true,
      },
      "/api/evidence": {
        target: "http://127.0.0.1:8809",
        changeOrigin: true,
      },
      // 其余路由（任务/订单/支付/场景）由 FastAPI（apps/api :8808）负责
      "/api": {
        target: "http://127.0.0.1:8808",
        changeOrigin: true,
      },
    },
  },
});
