import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  use: {
    baseURL: "http://localhost:8888",
  },
  webServer: {
    // 一键起 FastAPI + Vite（与 pnpm demo 同一入口）
    command: "node scripts/demo.mjs",
    url: "http://localhost:8888",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
