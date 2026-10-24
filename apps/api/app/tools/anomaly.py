"""确定性异常判读规则（ADR-0009：数值判断交给代码，LLM 只做解释与决策）。

阈值与正常范围与语料《各设备类型异常判据与告警阈值》一致
（fixtures/inspection/corpus/<type>-thresholds.md，权威来源为语料文档）：
- 阈值越限（threshold）→ critical；
- 趋势漂移（trend，连续 ≥3 点超出正常范围）→ warning；
- 阶跃突变（step，相邻点变化 > 30% 正常范围宽度）→ warning；
- 储能 PCS 功率骤降（rolling 6h 中位数对比，降幅 > 30%）→ critical。

期望值从数据推导（fixtures/inspection/README 约束），不引用生成器注释。
"""

from typing import Literal

from pydantic import BaseModel, Field

from app.tools.devices import DEFAULT_FIXTURE_ROOT, TelemetrySeries, load_devices, load_telemetry

AnomalyKind = Literal["threshold", "trend", "step", "drop"]

# 设备类型 → (告警阈值; 正常范围; 功率骤降检测配置)
# 与 corpus/<type>-thresholds.md 的表一致；两处必须同改。
THRESHOLDS: dict[str, dict[str, float]] = {
    "pv_inverter": {"cabinet_temp_c": 85},
    "wind_turbine": {"vibration_mm_s": 7.1},
    "storage_pcs": {},  # 阈值型无；功率骤降走 drop 规则
    "substation_bay": {"oil_temp_c": 75},
    "bridge_sensor": {"displacement_mm": 30},
}

NORMAL_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    "pv_inverter": {"output_kw": (180, 250), "cabinet_temp_c": (35, 55), "dc_input_v": (580, 820)},
    "wind_turbine": {
        "output_kw": (800, 1500),
        "nacelle_temp_c": (30, 60),
        "vibration_mm_s": (1.5, 3.5),
        "rotor_rpm": (10, 18),
    },
    "storage_pcs": {"output_kw": (200, 500), "cell_temp_max_c": (25, 45), "soc_pct": (20, 95)},
    "substation_bay": {"load_ratio": (0.4, 0.8), "oil_temp_c": (40, 70)},
    "bridge_sensor": {"displacement_mm": (5, 15), "temp_c": (10, 30)},
}

# 功率骤降阈值（storage_pcs 专属，语料阈值 output_kw_drop_pct > 30）
DROP_PCT = 30.0
TREND_MIN_POINTS = 3
STEP_RATIO = 0.3


class AnomalyItem(BaseModel):
    metric: str
    kind: AnomalyKind
    detail: str
    max_value: float | None = None
    threshold: float | None = None


class AnomalyResult(BaseModel):
    device_id: str
    level: Literal["normal", "warning", "critical"]
    anomalies: list[AnomalyItem] = Field(default_factory=list)
    checked_points: int = 0


def detect(device_id: str, root=None) -> AnomalyResult:
    """对单台设备跑全部确定性规则。未知设备类型/缺数据按 FileNotFoundError/KeyError 上抛。"""
    root = root if root is not None else DEFAULT_FIXTURE_ROOT
    devices = {d.device_id: d for d in load_devices(root)}
    if device_id not in devices:
        raise KeyError(f"未知设备: {device_id}")
    dtype = devices[device_id].type
    series: TelemetrySeries = load_telemetry(device_id, root)
    values_by_metric = _metric_columns(series)

    anomalies: list[AnomalyItem] = []
    first_step: dict[str, int] = {}
    for metric, threshold in THRESHOLDS.get(dtype, {}).items():
        values = values_by_metric.get(metric, [])
        if values and max(values) > threshold:
            anomalies.append(
                AnomalyItem(
                    metric=metric,
                    kind="threshold",
                    detail=f"{metric} 最大值 {max(values):.1f} 超过告警阈值 {threshold}",
                    max_value=max(values),
                    threshold=threshold,
                )
            )

    for metric, (lo, hi) in NORMAL_RANGES.get(dtype, {}).items():
        values = values_by_metric.get(metric, [])
        if not values:
            continue
        items, step_at = _range_anomalies(metric, values, lo, hi)
        anomalies.extend(items)
        if step_at is not None:
            first_step[metric] = step_at

    if dtype == "storage_pcs":
        values = values_by_metric.get("output_kw", [])
        if values:
            anomalies.extend(
                _drop_anomaly("output_kw", values, first_step.get("output_kw"), DROP_PCT)
            )

    level = "normal"
    if any(a.kind in ("threshold", "drop") for a in anomalies):
        level = "critical"
    elif anomalies:
        level = "warning"
    return AnomalyResult(device_id=device_id, level=level, anomalies=anomalies, checked_points=len(series.points))


