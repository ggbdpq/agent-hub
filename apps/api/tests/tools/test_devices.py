"""B0 领域模型与设备数据读取：Schema 往返测试 + fixtures 真实读取。

计划 §7.2-B0：``app/tools/devices.py`` 读取 ``fixtures/inspection/`` 的设备与遥测；
模型往返（dump → parse）必须无损，遥测字段按设备类型不同 → 统一为 metrics dict。
"""

from pathlib import Path

from app.tools.devices import (
    DeviceRecord,
    TelemetryPoint,
    TelemetrySeries,
    load_devices,
    load_telemetry,
    telemetry_summary,
)

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURES = REPO_ROOT / "fixtures" / "inspection"


def test_device_record_roundtrip() -> None:
    """devices.json 的 camelCase 字段 dump→parse 无损（往返测试）。"""
    raw = {
        "deviceId": "INV-001",
        "site": "SITE-A",
        "siteName": "临港园区光伏电站",
        "type": "pv_inverter",
        "typeLabel": "光伏逆变器",
        "model": "SG320HX",
        "capacityKw": 320,
        "commissionedAt": "2020-06-01",
        "status": "online",
    }
    record = DeviceRecord.model_validate(raw)
    assert DeviceRecord.model_validate(record.model_dump()) == record


def test_telemetry_point_roundtrip_with_extra_metrics() -> None:
    """遥测点位：ts + 任意数值字段进 metrics dict；未知标量字段不丢。"""
    point = TelemetryPoint.model_validate(
        {
            "ts": "2026-10-23T18:00:00Z",
            "output_kw": 213.9,
            "cabinet_temp_c": 61.0,
            "injected": "temp_drift",
        }
    )
    assert point.metrics["output_kw"] == 213.9
    assert point.injected == "temp_drift"
    restored = TelemetryPoint.model_validate(point.model_dump())
    assert restored == point


def test_load_devices_from_fixtures() -> None:
    """读取真实 fixtures：20 台设备，字段齐全，含全部 5 种类型。"""
    devices = load_devices(FIXTURES)
    assert len(devices) == 20
    assert {d.type for d in devices} == {
        "pv_inverter",
        "wind_turbine",
        "storage_pcs",
        "substation_bay",
        "bridge_sensor",
    }
    inv001 = next(d for d in devices if d.device_id == "INV-001")
    assert inv001.site == "SITE-A"
    assert inv001.capacity_kw == 320


def test_load_devices_site_filter() -> None:
    devices = load_devices(FIXTURES, site="SITE-A")
    assert len(devices) == 8
    assert all(d.site == "SITE-A" for d in devices)


def test_load_telemetry_series() -> None:
    """每台设备 24 个小时级采样点，window 保留。"""
    series = load_telemetry("INV-005", FIXTURES)
    assert isinstance(series, TelemetrySeries)
    assert series.device_id == "INV-005"
    assert len(series.points) == 24
    assert series.window  # 自由文本时间窗
    assert "cabinet_temp_c" in series.points[-1].metrics


def test_load_telemetry_missing_device_raises() -> None:
    import pytest

    with pytest.raises(FileNotFoundError):
        load_telemetry("NO-SUCH-DEVICE", FIXTURES)


def test_telemetry_summary_is_compact() -> None:
    """摘要只保留统计量：每指标 min/max/mean/last（B6 上下文压缩的基础）。"""
    series = load_telemetry("INV-005", FIXTURES)
    summary = telemetry_summary(series)
    stats = summary["cabinet_temp_c"]
    assert stats["max"] == 91.0  # 异常注入尾部漂移到 91°C
    assert stats["min"] < stats["max"]
    assert stats["last"] == 91.0
    assert len(summary) == len({k for p in series.points for k in p.metrics})


def test_anomalous_devices_detectable_from_data() -> None:
    """5 台注入异常设备的注入字段在数据里可观测（评测期望从数据推导）。"""
    expected = {
        "INV-005": "cabinet_temp_c",
        "WT-002": "vibration_mm_s",
        "PCS-003": "output_kw",
        "SW-002": "oil_temp_c",
        "BS-001": "displacement_mm",
    }
    for device_id, metric in expected.items():
        series = load_telemetry(device_id, FIXTURES)
        assert metric in series.points[-1].metrics, device_id
