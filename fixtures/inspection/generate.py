"""生成 fixtures/inspection/** 的确定性合成数据：20 台设备 + 24h 遥测 + 30 篇规程。

所有数据为**合成数据**，仅供离线评测与演示，不代表任何真实设备（README 须保留此口径）。
再生成：`python fixtures/inspection/generate.py`（无随机源，重复运行输出逐字节一致）。

异常注入模式（写死在 ANOMALIES；B 的 detect_anomaly 规则与 C 的评测集期望值
仍须从数据推导，不得直接引用本注释）：
- INV-005  柜温后 6h 线性漂移至超阈值（55→92°C）
- WT-002   振动后 6h 阶跃超限（~3→9.5 mm/s）
- PCS-003  中段输出功率骤降 40%
- SW-002   油温全程缓慢漂移至超阈值
- BS-001   位移后 6h 阶跃（~12→45 mm）
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

HERE = Path(__file__).parent
WINDOW_START = datetime(2026, 10, 23, tzinfo=UTC)
HOURS = 24

# 类型 →（字段与正常范围，告警阈值；阈值同时用于语料"异常判据"篇，两处必须一致）
TYPES = {
    "pv_inverter": {
        "label": "光伏逆变器",
        "fields": {"output_kw": (180, 250), "cabinet_temp_c": (35, 55), "dc_input_v": (580, 820)},
        "thresholds": {"cabinet_temp_c": 85},
    },
    "wind_turbine": {
        "label": "风力发电机组",
        "fields": {"output_kw": (800, 1500), "nacelle_temp_c": (30, 60),
                    "vibration_mm_s": (1.5, 3.5), "rotor_rpm": (10, 18)},
        "thresholds": {"vibration_mm_s": 7.1},
    },
    "storage_pcs": {
        "label": "储能变流器",
        "fields": {"output_kw": (200, 500), "cell_temp_max_c": (25, 45), "soc_pct": (20, 95)},
        "thresholds": {"output_kw_drop_pct": 30},
    },
    "substation_bay": {
        "label": "变电站间隔",
        "fields": {"load_ratio": (0.4, 0.8), "oil_temp_c": (40, 70)},
        "thresholds": {"oil_temp_c": 75},
    },
    "bridge_sensor": {
        "label": "桥梁监测传感",
        "fields": {"displacement_mm": (5, 15), "temp_c": (10, 30)},
        "thresholds": {"displacement_mm": 30},
    },
}

SITES = {
    "SITE-A": "临港园区光伏电站",
    "SITE-B": "沿海风电场",
    "SITE-C": "储能电站",
    "SITE-D": "城区变电站与桥梁",
}

# (device_id, type, site, model, capacity_kw) —— 20 台
DEVICES = [
    ("INV-001", "pv_inverter", "SITE-A", "SG320HX", 320),
    ("INV-002", "pv_inverter", "SITE-A", "SG320HX", 320),
    ("INV-003", "pv_inverter", "SITE-A", "SG110CX", 110),
    ("INV-004", "pv_inverter", "SITE-A", "SG110CX", 110),
    ("INV-005", "pv_inverter", "SITE-A", "SG320HX", 320),
    ("INV-006", "pv_inverter", "SITE-A", "SG320HX", 320),
    ("INV-007", "pv_inverter", "SITE-A", "SG110CX", 110),
    ("INV-008", "pv_inverter", "SITE-A", "SG320HX", 320),
    ("WT-001", "wind_turbine", "SITE-B", "GW155-4.5", 4500),
    ("WT-002", "wind_turbine", "SITE-B", "GW155-4.5", 4500),
    ("WT-003", "wind_turbine", "SITE-B", "GW140-3.4", 3400),
    ("WT-004", "wind_turbine", "SITE-B", "GW140-3.4", 3400),
    ("PCS-001", "storage_pcs", "SITE-C", "SC500HV", 500),
    ("PCS-002", "storage_pcs", "SITE-C", "SC500HV", 500),
    ("PCS-003", "storage_pcs", "SITE-C", "SC3420kW", 3420),
    ("SW-001", "substation_bay", "SITE-D", "10kV馈线间隔", 0),
    ("SW-002", "substation_bay", "SITE-D", "35kV主变间隔", 0),
    ("SW-003", "substation_bay", "SITE-D", "10kV馈线间隔", 0),
    ("BS-001", "bridge_sensor", "SITE-D", "DS-30型位移计", 0),
    ("BS-002", "bridge_sensor", "SITE-D", "DS-30型位移计", 0),
]


def _noise(seed: str, i: int) -> float:
    """确定性伪随机（0~1）：sha256(seed|i) 前 8 字节映射，无随机模块依赖。"""
    digest = hashlib.sha256(f"{seed}|{i}".encode()).digest()
    return int.from_bytes(digest[:8]) / 2**64


def _lerp(lo: float, hi: float, t: float) -> float:
    return lo + (hi - lo) * t


def gen_devices() -> list[dict]:
    devices = []
    for device_id, dtype, site, model, kw in DEVICES:
        devices.append({
            "deviceId": device_id,
            "site": site,
            "siteName": SITES[site],
            "type": dtype,
            "typeLabel": TYPES[dtype]["label"],
            "model": model,
            "capacityKw": kw,
            "commissionedAt": f"20{20 + int(_noise(device_id, 0) * 6):02d}-06-01",
            "status": "online",
        })
    return devices


def gen_telemetry(device_id: str, dtype: str) -> list[dict]:
    spec = TYPES[dtype]
    rows = []
    for i in range(HOURS):
        row = {"ts": (WINDOW_START + timedelta(hours=i)).isoformat().replace("+00:00", "Z")}
        for field, (lo, hi) in spec["fields"].items():
            t = i / (HOURS - 1)
            base = _lerp(lo, hi, 0.5 + 0.4 * _cos_day(i))
            jitter = (_noise(device_id + field, i) - 0.5) * (hi - lo) * 0.06
            row[field] = round(base + jitter, 2)
        row.update(_inject_anomaly(device_id, dtype, i))
        rows.append(row)
    return rows


def _cos_day(i: int) -> float:
    """日周期形状（光伏/负荷曲线的确定性近似）。"""
    import math

    return math.sin((i - 6) / 24 * 2 * math.pi)


def _inject_anomaly(device_id: str, dtype: str, i: int) -> dict:
    """按写死的模式改写字段；返回该行的告警标记（供人核对，非评测答案）。"""
    tail = i >= HOURS - 6
    if device_id == "INV-005" and tail:
        return {"cabinet_temp_c": round(55 + (i - (HOURS - 7)) * 6.0, 2), "injected": "temp_drift"}
    if device_id == "WT-002" and tail:
        return {"vibration_mm_s": round(9.5 + _noise("wt2v", i), 2), "injected": "vib_step"}
    if device_id == "PCS-003" and 8 <= i < 16:
        return {"output_kw": round(_lerp(200, 500, _cos_day(i)) * 0.6, 2), "injected": "power_drop"}
    if device_id == "SW-002":
        return {"oil_temp_c": round(40 + i * 1.6 + _noise("sw2", i), 2), "injected": "oil_drift"}
    if device_id == "BS-001" and tail:
        return {"displacement_mm": round(45 + _noise("bs1", i), 2), "injected": "disp_step"}
    return {}


def _doc_devices() -> dict[str, list[tuple[str, str]]]:
    """语料按类型组织：每类 6 篇固定结构。"""
    return {
        dtype: [
            (f"{label}安装验收规程", "acceptance"),
            (f"{label}日常巡检规程", "daily"),
            (f"{label}异常判据与告警阈值", "thresholds"),
            (f"{label}常见故障处理", "troubleshoot"),
            (f"{label}定期维护周期表", "maintenance"),
            (f"{label}作业安全规程", "safety"),
        ]
        for dtype, spec in TYPES.items()
        for label in [spec["label"]]
    }


def _threshold_lines(dtype: str) -> str:
    spec = TYPES[dtype]
    rows = "\n".join(
        f"| {field} | {lo}~{hi} | - |" for field, (lo, hi) in spec["fields"].items()
    )
    alarm = "\n".join(
        f"| {field} | > {th} | 立即上报并启动异常处理流程 |"
        for field, th in spec["thresholds"].items()
    )
    return f"""## 正常运行范围

