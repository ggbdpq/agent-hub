import { defineConfig } from "@playwright/test";
import base from "./playwright.config";

// 录屏专用配置（Release 资产制作）：继承主 config，仅开启逐用例视频录制。
// 运行：DEMO_RECORD=1 no_proxy=localhost,127.0.0.1 ./node_modules/.bin/playwright
//       test -c playwright.record.config.ts e2e/demo-recording.spec.ts
export default defineConfig({
  ...base,
  use: {
    ...base.use,
    video: "on",
  },
  outputDir: "./test-results/recording",
});
