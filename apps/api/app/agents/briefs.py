"""B4/B6：子任务简报——上下文隔离的数据载体。

子任务只接收"任务简报 + 该设备的数据摘要"，不接收全局消息历史（计划 §7.2-B6）；
简报为纯 JSON 可序列化 dict（LangGraph 检查点友好），字段做长度收敛。
"""

import json
from typing import Any

from app.tools.devices import (
    DEFAULT_FIXTURE_ROOT,
    DeviceRecord,
    TelemetrySeries,
    load_devices,
    load_telemetry,
    telemetry_summary,
)

# 简报收敛上限（假设：一个设备的统计摘要远小于模型上下文；
# 防御性截断指标名与检查项，完整遥测序列永远不进简报）
MAX_TELEMETRY_METRICS = 12
MAX_CHECKS = 6


def build_subtask_brief(
    device_id: str,
    checks: list[str],
    device: DeviceRecord,
    telemetry: dict[str, dict[str, float]] | None = None,
) -> dict[str, Any]:
    """构造子任务简报：设备静态信息 + 检查项 + 该设备遥测统计摘要（仅本设备）。"""
    summary = telemetry or {}
    return {
        "device": {
            "device_id": device.device_id,
            "site": device.site,
            "site_name": device.site_name,
            "type": device.type,
            "type_label": device.type_label,
            "model": device.model,
            "capacity_kw": device.capacity_kw,
            "status": device.status,
        },
        "checks": checks[:MAX_CHECKS],
        "telemetry_summary": {k: summary[k] for k in list(summary)[:MAX_TELEMETRY_METRICS]},
    }


def load_brief(device_id: str, checks: list[str], *, fixture_root=None) -> dict[str, Any]:
    """便捷入口：从 fixtures 读取设备与遥测后构造简报（生产路径）。"""
    root = fixture_root if fixture_root is not None else DEFAULT_FIXTURE_ROOT
    device = next(d for d in load_devices(root) if d.device_id == device_id)
    series: TelemetrySeries = load_telemetry(device_id, root)
    return build_subtask_brief(device_id, checks, device, telemetry_summary(series))


def brief_json(brief: dict[str, Any]) -> str:
    return json.dumps(brief, ensure_ascii=False)