| 参数 | 正常范围 | 备注 |
|---|---|---|
{rows}

## 告警阈值

| 参数 | 阈值 | 动作 |
|---|---|---|
{alarm}
"""


def gen_doc(dtype: str, title: str, kind: str) -> str:
    label = TYPES[dtype]["label"]
    site = next(s for d in DEVICES if d[1] == dtype for s in [SITES[d[2]]])
    sections = {
        "acceptance": f"""## 验收前检查
1. 核对设备铭牌型号与设计图纸一致，附件齐全。
2. 绝缘电阻测试合格，接地连续性满足规程要求。
3. 通信接口（RS485 / 以太网）联通，遥测点位与平台一致。

## 验收测试
- 满负荷试运行 4 小时，各参数处于正常运行范围。
- 保护定值核对无误，告警动作正确。

## 资料移交
安装记录、试验报告、出厂资料归档，录入设备台账。""",
        "daily": f"""## 巡检准备
核对设备清单与通信状态，确认遥测采集窗口正常（建议最近 24 小时）。

## 巡检项目
1. 外观：无异味、无异响、无明显过热痕迹。
2. 参数比对：实时值与 {site} 监控平台一致。
3. 环境检查：散热通道无遮挡，环境温度满足运行条件。

## 巡检记录
记录参数读数与偏差；发现越限按《{label}异常判据与告警阈值》处理。""",
        "thresholds": f"""{_threshold_lines(dtype)}
