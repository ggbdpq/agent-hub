import { expect, test } from "@playwright/test";

// 三条最小端到端用例，对应 README 四主线中的三条用户故事：
// 流式分段 → A2UI 卡片 → 证据抽屉 → 任务按 taskId 恢复。
// mock LLM 确定性剧本驱动，无需真实 API Key。

test("对话页：提问后收到流式分段并渲染 A2UI 卡片", async ({ page }) => {
  await page.goto("/");
  const input = page.getByPlaceholder(/说说你的用花场景/);
  await input.fill("想给妈妈送生日礼物");
  await input.press("Enter");

  // 思考分段流式到达（thought 事件 → 可折叠思考面板）
  await expect(page.getByText(/思考过程 · 第 \d+ 段/).first()).toBeVisible();
  // card 事件 → A2UI 推荐卡（gift 场景确定性剧本）
  await expect(page.getByText("送礼首选：轻奢礼盒")).toBeVisible();
  // token 事件 → 正文打字机段落
  await expect(page.getByText(/送礼场景我推荐礼盒款/)).toBeVisible();
});

test("A2UI 卡片：点依据打开证据抽屉并展示可信度分级", async ({ page }) => {
  await page.goto("/");
  const input = page.getByPlaceholder(/说说你的用花场景/);
  await input.fill("日常自用来点花");
  await input.press("Enter");

  // 卡片携带证据引用入口（evidenceId → 依据按钮）
  const evidenceButton = page.getByRole("button", { name: "近 90 天订单记录" });
  await expect(evidenceButton).toBeVisible();
  await evidenceButton.click();

  // 证据抽屉：来源 + 可信度 + 引用原文（结论与依据分离）
  await expect(page.getByRole("heading", { name: "推荐依据" })).toBeVisible();
  await expect(page.getByText(/可信度 88/)).toBeVisible();
  await expect(page.getByText(/近 90 天购买 3 次鲜切花/)).toBeVisible();
});

test("任务页：刷新后按 taskId 从远端恢复", async ({ page }) => {
  await page.goto("/tasks");
  await page.getByRole("button", { name: "创建生成任务" }).click();

  // 任务创建：业务单号出现（taskId 为内部主键不上页面）
  await expect(page.getByText(/单号 YH/)).toBeVisible();

  // 刷新 → localStorage 里的 taskId 作为恢复锚点，远端详情回放
  await page.reload();
  await expect(page.getByText(/单号 YH/)).toBeVisible();
  await expect(page.getByText(/进度 \d+%/)).toBeVisible();
});
