---
name: work-order-drafting
description: 工单起草——为异常设备生成结构化检修工单，状态一律 draft 待人工确认
allowed_tools: create_work_order
output_schema: app.contracts.agent.WorkOrder
---

## 起草守则
- 只为 warning/critical 设备起草工单；
- title 简明（不超过 40 字），action 引用判读发现的异常参数与处置方向；
- 工单状态一律 draft，执行需人工确认；工单不代替现场安全规程。
