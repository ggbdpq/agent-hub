"""C7：智能体评测集校验——Schema、分布、期望值必须由遥测判据推导复核。

期望异常设备/等级不得引用 fixtures 注释当答案：本测试对每条用例的 input.device_ids
重新运行 evals.derive_expectations.detect（判据代码化自语料 thresholds 文档），
与数据集中存储的期望逐台比对——数据与期望漂移即红。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.contracts.agent import InspectionRequest  # noqa: E402
from evals.datasets import load_agent_cases  # noqa: E402
from evals.derive_expectations import detect_device  # noqa: E402

KNOWN_TOOLS = {
    "list_devices",
    "get_telemetry",
    "detect_anomaly",
    "search_procedures",
    "create_work_order",
    "dispatch_drone",
}
FAULT_TYPES = {"tool_timeout", "llm_invalid_output", "subtask_failure"}


def _cases() -> list[dict]:
    return load_agent_cases()["cases"]


def test_dataset_has_60_cases_unique_cid() -> None:
    cases = _cases()
    assert len(cases) == 60
    cids = [c["cid"] for c in cases]
    assert len(set(cids)) == 60


def test_difficulty_three_tiers_20_each() -> None:
    counts: dict[str, int] = {"easy": 0, "medium": 0, "hard": 0}
    for c in _cases():
        assert c["difficulty"] in counts, f"{c['cid']} 难度档非法：{c['difficulty']}"
        counts[c["difficulty"]] += 1
    assert counts == {"easy": 20, "medium": 20, "hard": 20}


def test_exactly_10_fault_injection_cases_of_known_types() -> None:
    faulted = [c for c in _cases() if c.get("fault")]
    assert len(faulted) == 10, f"故障注入应为 10 条，实际 {len(faulted)}"
    for c in faulted:
        assert c["fault"]["type"] in FAULT_TYPES, f"{c['cid']} 故障类型非法"
    by_type = {t: 0 for t in FAULT_TYPES}
    for c in faulted:
        by_type[c["fault"]["type"]] += 1
    assert all(v >= 3 for v in by_type.values()), f"三类故障覆盖不足：{by_type}"


def test_input_validates_against_inspection_request() -> None:
    for c in _cases():
        try:
            InspectionRequest(**c["input"])
        except ValidationError as e:
            pytest.fail(f"{c['cid']} input 不满足 InspectionRequest 契约：{e}")


def test_expected_structure_and_known_tools() -> None:
    for c in _cases():
        exp = c["expected"]
        device_ids = set(c["input"]["device_ids"])
        anomalies = set(exp["anomaly_devices"])
        assert anomalies <= device_ids, f"{c['cid']} 期望异常设备必须在巡检名单内"
        assert set(exp["anomaly_levels"]) == anomalies, f"{c['cid']} 等级表必须与异常设备一一对应"
        assert set(exp["tools_expected"]) <= KNOWN_TOOLS, f"{c['cid']} 未知工具：{exp['tools_expected']}"
        assert not (set(exp["tools_expected"]) & set(exp["forbidden_tools"])), (
            f"{c['cid']} 期望与禁止工具冲突"
        )
        for pair in exp.get("tools_order_required", []):
            assert len(pair) == 2 and all(t in exp["tools_expected"] for t in pair), (
                f"{c['cid']} 顺序约束必须引用期望工具"
            )
        for wo in exp.get("work_orders", []):
            assert wo["device_id"] in anomalies, f"{c['cid']} 工单只能对应期望异常设备"


def test_expected_anomalies_derived_from_telemetry_not_from_comments() -> None:
    """逐台重跑确定性判据：数据集中存储的期望必须与遥测推导结果一致。"""
    mismatch: list[str] = []
    for c in _cases():
        derived: dict[str, str] = {}
        for device_id in c["input"]["device_ids"]:
            result = detect_device(device_id)
            if result["anomalous"]:
                derived[device_id] = result["level"]
        stored_levels = {
            d: lvl
            for d, lvl in c["expected"]["anomaly_levels"].items()
            if d in set(c["input"]["device_ids"])
        }
        if derived != stored_levels:
            mismatch.append(f"{c['cid']}: 推导={derived} 存储={stored_levels}")
    assert not mismatch, "期望异常与遥测推导不一致（禁止引用注释当答案）：\n" + "\n".join(mismatch)


def test_derivation_flags_exactly_injected_devices() -> None:
    """全部 20 台过判据：仅注入设备异常且为 critical（管道健康检查）。"""
    flagged = {d: detect_device(d) for d in [
        *[f"INV-{i:03d}" for i in range(1, 9)],
        *[f"WT-{i:03d}" for i in range(1, 5)],
        *[f"PCS-{i:03d}" for i in range(1, 4)],
        *[f"SW-{i:03d}" for i in range(1, 4)],
        *[f"BS-{i:03d}" for i in range(1, 3)],
    ]}
    anomalous = {d: r for d, r in flagged.items() if r["anomalous"]}
    assert set(anomalous) == {"INV-005", "WT-002", "PCS-003", "SW-002", "BS-001"}, (
        f"判据标记结果异常：{sorted(anomalous)}"
    )
    assert all(r["level"] == "critical" for r in anomalous.values())
