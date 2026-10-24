"""评测期望值推导（C7）：把语料 thresholds 文档的判据代码化，从遥测数据推导期望异常。

**这是评测集期望异常设备的唯一答案来源**：期望值由本模块对
``fixtures/inspection/telemetry/<deviceId>.json`` 逐台推导（tests/evals/test_agent_cases.py 复核），
不引用 fixtures 注释或生成器源码。

判据（fixtures/inspection/corpus/<type>-thresholds.md，合成文档）：
- 正常运行范围：参数应落在 [lo, hi]；
- 告警阈值：参数超过阈值 → 立即上报（单点越限即告警级证据）；
- 判读方法：连续 3 个采样点单调偏离正常范围 → 漂移类；相邻采样点变化幅度超过
  正常波动 3 倍 → 突变类。本模块把"正常波动"基线定义为 max(相邻增量中位数,
  正常范围宽度×10%)，以抵御量化噪声（如 load_ratio 按 0.1 步进）。
- 储能变流器的告警指标是派生量 output_kw_drop_pct：相邻采样点环比降幅
  （``(1 - v[i]/v[i-1]) × 100%``）超过阈值 30 → 骤降告警。

等级：越过告警阈值 → critical；仅偏离正常范围（漂移/阶跃未越告警线）→ warning。
"""

import json
import statistics
from pathlib import Path

from evals.datasets import fixtures_root

# 语料 <type>-thresholds.md 的数值判据（代码化；改动须与语料同步并在测试中复核）
RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "pv_inverter": {
        "output_kw": (180, 250),
        "cabinet_temp_c": (35, 55),
        "dc_input_v": (580, 820),
    },
    "wind_turbine": {
        "output_kw": (800, 1500),
        "nacelle_temp_c": (30, 60),
        "vibration_mm_s": (1.5, 3.5),
        "rotor_rpm": (10, 18),
    },
    "storage_pcs": {
        "output_kw": (200, 500),
        "cell_temp_max_c": (25, 45),
        "soc_pct": (20, 95),
    },
    "substation_bay": {
        "load_ratio": (0.4, 0.8),
        "oil_temp_c": (40, 70),
    },
    "bridge_sensor": {
        "displacement_mm": (5, 15),
        "temp_c": (10, 30),
    },
}

ALARM_ABOVE: dict[str, dict[str, float]] = {
    "pv_inverter": {"cabinet_temp_c": 85.0},
    "wind_turbine": {"vibration_mm_s": 7.1},
    "substation_bay": {"oil_temp_c": 75.0},
    "bridge_sensor": {"displacement_mm": 30.0},
}

DROP_PCT_ALARM: dict[str, tuple[str, float]] = {  # type → (base_metric, alarm_pct)
    "storage_pcs": ("output_kw", 30.0),
}

DEVICE_PREFIX_TYPE: dict[str, str] = {
    "INV": "pv_inverter",
    "WT": "wind_turbine",
    "PCS": "storage_pcs",
    "SW": "substation_bay",
    "BS": "bridge_sensor",
}

_BASELINE_FLOOR_FRACTION = 0.10  # 正常波动下限 = 范围宽度 × 10%
_DRIFT_MIN_RUN = 3  # 连续 3 点
_STEP_FACTOR = 3.0  # 相邻变化 > 3 × 正常波动
_DROP_BASELINE_POINTS = 8  # 保留：文档口径记录（当前判据用相邻环比，见 derive）


def device_type_of(device_id: str) -> str:
    prefix = device_id.split("-", 1)[0]
    if prefix not in DEVICE_PREFIX_TYPE:
        raise ValueError(f"未知设备编号前缀：{device_id}")
    return DEVICE_PREFIX_TYPE[prefix]


def load_telemetry(device_id: str, fixtures_dir: Path | None = None) -> dict:
    root = fixtures_dir or fixtures_root()
    return json.loads((root / "telemetry" / f"{device_id}.json").read_text(encoding="utf-8"))


