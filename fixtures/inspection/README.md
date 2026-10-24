# fixtures/inspection — 巡检多智能体合成数据

> **全部为合成数据**，仅供离线评测与演示，不代表任何真实设备或企业规程。

## 结构

| 路径 | 内容 | 消费方 |
|---|---|---|
| `devices.json` | 20 台设备（光伏逆变器 / 风机 / 储能 PCS / 变电站间隔 / 桥梁传感） | B：`tools/devices.py` |
| `telemetry/<deviceId>.json` | 每台 24 个小时级采样点（2026-10-23 窗口） | B：`get_telemetry` / `detect_anomaly` |
| `corpus/*.md` | 30 篇规程（5 类设备 × 6 篇），层级标题供按标题切分 | C：切分 / 索引 / 检索评测 |
| `generate.py` | 确定性生成器（无随机源，重复运行逐字节一致） | 再生成入口 |

## 异常注入

5 台设备带写死的异常模式（`INV-005` 温度漂移、`WT-002` 振动阶跃、`PCS-003` 功率骤降、
`SW-002` 油温漂移、`BS-001` 位移阶跃）。**评测期望值必须由 `detect_anomaly` 从数据推导**，
不得引用本文件或生成器注释作为答案来源。

## 再生成

```bash
apps/api/.venv/bin/python fixtures/inspection/generate.py
```

C 可向 `corpus/` 追加语料（追加不影响既有 `chunk_id` 稳定性的前提：内容与文件名不变）。
