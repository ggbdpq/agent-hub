<!-- 集成请求收件箱：子智能体 X 追加，主线程读并处理。只追加，不改历史记录。 -->

# 集成请求 — 子智能体 X（第 5 阶段补缺）

格式：`- [日期] 文件 / 改动 / 原因 / 是否阻塞`。阻塞请求先在日报标红。

## 待处理

- [2026-10-25] apps/api/app/agents/graph.py（reporter/_draft_work_orders）/ 无改动请求，仅通报（X 所有权外，未触碰）/ E1 离线（X2）与真实口径（e1-real-20261024）multi 的 forbidden_calls=2 同源：reporter 对 warning/critical 一律起草工单，goal（"只诊断不生成工单"）未传入起草层，2 条只诊断用例被开工单。评测已如实呈现并写入两份报告已知偏差；是否按 goal 收敛由主线程决策 / 不阻塞

## 主线程下行（2026-10-25）：E1 工具 P/R 口径修正方案（重跑时落实）

对 e1-real-20261024-222238.json 的逐用例核实结论：multi 漏调 get_telemetry 12/12（B6 简报注入设计与期望集错配，主因）；list_devices 漏 2 例（R3 权限收缩相关，次要）；dispatch_drone 期望 2 例按"批准"写但 E1 默认拒绝审批（口径错配）。要求：
1. 期望集区分「必调」与「信息获取方式不限」（get_telemetry/list_devices 归后者：工具调用或简报注入均可，以结论/证据引用判定）；
2. dispatch_drone 在默认拒绝审批口径下期望为「不调用」（或双口径分列）；
3. E1 重跑报告同时呈现 process-strict（旧口径）与 goal-oriented（新口径）两列工具 P/R，量化口径影响，不隐藏差异。
