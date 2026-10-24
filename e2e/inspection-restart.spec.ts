import { expect, test } from "@playwright/test";
import {
  ApiProcess,
  cleanupDb,
  collectRunEvents,
  dbPaths,
  ledgerRows,
  startWeb,
  WEB_ORIGIN,
} from "../scripts/e2e-restart/stack.mjs";

/**
 * 进程重启 E2E（Y1/Y2，第 5 阶段补缺）
 *
 * 与既有 7 条 E2E 的差别：本文件不依赖 playwright webServer 的共享栈——
 * spec 直接持有编排（scripts/e2e-restart/stack.mjs）：
 * - API uvicorn :8818，独立显式 db 路径，可被真实 SIGKILL 并以同一 db 重启
 *   （runs+checkpoints 持久化是恢复的前提）；
 * - web Vite :8890 程序化启动，/api 代理指向 :8818，与共享栈（8888/8808/8809）零端口
 *   冲突，可与既有 spec 同命令共存（本用例未启用时 beforeAll 不启动任何进程）。
 *
 * 恢复链路：页面恢复锚点（localStorage runId+lastSeq）→ reload 后拉详情 + 从 0 重放
 * 事件重建时间线/审批条 → 服务端旧租约过期（默认 15s）→ 新 worker 重认领（run.resumed）
 * → 续跑到终态。不依赖 vite 代理的断连传播行为——重启后以 reload 主动重连
 * （与既有"刷新恢复"用例同口径；页内 Last-Event-ID 自动重连在 API 掉线窗口内
 * 会因 getRun 失败而中止，这是页面层已知边界，不在本 spec 范围）。
 *
 * 门闩：审批等待期重启依赖 runner.py 的 pending-interrupt approver await 补丁
 * （见 docs/integration-requests/Y.md，红测试即该缺陷的复现）。补丁合入前默认
 * 跳过以保持 `pnpm test:e2e` 全绿；用 `pnpm test:e2e:restart`（或设
 * AGENT_HUB_E2E_RESTART=1）显式启用。
 */
test.skip(
  !process.env.AGENT_HUB_E2E_RESTART,
  "需 runner 审批恢复补丁 + 独立编排：scripts/e2e-restart/ 与 docs/integration-requests/Y.md",
);

test.setTimeout(150_000);

let api: ApiProcess | null = null;
let web: Awaited<ReturnType<typeof startWeb>> | null = null;

test.beforeAll(async () => {
  test.setTimeout(60_000);
  await cleanupDb(dbPaths("y1"));
  await cleanupDb(dbPaths("y2"));
  web = await startWeb();
  api = new ApiProcess(dbPaths("y1"));
  await api.start();
});

test.afterAll(async () => {
  await api?.kill9();
  await web?.close();
});

interface RunEventLike {
  seq: number;
  type: string;
  node?: string | null;
  attempt?: number;
  payload: Record<string, unknown>;
}

/** 按 device 聚合某工具某类事件的出现次数（payload.args.device_id） */
function toolEventCounts(events: RunEventLike[], type: string, tool: string): Map<string, number> {
  const byDevice = new Map<string, number>();
  for (const event of events) {
    if (event.type !== type) continue;
    if (String(event.payload.tool ?? "") !== tool) continue;
    const args = (event.payload.args ?? {}) as Record<string, unknown>;
    const device = String(args.device_id ?? args.deviceId ?? "?");
    byDevice.set(device, (byDevice.get(device) ?? 0) + 1);
  }
  return byDevice;
}

/**
 * 写工具不重复执行的完整证明（事件流 + 账本双口径）：
 * - create_work_order 每台设备真实执行恰一次（tool.called 只在真实执行路径发出，
 *   去重命中发 tool.deduplicated 并直接返回缓存结果）；
 * - 恢复重跑 reporter 时账本拦截重复执行（tool.deduplicated 至少出现一次）；
 * - dispatch_drone（dangerous）审批通过后每台 critical 恰好执行一次；
 * - 账本无 in_progress/unknown 崩溃遗留，全部 completed。
 */
