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
      // 花语智能体 API（FastAPI, apps/api）
      "/api": {
        target: "http://127.0.0.1:8808",
        changeOrigin: true,
      },
    },
  },
});
