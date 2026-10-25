import { expect, test } from "@playwright/test";

/**
 * 演示录屏（v0.2.0 Release 资产制作入口，默认跳过）。
 *
 * 运行：DEMO_RECORD=1 no_proxy=localhost,127.0.0.1 ./node_modules/.bin/playwright
 *       test -c playwright.record.config.ts e2e/demo-recording.spec.ts
 * 产物：test-results 下逐用例 video.webm（供 Release 附件 / 转 GIF）。
 */

test.skip(!process.env.DEMO_RECORD, "录屏专用：需 DEMO_RECORD=1 显式启用");

test("录屏：巡检审批流（发起 → 时间线 → 审批 → 报告）", async ({ page }) => {
  await page.goto("/runs");
  await page.waitForTimeout(800); // 片头稳定帧
  await page.getByLabel(/设备 ID/).fill("INV-001, INV-005, INV-002");
  await page.getByRole("button", { name: "发起巡检" }).click();
  // 时间线推进（留出可读节奏）
  await expect(page.getByText(/规划完成/)).toBeVisible({ timeout: 20_000 });
  await page.waitForTimeout(1_200);
  await expect(page.getByText("批准派无人机")).toBeVisible({ timeout: 20_000 });
  await page.waitForTimeout(1_500);
  await page.getByRole("button", { name: "批准派无人机" }).click();
  await expect(page.getByText("巡检报告")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(2_500); // 片尾停留展示报告
});

test("录屏：demo_crash 终端回放（kill -9 → 回收 → 续跑 → 完成）", async ({ page }) => {
  // 终端回放页由 scripts/e2e-restart/render-terminal.mjs 从 demo_crash 实际输出生成
  // （录制资产制作，不进 CI 默认路径）
  await page.goto("file:///tmp/ah-demo-crash.html");
  await page.waitForTimeout(1_000);
  // 等待打字机回放完成（页面 body 标记 data-done）
  await page.waitForSelector("body[data-done]", { timeout: 120_000 });
  await page.waitForTimeout(2_000);
});
