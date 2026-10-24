"""B0：设备与遥测数据读取（fixtures/inspection/ 的唯一消费入口）。

数据全部为合成数据（fixtures/inspection/README.md），仅离线评测与演示。
- devices.json：20 台设备，camelCase 字段 → DeviceRecord（alias 往返无损）；
- telemetry/<deviceId>.json：24 个小时级采样点，字段随设备类型不同 → 统一进
  TelemetryPoint.metrics dict，未知标量字段不丢弃（injected 单独保留）；
- telemetry_summary：每指标 min/max/mean/last 的紧凑统计（B6 上下文压缩基础）。
"""

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 默认 fixtures 根：<repo>/fixtures/inspection（本文件位于 <repo>/apps/api/app/tools/）
DEFAULT_FIXTURE_ROOT = Path(__file__).resolve().parents[4] / "fixtures" / "inspection"


class DeviceRecord(BaseModel):
    """设备台账记录；对外字段保持 fixtures 的 camelCase（往返无损）。"""

    model_config = ConfigDict(populate_by_name=True, frozen=True)

    device_id: str = Field(alias="deviceId")
    site: str
    site_name: str = Field(alias="siteName")
    type: str
    type_label: str = Field(alias="typeLabel")
    model: str
    capacity_kw: float = Field(alias="capacityKw")
    commissioned_at: str = Field(alias="commissionedAt")
    status: str


class TelemetryPoint(BaseModel):
    """单个采样点：ts + 任意数值指标（按设备类型不同）。

    数值字段收进 ``metrics``；非数值标记字段（如 injected）单独保留。
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    ts: str
    metrics: dict[str, float] = Field(default_factory=dict)
    injected: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _collect_metrics(cls, data: dict) -> dict:
        """把 ts/injected 之外的数值字段收进 metrics（字段集随设备类型不同）。"""
        if isinstance(data, dict):
            metrics = {
                k: float(v)
                for k, v in data.items()
                if k not in ("ts", "injected", "metrics") and isinstance(v, (int, float))
            }
            extra_metrics = data.get("metrics") or {}
            data = {**data, "metrics": {**metrics, **extra_metrics}}
        return data


class TelemetrySeries(BaseModel):
    """一台设备一个时间窗的采样序列。"""

    model_config = ConfigDict(frozen=True)

    device_id: str = Field(alias="deviceId")
    window: str
    points: list[TelemetryPoint]


def load_devices(root: Path | str = DEFAULT_FIXTURE_ROOT, site: str | None = None) -> list[DeviceRecord]:
    """读取设备清单；可选按站点过滤。"""
    data = json.loads((Path(root) / "devices.json").read_text(encoding="utf-8"))
    devices = [DeviceRecord.model_validate(d) for d in data["devices"]]
    if site is not None:
        devices = [d for d in devices if d.site == site]
    return devices


def load_telemetry(device_id: str, root: Path | str = DEFAULT_FIXTURE_ROOT) -> TelemetrySeries:
    """读取单台设备遥测；文件缺失按 FileNotFoundError 上抛（不编造数据）。"""
    path = Path(root) / "telemetry" / f"{device_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    points = [TelemetryPoint.model_validate(raw) for raw in data["points"]]
    return TelemetrySeries(deviceId=data["deviceId"], window=data["window"], points=points)


def telemetry_summary(series: TelemetrySeries) -> dict[str, dict[str, float]]:
    """每指标的 min/max/mean/last 紧凑统计；空序列返回空 dict。"""
    if not series.points:
        return {}
    names = sorted({k for p in series.points for k in p.metrics})
    summary: dict[str, dict[str, float]] = {}
    for name in names:
        values = [p.metrics[name] for p in series.points if name in p.metrics]
        summary[name] = {
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
            "last": values[-1],
        }
    return summary
