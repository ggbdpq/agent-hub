# 智能体评测报告（C8，runner=scripted-tools）

- 生成时间：2026-10-25T02:38:34.791955+00:00
- 样本：60 条用例（easy/medium/hard = 20/20/20）
- 成本单价：离线默认零单价；真实模型评测须在报告中声明模型/单价/日期
- 样本量：cases=60

> **确定性工具型脚本参考智能体（证据来自真实检索、判据为确定性规则）：仅验证评测流程与指标口径，不代表模型效果。**

## 方法

- 评测框架只依赖 AgentRunner 契约（app/contracts/agent.py）：构造 RunContext（事件收集、预算、账本、注入的 Retriever）执行单条用例并评分。
- 异常识别 P/R 按 finding(level!=normal) 设备集合计算；工具调用 P/R 按 tool.called 事件集合计算；预测空集按 0 计（不虚计满分）。
- 工具 P/R 双口径（X1）：process-strict=期望集内工具必须调用（旧口径）；goal-oriented=get_telemetry/list_devices 按「信息获取方式不限」以结论覆盖判定（结论覆盖全部请求设备即不计漏调），dispatch_drone 按默认拒绝审批口径期望不调用（调用计为多余，合规率单列）。
- 顺序约束 = 期望工具对的首次出现次序；证据有效 = 异常 finding 的 chunk_id 非空且全部真实存在。
- 每条用例保存完整事件序列（同名 JSON cases[].events）供复盘。

## 总体指标

| 指标 | 数值 |
|---|---|
| 任务成功率 | 100.0% |
| 异常识别 Precision / Recall | 87.2% / 87.2% |
| 工具调用 Precision / Recall（process-strict 旧口径） | 92.6% / 98.6% |
| 工具调用 Precision / Recall（goal-oriented 新口径） | 91.6% / 98.6% |
| 派工合规率（默认拒绝口径，期望含派工的用例） | 0.0% |
| 参数准确率 | 97.6% |
| 工单字段命中率 | 100.0% |
| 顺序约束满足率 | 98.3% |
| 证据有效率（要求用例） | 100.0% |
| 越权调用次数（合计） | 0 |
| 写工具重复执行违规 | 0 |
| 崩溃 run 数 | 0 |
| 步数均值 | 7.38 |
| 时延 P50 / P95 (ms) | 0.47 / 1.30 |
| Token（输入/输出，脚本估算） | 15480 / 7740 |
| 成本 (USD) | 0.0000 |

## 分难度档成功率

| 难度 | 用例数 | 成功率 |
|---|---|---|
| easy | 20 | 100.0% |
| medium | 20 | 100.0% |
| hard | 20 | 100.0% |

## 方差与波动

- success_std = 0
- anomaly_recall_std = 0.3343
- steps_std = 4.77
- latency_ms_std = 0.48

## 已知偏差

- 离线口径：成功率验证的是评测流程与契约管道，不代表模型效果。
- Token 为脚本估算值（非模型计费口径）；成本单价默认 0。
- 事件 payload 无 Schema（ADR-0008），指标解析按字段容错；runner 未发的事件不计入指标。
- P/R 聚合跳过期望与预测均空的用例（不虚计满分），宏平均对故障注入用例的 0 分敏感。
- Q3 口径变更说明（2026-10-25）：scripted-tools 替身的只诊断判定由「不生成工单」单措辞扩展为禁写三措辞（不开工单/不生成工单/禁止创建工单，evals.agent_eval.is_diagnosis_goal 共享判定），与评测集 forbidden 口径（只诊断 → create_work_order/dispatch_drone 属 forbidden）对齐。本报告数字与上一份（agent-scripted-tools-20261024-234243）完全一致：当前只诊断用例（E01-E20/H20）设备判据均无异常，判定扩展未改变现网行为；行为守卫测试（tests/evals/test_q3_diagnosis_goal.py，修前红证据 2 failed）锁定未来设备判据或措辞变化时不回归。仅验证流程，不代表模型效果。
