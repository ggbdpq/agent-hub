# 简历要点草稿（Z3，已回填 X 真实模型数字）

> 硬口径：机制、架构、测试类定性表述基于代码事实（ah-Z@`ebf93a3`，行号锚点可 `git show ebf93a3:` 复核）；效果类数字已用 X 真实模型报告（`e1-real-20261025-011659` / `e3-real-20261025-015300` / `e7-offline-20261025-000228`）回填，出处可查。旧报告（20261024 的 e1-real）数字口径有错配，未引用。真实模型口径：deepseek-chat @ api.deepseek.com/v1，2026-10-25，50 条非故障用例 × 3 轮（E7 为确定性代码路径，无真实模型）。

## 要点 1：多智能体编排与运行时强制（LangGraph）

> 基于 LangGraph StateGraph 手写 Supervisor，规划 / 巡检分析 / 报告三类 Worker：多设备并行扇出（Send 经条件边）、子任务节点级超时降级为 partial、覆盖率以请求设备清单为分母（缩水计划不虚抬、缺失设备记入 notes）；工具按读 / 写 / 高危三级建模，节点权限、步数、Token 与调用次数预算、幂等账本全部由工具执行器在代码层强制，高危操作经 interrupt 人工审批（缺省拒绝）；异常判读用确定性规则（阈值 / 趋势 / 阶跃 / 骤降），LLM 只做解释与决策，结论中的证据引用经代码剔除未观测项。

可核实锚点（面试被问"证据呢"时）：
- 扇出与超时降级：`apps/api/app/agents/graph.py:142-163`（wait_for + failures）、`graph.py:323-340`（coverage 分母与缩水检查）；测试 `tests/agents/test_graph.py::test_subtask_timeout_degrades_to_partial_with_coverage`、`test_shrunken_plan_coverage_uses_requested_denominator`。
- 执行器强制链：`apps/api/app/tools/executor.py:66-178`（白名单→权限→预算→取消→参数→审批凭证→账本→超时→双 Schema 校验）；测试 `tests/tools/test_executor.py::test_dangerous_tool_requires_approval_id`、`test_budget_tool_calls_exhausted_denies`。
- 结论可信：`apps/api/app/agents/inspector.py:258-261`（证据剔除）与 `inspector.py:277`（level 以确定性判读为准）；测试 `tests/agents/test_inspector.py::test_bogus_chunk_id_stripped`、`test_level_forced_by_code_when_model_skips_anomaly`。

## 要点 2：持久化运行时（租约 / 心跳 / fencing / CAS / 事件流）

> 自研 SQLite（WAL）持久化运行时：写事务内原子认领（BEGIN IMMEDIATE），fencing token 随接管单调递增，事件与终态写入带 CAS 条件——被顶替的过期执行者写入一律被拒；租约心跳失败经 RunContext 传导，编排层在步边界与工具调用前主动停止；kill -9 故障注入测试验证崩溃后租约回收、新执行者接管并续跑到终态；写工具走幂等账本（账本键由 run_id + 图节点 + 设备构成，跨 attempt 可复现），崩溃遗留"状态未知"的副作用禁止自动重试；审批等待期进程重启后从 LangGraph 检查点以 Command(resume) 恢复（修复过"带输入重启导致整图重跑、findings 翻倍"的真实缺陷并回归锁定）；事件按 (run_id, seq) 事务内分配无空洞，SSE 支持 Last-Event-ID 断点续传。

可核实锚点：
- 认领与 CAS：`apps/api/app/runtime/store.py:187-228`（claim）、`store.py:319-333`（finish CAS）；测试 `tests/runtime/test_cas_writes.py::test_stale_writer_rejected_after_takeover`。
- kill -9 接管：`tests/runtime/test_kill9_reclaim.py::test_kill9_then_reclaimed_within_lease`（真实子进程 SIGKILL + 数据库时钟）。
- 账本幂等：`store.py:546-559`（begin 幂等）+ `graph.py:207`（键构成）；测试 `tests/runtime/test_tool_ledger.py::test_write_tool_not_executed_twice_after_crash`。
- 审批重启恢复：`apps/api/app/agents/runner.py:84-92`；测试 `tests/agents/test_runner_resume.py::test_reclaimed_run_resumes_from_checkpoint_not_rerun`。
- 事件续传：`store.py:408-426` + `apps/api/app/main.py:313-349`；测试 `tests/test_runs_api.py::test_approval_flow_with_last_event_id_resume`。

