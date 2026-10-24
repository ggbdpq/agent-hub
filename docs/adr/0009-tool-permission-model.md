# ADR-0009：工具权限模型（读 / 写 / 高危三级 + 执行器强制）

- 状态：已接受
- 日期：2026-10-24
- 相关：ADR-0006（账本）、ADR-0007、计划 §5.2 / §7.2-B2

## 背景

JD 职责 3 要求工具有元数据、权限、可发现性。花语侧（ADR-0004）已验证"工具 + 因果校验"
优于提示词约束；巡检侧把同一思想扩展为运行时强制：所有约束在工具执行器中由代码保证，
不写在提示词里。

## 决定

1. `ToolSpec`（`apps/api/app/contracts/tools.py`）：
   - `effect: read | write | dangerous`——write 必须走幂等账本；dangerous 必须人工审批（LangGraph interrupt）；
   - `allowed_nodes: set[str]`——节点级权限隔离，越权 → `tool.denied` 事件（不是异常崩溃）；
   - `retry: safe | never | ledger`——超时/状态未知后能否重试；`ledger` 类不自动重试；
   - `timeout_s` 由执行器统一 `asyncio.wait_for`。
2. 统一入口 `execute_tool(spec, args, ctx)`（B 实现）：权限检查 → 参数 Schema 校验 →
   预算扣减 → 超时 → write 走账本 → 结果 Schema 校验 → 发事件。
3. MCP：`apps/mcp_inspection/` 从同一 ToolSpec 注册表生成 MCP 工具；`dangerous` 工具
   在 MCP 侧默认不暴露（单一事实源，避免两套工具行为漂移）。
4. 初始工具面：`list_devices`、`get_telemetry`、`detect_anomaly`（确定性规则，数值判断交给代码）、
   `search_procedures`（调 Retriever）为 read；`create_work_order` 为 write；
   `dispatch_drone` 为 dangerous（仅模拟，不接真实设备）。

## 理由（vs 备选）

- 运行时强制 vs 提示词约束：消融实验 E7 将提供对照数据（越权次数）。
- 确定性规则 detect_anomaly vs LLM 判数值：可测、可复现；LLM 只做解释与决策。

## 后果

- 工具 handler 只收已校验入参（`Callable[[BaseModel], Awaitable[BaseModel]]`）；
  需要运行时能力时由 B 用闭包/partial 绑定。
- C7 评测集含越权与超预算用例；质量门可统计越权调用次数。
