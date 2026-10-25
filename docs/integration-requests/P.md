<!-- 集成请求收件箱：子智能体 P 追加，主线程读并处理。只追加，不改历史记录。 -->

# 集成请求 — 子智能体 P（第 6 轮：缺陷修复与评测补强）

格式：`- [日期] 文件 / 改动 / 原因 / 是否阻塞`。

## 待处理

- [2026-10-25] apps/api/app/tools/executor.py（执行器新增 2.5/2.6 两道闸）/ P1a 步数临界保护（只剩最后 1 步拒绝再执行 read 工具，detect_anomaly 豁免；写/dangerous 不拦，reporter 落工单不受影响）+ P1b 连续重复读调用拒绝（同执行器实例上紧随上一次成功调用的同工具名+同参数 read 调用 deny `duplicate_call`，拒绝发生在预算扣减前不消耗 tool_calls；被拒/出错调用不记指纹，出错后重试仍合法）/ 原因：E1 hard 档 `step_budget_exhausted` 失败形态的两个根因都在执行器层才能跨节点生效（引导语需直达模型观察消息，inspector/single_react 已同步补 reason 字段）/ 不阻塞（P1 主线程下行明确允许实施；Budget 默认值未动）
- [2026-10-25] P1b 跨节点语义补充说明 / 指纹按 ToolExecutor 实例隔离：multi 图每设备 inspector 独立实例（device_id 由代码覆盖进参数，跨设备不会误判），single_react 全循环一实例，reporter 一实例（全写工具，不受 read 门影响）；「连续」= 同实例上紧随上一次成功调用，中间隔着其他调用（A→B→A）放行 / 幂等账本语义不变：write/dangerous 的重复仍走 ledger 去重（§9），审批恢复重跑 reporter 的 create_work_order 二次调用照旧 deduplicated / 不阻塞
- [2026-10-25] apps/api/app/agents/inspector.py（_rescue_or_fail 扩面）/ P1c：step/token 预算耗尽时以确定性判读兜底出 finding（判读不可得才记失败）；llm_error 不扩面（与预算无关，保持原语义）/ P1c 行为口径：判读兜底的 finding level/summary 全部来自 detect_anomaly 确定性输出，证据只含真实观测 chunk，不编造 / 不阻塞
- [2026-10-25] apps/api/app/agents/graph.py + docs/interview/deep-dive.md、red-team-qa.md / P2：goal 只诊断（diagnosis_only_goal：显式"只出结论/不开工单/禁止创建工单"类信号且无肯定式工单/派工/处置动作词）时 reporter 跳过起草，notes 记「按 goal 只诊断，未起草工单」；无信号或处置类 goal 行为完全不变；两份面试文档各补版本行为变化注 / 与 Q.md 下行口径对齐（只诊断 → create_work_order 属 forbidden）/ 不阻塞
- [2026-10-25] apps/api/app/agents/runner.py + graph.py / P3：规划结果按 run_id 缓存（request 指纹一致才复用，FIFO 上限 256，plan.created 带 cached 标记）——同进程崩溃续跑重驱动不再重跑 planner；跨进程不共享（新进程重新规划，语义同首次） / 不阻塞

### 主线程下行（2026-10-25）：P1 的证据链与口径

- 证据源：`apps/api/evals/reports/e1-real-20261025-011659.json`（e1-real-rounds/ 逐轮文件）——multi hard 档 30 样本 5 例失败，`step_budget_exhausted`（H13/H17 等）与 token 预算耗尽；失败 run 的 per_case 里有完整事件序列可作复现参考。
- 生产口径注意：`Budget` 默认 max_steps=24 是运行时契约（runner 从 ctx.budget 组装），P1 的改动方向是**预算临界行为**（最后 1 步禁工具、强制出结论；重复工具调用拒绝；耗尽时确定性判读兜底出 finding），不是放宽预算数值——不改 Budget 默认值。
- "连续两次相同工具名+参数"的重复判定在**执行器层**实现（executor），跨节点生效；注意与幂等账本（只管 write/dangerous）区分，read 工具的重复调用也可能浪费步数。
- E1 报告口径：改后真实重跑由主线程执行（hard 10×3 + 全量 50×1），你的报告只需离线验证 + 单测证据。