def detect_device(device_id: str, fixtures_dir: Path | None = None) -> dict:
    """对单台设备推导异常：{"anomalous", "level", "patterns", "evidence"}。

    evidence 为触发判据的 (metric, ts, value) 列表，供评测断言引用真实数据点。
    """
    device_type = device_type_of(device_id)
    points = load_telemetry(device_id, fixtures_dir)["points"]
    ranges = RANGES[device_type]
    alarms = ALARM_ABOVE.get(device_type, {})
    patterns: set[str] = set()
    evidence: list[dict] = []
    crossed_alarm = False
    out_of_range_only = False

    for metric, values in _series(points).items():
        if metric not in ranges:
            continue
        lo, hi = ranges[metric]
        alarm_at = alarms.get(metric)
        # 单点越限（告警阈值）
        if alarm_at is not None:
            for point in points:
                if point[metric] > alarm_at:
                    crossed_alarm = True
                    patterns.add("alarm_threshold")
                    evidence.append(
                        {"metric": metric, "ts": point["ts"], "value": point[metric], "rule": "alarm_above"}
                    )
        # 趋势越限：连续 N 点单调偏离正常范围
        run = _longest_monotonic_run(values, lo, hi)
        if run is not None:
            run_values = values[run[0] : run[1]]
            run_points = points[run[0] : run[1]]
            if alarm_at is not None and max(run_values) > alarm_at:
                crossed_alarm = True
            else:
                out_of_range_only = True
            patterns.add("drift")
            evidence.append(
                {
                    "metric": metric,
                    "ts": run_points[0]["ts"],
                    "value": run_values,
                    "rule": f"drift_run>={_DRIFT_MIN_RUN}",
                }
            )
        # 阶跃越限：相邻增量 > 3 × 正常波动（基线 = max(增量中位数, 范围宽度×10%)）
        baseline = _fluctuation_baseline(values, lo, hi)
        for prev, point in zip(points, points[1:]):
            delta = abs(point[metric] - prev[metric])
            if delta > _STEP_FACTOR * baseline:
                step_crosses = alarm_at is not None and max(point[metric], prev[metric]) > alarm_at
                if step_crosses:
                    crossed_alarm = True
                elif alarm_at is None and (point[metric] > hi or prev[metric] > hi or point[metric] < lo or prev[metric] < lo):
                    out_of_range_only = True
                patterns.add("step")
                evidence.append(
                    {
                        "metric": metric,
                        "ts": point["ts"],
                        "value": delta,
                        "rule": f"step>{_STEP_FACTOR:g}x_baseline({baseline:.4g})",
                    }
                )

    # 储能骤降：派生指标 output_kw_drop_pct（相邻点环比降幅）
    if device_type in DROP_PCT_ALARM:
        metric, alarm_pct = DROP_PCT_ALARM[device_type]
        values = [p[metric] for p in points]
        for i in range(1, len(values)):
            if values[i - 1] <= 0:
                continue
            decline_pct = (1.0 - values[i] / values[i - 1]) * 100.0
            if decline_pct > alarm_pct:
                crossed_alarm = True
                patterns.add("drop_pct")
                evidence.append(
                    {
                        "metric": f"{metric}_drop_pct",
                        "ts": points[i]["ts"],
                        "value": round(decline_pct, 2),
                        "rule": f"drop_pct>{alarm_pct:g}",
                    }
                )
                break

    level = "critical" if crossed_alarm else ("warning" if out_of_range_only else "normal")
    return {
        "anomalous": level != "normal",
        "level": level,
        "patterns": sorted(patterns),
        "evidence": evidence,
    }


def _series(points: list[dict]) -> dict[str, list[float]]:
    """只取数值指标列；``injected`` 等生成器标记一律忽略（推导只看遥测数值本身）。"""
    keys: set[str] = set()
    for point in points:
        keys.update(k for k, v in point.items() if k != "ts" and isinstance(v, (int, float)))
    return {k: [float(p[k]) for p in points] for k in sorted(keys)}


def _longest_monotonic_run(values: list[float], lo: float, hi: float) -> tuple[int, int] | None:
    """最长"连续越界且同向"窗口 (start, end_exclusive)；长度 >= 3 才返回。"""
    best: tuple[int, int] | None = None
    i, n = 0, len(values)
    while i < n:
        if lo <= values[i] <= hi:
            i += 1
            continue
        direction = 1 if values[i] > hi else -1
        j = i + 1
        while j < n and (values[j] > hi if direction == 1 else values[j] < lo) and (
            (values[j] - values[j - 1]) * direction >= 0
        ):
            j += 1
        if j - i >= _DRIFT_MIN_RUN and (best is None or j - i > best[1] - best[0]):
            best = (i, j)
        i = j
    return best


def _fluctuation_baseline(values: list[float], lo: float, hi: float) -> float:
    deltas = [abs(b - a) for a, b in zip(values, values[1:])]
    median_delta = statistics.median(deltas) if deltas else 0.0
    return max(median_delta, (hi - lo) * _BASELINE_FLOOR_FRACTION)


def derive_all(fixtures_dir: Path | None = None) -> dict[str, dict]:
    """全部 20 台设备的推导结果（评测集构建与测试复核共用）。"""
    results: dict[str, dict] = {}
    for prefix, count in (("INV", 8), ("WT", 4), ("PCS", 3), ("SW", 3), ("BS", 2)):
        for i in range(1, count + 1):
            device_id = f"{prefix}-{i:03d}"
            results[device_id] = detect_device(device_id, fixtures_dir)
    return results


if __name__ == "__main__":  # 人工核对入口：python -m evals.derive_expectations
    for device_id, result in derive_all().items():
        flag = "!!" if result["anomalous"] else "ok"
        print(f"{flag} {device_id}: {result['level']} {result['patterns']}")
