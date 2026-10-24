import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 45_000,
  use: {
    baseURL: "http://localhost:8888",
  },
  webServer: {
    // 一键起 FastAPI + Vite（与 pnpm demo 同一入口）
    // E2E 需要干净的运行库：遗留 run 会占据 worker 队列（FIFO 认领）拖垮断言。
    // db 路径显式固定（macOS tempfile.gettempdir() 是 /var/folders/...，与 /tmp 不同）
    command: "rm -f /tmp/ah-e2e-runs.sqlite /tmp/ah-e2e-checkpoints.sqlite && AGENT_HUB_RUNS_DB=/tmp/ah-e2e-runs.sqlite AGENT_HUB_CHECKPOINTS=/tmp/ah-e2e-checkpoints.sqlite AGENT_HUB_LLM=scripted node scripts/demo.mjs",
    url: "http://localhost:8888",
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