def _metric_columns(series: TelemetrySeries) -> dict[str, list[float]]:
    columns: dict[str, list[float]] = {}
    for point in series.points:
        for metric, value in point.metrics.items():
            columns.setdefault(metric, []).append(value)
    return columns


def _range_anomalies(
    metric: str, values: list[float], lo: float, hi: float
) -> tuple[list[AnomalyItem], int | None]:
    """趋势（连续 ≥3 点出界）与阶跃（相邻变化 > 30% 范围宽度）规则；返回首个阶跃下标。"""
    out: list[AnomalyItem] = []
    first_step: int | None = None
    breach = [v > hi or v < lo for v in values]
    run = 0
    best_run = 0
    for flag in breach:
        run = run + 1 if flag else 0
        best_run = max(best_run, run)
    if best_run >= TREND_MIN_POINTS:
        out.append(
            AnomalyItem(
                metric=metric,
                kind="trend",
                detail=f"{metric} 连续 {best_run} 个采样点超出正常范围 {lo}~{hi}，判定漂移类异常",
                max_value=max(values),
                threshold=hi,
            )
        )
    width = hi - lo
    for i, (prev, cur) in enumerate(zip(values, values[1:])):
        if abs(cur - prev) > STEP_RATIO * width:
            out.append(
                AnomalyItem(
                    metric=metric,
                    kind="step",
                    detail=f"{metric} 相邻采样点变化 {prev:.1f}→{cur:.1f}，超过正常波动 {STEP_RATIO:.0%}，判定突变类异常",
                    max_value=cur,
                    threshold=hi,
                )
            )
            if first_step is None:
                first_step = i + 1  # 阶跃后第 i+1 个点起为平台候选
            break  # 每指标只报第一次阶跃，避免事件刷屏
    return out, first_step


def _drop_anomaly(
    metric: str,
    values: list[float],
    step_index: int | None,
    pct: float,
) -> list[AnomalyItem]:
    """功率骤降检测（储能 PCS 专属，语料阈值 output_kw_drop_pct > 30）。

    单日数据无跨日基线；确定性近似：在阶跃点（``step_index``，由阶跃规则先行检出）
    之后取 4 点平台中位数，与前 6 点最小二乘直线外推到平台中点的期望值比较，
    降幅超过 ``pct`` 判 critical。无阶跃（平滑缓降）不触发，避免正常日曲线误报。
    """
    if step_index is None or step_index + 1 >= len(values):
        return []
    plateau = values[step_index + 1 : step_index + 5]
    if not plateau:
        return []
    plateau_median = sorted(plateau)[len(plateau) // 2]
    base_start = max(0, step_index - 6)
    base = values[base_start:step_index]
    if len(base) < 3:
        return []
    # 最小二乘直线 y = a + b*x（x 为绝对时刻），外推到平台中点
    xs = list(range(base_start, step_index))
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(base) / n
    b = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, base)) / sum((x - mean_x) ** 2 for x in xs)
    a = mean_y - b * mean_x
    plateau_mid = step_index + 1 + (len(plateau) - 1) / 2
    expected = a + b * plateau_mid
    if expected <= 0:
        return []
    drop_pct = (1 - plateau_median / expected) * 100
    if drop_pct > pct:
        return [
            AnomalyItem(
                metric=metric,
                kind="drop",
                detail=(
                    f"{metric} 在第 {step_index} 点后形成低位平台（中位数 {plateau_median:.1f}），"
                    f"较前段趋势外推期望 {expected:.1f} 下降 {drop_pct:.0f}%，超过 {pct:.0f}%"
                ),
                max_value=plateau_median,
                threshold=pct,
            )
        ]
    return out
