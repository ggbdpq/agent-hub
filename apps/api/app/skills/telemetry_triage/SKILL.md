---
name: telemetry-triage
description: 遥测分诊——读取设备遥测统计并跑确定性异常判读，level 以判读为准
allowed_tools: get_telemetry, detect_anomaly
output_schema: app.tools.anomaly.AnomalyResult
---

## 分诊守则
- 先读遥测统计（get_telemetry），再跑确定性判读（detect_anomaly）；
- level 以判读结果为准，禁止凭原始数值自行升级或降级；
- 判读明细（anomalies）必须原样并入结论 metrics，供报告与评测复盘。