## 要点 3：混合检索、评测与消融（数字回填区）

> 同一 ToolSpec 注册表同时驱动 API 与 MCP Server（dangerous 工具 MCP 侧不暴露）；混合检索 BM25 + 向量双路召回经 RRF 融合，中文分词用零依赖字符 bigram（与 jieba 对照评测后不引入新依赖），检索无相关结果返回空集不编造；分层评测集期望从遥测数据派生（不从生成器注释抄取，防泄露），指标（Recall@k / MRR / 成功率 / 工具 P-R）全部有手工算例单测锁定口径，质量门低于阈值 CI 即红；真实模型消融：多智能体 vs 单智能体（E1：成功率 96.7% vs 98.7%、goal-oriented 工具召回 99.2% / 98.4%、时延 P95 8.6s vs 26.2s、输入 Token 879K vs 2.25M；双口径 process-strict 与 goal-oriented 并列）、并行扇出 vs 串行（E3：时延 P50/P95 = 4.6s/6.5s vs 7.1s/19.1s，6+ 台设备 P50 相差 3.9×）、运行时强制 vs 仅提示词约束（E7：越权替身 336 次请求，运行时强制开启全部拦截、关闭时站点级越权执行 112 次而 write/dangerous 仍被账本与审批门拦下）。合成语料口径、成本按公开单价估算（deepseek-flash 档，未与实际账单核对）；E1 结论按数据如实：单智能体成功率更高（差距 <3pp），多智能体的实证价值在并行时延（P95 3 倍）；多智能体输入 Token 约为单智能体的 39%（879K vs 2.25M）——该差异的「上下文隔离」归因待 E4 真实验证后才能声明。

可核实锚点：
- 混合检索：`apps/api/app/knowledge/retriever.py:32-42`（双路 + RRF + 空结果不编造）、`app/knowledge/fuse.py:20-31`；测试 `tests/knowledge/test_hybrid_retriever.py`、`tests/knowledge/test_embedding.py`。
- 评测防泄露与质量门：测试 `tests/evals/test_agent_cases.py::test_expected_anomalies_derived_from_telemetry_not_from_comments`、`tests/evals/test_gate.py::test_gate_main_exit_codes`。
- MCP 单一事实源：测试 `tests/tools/test_mcp_server.py::test_list_tools_exposes_five_non_dangerous_tools`。

## 数字回填登记（已全部回填）

| 占位 | 来源报告（apps/api/evals/reports/） | 回填值 |
|---|---|---|
| E1 成功率 / 工具 P-R / 时延 / Token | `e1-real-20261025-011659.md` | 96.7% vs 98.7%（hard 83.3% vs 96.7%）；goal 召回 99.2%/98.4%（strict 56.5%/87.7%）；P95 8632.0ms vs 26249.1ms；输入 879K vs 2.25M |
| E3 时延 P50/P95 | `e3-real-20261025-015300.md` | 4638.7/6541.6ms vs 7079.5/19083.6ms（6+ 台 P50 6849 vs 26843ms，3.9×） |
| E7 越权调用次数 | `e7-offline-20261025-000228.md` | 336 次越权请求：on 全拦（执行 0）；off 站点级 list_devices 执行 112 次，write/dangerous 被账本/审批门拦下 |

红线（写进简历前自查）：
1. 任何数字必须能在 evals/reports/ 报告原文找到出处（计划 §10 口径审查项）。
2. 合成语料声明不可省：数字旁注明"离线合成语料口径"；FakeLLM/剧本回放的成功率不得作为模型效果出现。
3. E1 若两配置差距在噪声内且单智能体更优，按计划 §11 判定如实写（"知道何时不该用多智能体"本身是卖点），不修饰。
