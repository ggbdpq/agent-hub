<!-- 集成请求收件箱：子智能体 Y 追加，主线程读并处理。只追加，不改历史记录。 -->

# 集成请求 — 子智能体 Y（第 5 阶段补缺）

格式：`- [日期] 文件 / 改动 / 原因 / 是否阻塞`。阻塞请求先在日报标红。

## 待处理

### [2026-10-25] R-Y1（阻塞）：runner.py pending-interrupt 分支未 await 异步 approver

- **文件**：`apps/api/app/agents/runner.py`（`InspectionAgentRunner.run`，R1 恢复分支）
- **改动**（3 行）：

```diff
         pending = await _pending_interrupt_payload(graph, config)
         if pending is not None:
             # 审批决定仍经 approver 路由：重启后由新进程的 approver 接管一次性消费
             decision = self._approver(pending)
+            # 与下方 in-process 循环的 I5 使能同一语义：异步 approver 必须 await
+            if inspect.isawaitable(decision):
+                decision = await decision
             result = await graph.ainvoke(Command(resume=decision), config)
```

- **原因**：审批等待期 API 进程被 SIGKILL → 重启后 worker 重认领 → 检查点停在
  pending interrupt → `self._approver(pending)` 返回 coroutine 未 await →
  `Command(resume=<coroutine>)` 在 langgraph 检查点序列化时抛
  `TypeError: Type is not msgpack serializable: coroutine` → reporter 以
  `decision={}`（非 dict 兜底）走完并落 `run.failed`。生产 HTTP 路径
  （main.py `ApprovalHub.approver`，异步）必现；既有单测
  `tests/agents/test_runner_resume.py` 只注入同步 approver，故未覆盖。
  I5 补丁只改了 in-process interrupt 循环，漏了 R1 的 pending 分支。
- **验证证据**（ah-Y 本地）：
  - 红：`node scripts/e2e-restart/run.mjs` → Y1/Y2 均
    `执行失败：TypeError: Type is not msgpack serializable: coroutine`（页面时间线可见，
    test-results 快照在案）；
  - 绿：应用上述 3 行后 `2 passed (34.6s)`（两连绿）；既有
    `pytest tests/agents/test_runner_resume.py tests/agents/test_checkpoints.py -q` → 2 passed。
- **建议随附单测**（可选，`tests/agents/test_runner_resume.py` 追加）：给进程 2 的
  approver 用 `async def`（返回与现用例相同决定），断言 completed——即可在单测层
  锁住本缺陷，不依赖 E2E。
- **是否阻塞**：**阻塞 Y1/Y2 E2E 转绿**（spec 已用 `AGENT_HUB_E2E_RESTART` 门闩默认
  跳过，`pnpm test:e2e` 当前 7 passed + 2 skipped 不受影响）。

### [2026-10-25] R-Y2（非阻塞）：package.json 增加 test:e2e:restart 独立命令

- **文件**：`package.json`
- **改动**（1 行）：

```diff
     "test:api": "node scripts/api.mjs test",
     "test:e2e": "playwright test",
+    "test:e2e:restart": "node scripts/e2e-restart/run.mjs",
     "lint": "oxlint .",
```

- **原因**：重启 E2E 的运行配方（AGENT_HUB_E2E_RESTART 门闩 + 离线剧本 + 回环不走
  代理 + 只跑 restart spec）已封装在 `scripts/e2e-restart/run.mjs`（Y 所有权目录），
  命令行入口只差 package.json 一行；跨平台（参数数组 spawn，不经 shell）。
- **是否阻塞**：否。不合并也可用 `node scripts/e2e-restart/run.mjs` 直接跑。

### [2026-10-25] R-Y3（非阻塞，随 R-Y1 一起做）：移除 restart spec 的门闩

- **文件**：`e2e/inspection-restart.spec.ts`
- **改动**：R-Y1 合入后删除这一行（门闩仅为保持补丁合入前 `pnpm test:e2e` 全绿）：

```diff
-test.skip(!process.env.AGENT_HUB_E2E_RESTART, "需 runner 审批恢复补丁 + 独立编排：scripts/e2e-restart/ 与 docs/integration-requests/Y.md");
```

- **原因**：补丁合入后 spec 已确定性绿（spec 自带 8818/8890 独立端口编排，与共享栈
  零冲突），应回归默认 `pnpm test:e2e` 与 CI e2e job 常驻运行；`run.mjs` 保留作
  单独跑 restart 套件的便捷入口（此时门闩 env 变为无害冗余，可留可删）。
- **是否阻塞**：否。

### [2026-10-25] 说明：playwright.config.ts 与 .github/workflows/ci.yml 无需改动

重启 E2E 最终**没有**采用"独立 playwright project / 独立 webServer"方案：spec 直接
持有进程编排（`scripts/e2e-restart/stack.mjs`）——API uvicorn :8818（独立显式 db，
可 SIGKILL/同 db 重启）+ 程序化 Vite :8890（/api 代理指向 8818、cacheDir 独立避免与
共享 Vite 并发写缓存），与共享栈 8888/8808/8809 零端口冲突。已实测同命令共存：
`pnpm test:e2e` → 7 passed + 2 skipped（13.2s，门闩状态下；R-Y1+R-Y3 合入后为
9 passed）。因此 config 与 CI 均零改动；若主线程仍希望 CI 单独分类跑，加一步
`pnpm test:e2e:restart` 即可（依赖 R-Y2）。

### 【主线程处理 2026-10-25】R-Y2 补合入（审查 D13 抓漏）

首次批量补丁脚本在门闩行断言处中断，package.json 这一行被跳过且未发现——对抗审查实测 `pnpm test:e2e:restart` 不存在而文档已宣称。现已补入；R-Y1/R-Y2/R-Y3 三项全部闭环。
