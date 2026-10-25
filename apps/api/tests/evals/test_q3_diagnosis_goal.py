"""Q3 评测期望修正：只诊断 goal 的 forbidden 口径与替身行为一致性。

两层守卫（主线程下行 2026-10-25：只诊断 → create_work_order 属 forbidden）：
1. 数据守卫：goal 为只诊断语义（禁写行为标记）的用例，forbidden_tools 必须包含
   create_work_order 与 dispatch_drone；非只诊断用例不得禁 create_work_order——
   期望与 goal 语义一致，防评测集漂移。
2. 替身行为守卫：ScriptedToolAgentRunner（scripted-tools）与合规替身 single 角色
   对三种诊断 goal 措辞（“不开工单”/“不生成工单”/“禁止创建工单”）都必须跳过
   工单起草——替身与期望同口径，措辞变体不产生越权开单。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402

from evals.agent_eval import is_diagnosis_goal  # noqa: E402
from evals.datasets import load_agent_cases  # noqa: E402

# 三种历史措辞的只诊断 goal（E 组例行巡检 / H13-H14 只诊断 / H20 深度诊断）
DIAGNOSIS_GOALS = [
    "例行巡检：核对遥测是否越限，只出结论，不开工单。",
    "只诊断不生成工单：输出异常结论与依据，禁止创建工单。",
    "深度诊断：给出运行评价与规程依据，禁止创建工单。",
]
DISPOSAL_GOAL = "巡检并处置：异常设备按规程生成工单，注明设备编号、异常参数、实测值与判定依据。"
ANOMALOUS_DEVICE = "INV-005"  # 判据 critical（20 台中 5 台注入异常之一）


def _diagnosis_case(goal: str, cid: str = "Q3-T1") -> dict:
    """构造“只诊断 + 异常设备”用例（形态对齐 agent_cases.json）。"""
    return {
        "cid": cid,
        "difficulty": "easy",
        "input": {
            "site": "SITE-A",
            "device_ids": [ANOMALOUS_DEVICE],
            "window": "2026-10-23 00:00~23:00Z",
            "goal": goal,
        },
        "expected": {
            "anomaly_devices": [ANOMALOUS_DEVICE],
            "anomaly_levels": {ANOMALOUS_DEVICE: "critical"},
            "tools_expected": ["get_telemetry", "detect_anomaly", "search_procedures"],
            "forbidden_tools": ["create_work_order", "dispatch_drone"],
            "work_orders": [],
        },
    }


# ---- 数据守卫：forbidden 与 goal 语义一致（期望修正的锁定，防漂移） ----


def test_dataset_diagnosis_cases_forbid_write_tools() -> None:
    for c in load_agent_cases()["cases"]:
        forbidden = set(c["expected"]["forbidden_tools"])
        if is_diagnosis_goal(c["input"]["goal"]):
            assert {"create_work_order", "dispatch_drone"} <= forbidden, (
                f"{c['cid']} goal 为只诊断但 forbidden 缺写工具：{sorted(forbidden)}"
            )
        else:
            assert "create_work_order" not in forbidden, (
                f"{c['cid']} goal 为处置语义却禁 create_work_order（期望与 goal 语义矛盾）"
            )


def test_is_diagnosis_goal_covers_all_wording_variants() -> None:
    for goal in DIAGNOSIS_GOALS:
        assert is_diagnosis_goal(goal), f"诊断措辞未识别：{goal}"
    assert not is_diagnosis_goal(DISPOSAL_GOAL)
    assert not is_diagnosis_goal("")
    assert not is_diagnosis_goal(None)


# ---- 替身行为守卫：scripted-tools 对诊断 goal 不开单（先红：措辞变体漏识别） ----


@pytest.mark.parametrize("goal", DIAGNOSIS_GOALS)
async def test_scripted_tools_never_orders_on_diagnosis_goal(goal: str) -> None:
    from evals.agent_eval import run_case
    from evals.scripted_tools import ScriptedToolAgentRunner

    outcome = await run_case(ScriptedToolAgentRunner(), _diagnosis_case(goal), retriever=None)
    called = [e for e in outcome["events"] if e["type"] == "tool.called"]
    order_calls = [e for e in called if (e.get("payload") or {}).get("tool") == "create_work_order"]
    assert not order_calls, f"只诊断 goal 下替身开了工单：{order_calls}"
    assert outcome["metrics"]["forbidden_calls"] == 0
    report = outcome["report"]
    assert report is not None, "只诊断用例不应崩溃"
    assert not report.work_orders, f"只诊断 goal 下报告含工单：{report.work_orders}"
    assert outcome["metrics"]["success"] == 1.0, "只诊断用例应正常完成（开结论而非失败）"