## 判读方法
- 单点越限：复核采样有效性，排除通信误码。
- 趋势越限：连续 3 个采样点单调偏离正常范围，判定为漂移类异常。
- 阶跃越限：相邻采样点变化幅度超过正常波动 3 倍，判定为突变类异常。

## 上报要求
判定异常后生成工单，注明设备编号、异常参数、实测值与判定依据。""",
        "troubleshoot": f"""## 常见故障
1. 通信中断：检查终端电阻与总线电压，重启采集单元。
2. 参数漂移：比对便携仪表实测值，校准或更换传感器。
3. 过热告警：检查散热风扇与滤网，降载运行并加强监视。

## 处理流程
隔离故障 → 现场确认 → 检修或更换 → 复测验证 → 恢复送电（设备投运）。""",
        "maintenance": f"""## 维护周期

| 周期 | 项目 |
|---|---|
| 月度 | 外观清洁、散热检查、通信状态确认 |
| 季度 | 端子紧固、滤网更换、绝缘抽测 |
| 年度 | 全项试验、保护定值核对、传感器校验 |

## 记录要求
维护记录录入台账，超期未维护的设备在巡检中重点标注。""",
        "safety": f"""## 作业前提
- 两票制度：工作票许可后方可作业，监护人全程在场。
- 停电作业执行验电、放电、挂牌上锁。

## 现场要求
1. 佩戴劳保用品，使用合格工器具。
2. {site} 内禁止单人作业与跨越围栏。
3. 紧急情况按现场应急处置卡执行，先救人后抢修。""",
    }
    body = sections[kind]
    return f"""# {title}

> 合成数据文档：仅供 agent-hub 巡检多智能体离线评测与演示使用，非任何真实企业规程。

适用对象：{label}（{site}）
适用场景：安装验收、日常巡检、异常处置与维护作业。

{body}

## 修订记录
- 版本 1.0（2026-10）：合成数据初版，仅用于评测。
"""


def main() -> None:
    (HERE / "telemetry").mkdir(exist_ok=True)
    (HERE / "corpus").mkdir(exist_ok=True)

    devices = gen_devices()
    (HERE / "devices.json").write_text(
        json.dumps({"note": "合成数据：仅供离线评测与演示", "devices": devices}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    for device_id, dtype, *_ in DEVICES:
        rows = gen_telemetry(device_id, dtype)
        (HERE / "telemetry" / f"{device_id}.json").write_text(
            json.dumps({"deviceId": device_id, "window": "2026-10-23 00:00~23:00Z", "points": rows},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    count = 0
    for dtype, docs in _doc_devices().items():
        for title, kind in docs:
            (HERE / "corpus" / f"{dtype}-{kind}.md").write_text(gen_doc(dtype, title, kind), encoding="utf-8")
            count += 1

    injected = [d for d, t, *_ in DEVICES if any(
        r.get("injected") for r in gen_telemetry(d, t))]
    print(f"设备 {len(devices)} 台，遥测 {len(devices)} 文 × {HOURS} 点，语料 {count} 篇")
    print(f"异常注入设备：{', '.join(sorted(injected))}")


if __name__ == "__main__":
    main()