async function assertWriteToolsExecutedOnce(runId: string, runsDb: string): Promise<void> {
  const events = (await collectRunEvents(runId)) as unknown as RunEventLike[];

  // 终态到达：事件流含 run.completed（而非 run.failed）
  expect(
    events.some((e) => e.type === "run.completed"),
    `终态应为 run.completed，实际：${events
      .filter((e) => e.type.startsWith("run."))
      .map((e) => e.type)
      .join(", ")}`,
  ).toBeTruthy();

  // 续跑痕迹：重启后新 worker 重认领（run.resumed，attempt ≥ 2）
  const resumed = events.find((e) => e.type === "run.resumed");
  expect(resumed?.attempt ?? 0).toBeGreaterThanOrEqual(2);

  const executed = toolEventCounts(events, "tool.called", "create_work_order");
  expect(executed.size, "至少应为一台异常设备创建工单").toBeGreaterThanOrEqual(1);
  for (const [device, count] of executed) {
    expect(count, `create_work_order 对 ${device} 重复执行`).toBe(1);
  }

  const deduped = events.filter(
    (e) => e.type === "tool.deduplicated" && String(e.payload.tool ?? "") === "create_work_order",
  );
  expect(deduped.length, "恢复重跑应触发账本去重（tool.deduplicated）").toBeGreaterThanOrEqual(1);

  // 派工（dangerous 写工具）：审批通过后每台 critical 恰好执行一次
  const approval = events.find((e) => e.type === "approval.required");
  const criticalDevices = ((approval?.payload.devices ?? []) as unknown[]).map(String);
  expect(criticalDevices.length).toBeGreaterThanOrEqual(1);
  const drones = toolEventCounts(events, "tool.called", "dispatch_drone");
  for (const device of criticalDevices) {
    expect(drones.get(device) ?? 0, `dispatch_drone 对 ${device} 应恰好执行一次`).toBe(1);
  }

  // 账本层面：写/危险工具全部 completed，无崩溃遗留的中间态
  const rows = ledgerRows(runsDb).filter((r) =>
    ["create_work_order", "dispatch_drone"].includes(String(r.tool)),
  );
  expect(rows.length).toBeGreaterThanOrEqual(executed.size + criticalDevices.length);
  for (const row of rows) {
    expect(String(row.status), `账本 ${row.key} 状态应为 completed`).toBe("completed");
  }
  // 账本 create_work_order 键数 == 真实执行与去重命中的设备并集（无账本外执行）
  const dedupDevices = new Set(
    deduped.map((e) =>
      String(e.payload.ledger_key ?? "")
        .split(":")
        .pop(),
    ),
  );
  const union = new Set([...executed.keys(), ...dedupDevices]);
  expect(rows.filter((r) => String(r.tool) === "create_work_order").length).toBe(union.size);
}

async function startInspection(page: import("@playwright/test").Page): Promise<string> {
  const created = page.waitForResponse(
    (res) => res.url().includes("/api/runs") && res.request().method() === "POST",
  );
  await page.getByLabel(/设备 ID/).fill("INV-001, INV-005, INV-002");
  await page.getByRole("button", { name: "发起巡检" }).click();
  const body = (await (await created).json()) as { runId: string };
  return body.runId;
}

test("Y1 运行中 SIGKILL 重启：页面恢复续跑到终态，写工具不重复执行", async ({ page }) => {
  await page.goto(`${WEB_ORIGIN}/runs`);
  const runId = await startInspection(page);

  // 运行中的确定性信号：规划完成（planner 已执行，run 持有租约处于 running）
  await expect(page.getByText(/规划完成/)).toBeVisible({ timeout: 20_000 });

  // 真实 SIGKILL（进程无清理机会：租约/心跳/审批 Future 戛然而止）→ 同一 db 重新拉起
  await api!.kill9();
  await api!.start();

  // 页面恢复：恢复锚点重放全量事件；旧租约过期（默认 15s）后 run.resumed 进入时间线
  await page.reload();
  await expect(page.getByText(/续跑（第 \d+ 次执行）/)).toBeVisible({ timeout: 45_000 });

  // 恢复后审批条可用（重放或恢复重跑产生）→ 批准 → 终态
  await expect(page.getByRole("button", { name: "批准派无人机" })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "批准派无人机" }).click();

  await expect(page.getByText("巡检报告")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText(/覆盖率 100%/)).toBeVisible();

  await assertWriteToolsExecutedOnce(runId, api!.runsDb);
});

test("Y2 审批等待期 SIGKILL 重启：审批条恢复 → 批准 → 报告完成，派工只执行一次", async ({
  page,
}) => {
  // 独立 db：杀掉 Y1 的 API，换 y2 库重新拉起（避免上一条用例的历史 run 干扰）
  await api!.kill9();
  await cleanupDb(dbPaths("y2"));
  api = new ApiProcess(dbPaths("y2"));
  await api!.start();

  await page.goto(`${WEB_ORIGIN}/runs`);
  const runId = await startInspection(page);

  // 运行至审批等待：此刻 create_work_order 已完成落账本，interrupt 检查点已持久化
  await expect(page.getByRole("button", { name: "批准派无人机" })).toBeVisible({ timeout: 20_000 });

  // SIGKILL → 同一 db（runs+checkpoints）重启 → 页面 reload 恢复
  await api!.kill9();
  await api!.start();
  await page.reload();

  // 恢复锚点重放全量事件：未决审批条仍然在（不需要重新发起巡检）
  await expect(page.getByRole("button", { name: "批准派无人机" })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "批准派无人机" }).click();

  // 重认领需等旧租约过期（≤15s）+ 恢复执行 → 报告完成
  await expect(page.getByText("巡检报告")).toBeVisible({ timeout: 45_000 });
  await expect(page.getByText(/审批通过/).first()).toBeVisible();
  await expect(page.getByText(/覆盖率 100%/)).toBeVisible();

  await assertWriteToolsExecutedOnce(runId, api!.runsDb);
});
