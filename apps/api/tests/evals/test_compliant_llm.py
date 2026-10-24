"""X2/X3/X4 共用合规替身：角色路由与策略锁定（信息面与真实智能体一致，无答案泄露）。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import json  # noqa: E402

from langchain_core.messages import HumanMessage, SystemMessage  # noqa: E402

from evals.compliant_llm import CompliantLLMFactory  # noqa: E402


def _msg(content: str) -> HumanMessage:
    return HumanMessage(content)


def _reply(system: str, humans: list[str]) -> str:
    model = CompliantLLMFactory().build()
    import asyncio

    return asyncio.run(model.ainvoke([SystemMessage(system)] + [_msg(h) for h in humans])).content


# ---------- planner ----------

def test_planner_covers_all_requested_devices() -> None:
    reply = _reply("你是巡检规划器。", [json.dumps({"site": "S", "devices": ["INV-001", "WT-002"], "goal": "g"})])
    steps = json.loads(reply)["steps"]
    assert [s["device_id"] for s in steps] == ["INV-001", "WT-002"]


# ---------- inspector（简报驱动：遥测已在简报，不再调 get_telemetry） ----------

_INSPECTOR_SYSTEM = "你是设备巡检分析员，负责一台设备的异常判读与解释。可用工具（只读）：…"
_BRIEF = json.dumps({"device_id": "INV-005", "type": "pv_inverter", "telemetry_summary": {"cabinet_temp_c": {"max": 90.0}}, "checks": ["遥测比对"]})


def test_inspector_skips_telemetry_brief_driven_then_detects() -> None:
    assert json.loads(_reply(_INSPECTOR_SYSTEM, [_BRIEF])) == {"tool": "detect_anomaly", "args": {}}


def test_inspector_searches_on_anomaly_then_finals_with_observed_level_and_evidence() -> None:
    detect_receipt = json.dumps(
        {"tool": "detect_anomaly", "status": "ok", "result": {"device_id": "INV-005", "level": "critical", "anomalies": [{"metric": "cabinet_temp_c", "kind": "threshold"}]}},
        ensure_ascii=False,
    )
    action = json.loads(_reply(_INSPECTOR_SYSTEM, [_BRIEF, detect_receipt]))
    assert action == {"tool": "search_procedures", "args": {"query": "异常判据与处置", "k": 3}}

    search_receipt = json.dumps(
        {"tool": "search_procedures", "status": "ok", "result": {"chunks": [{"chunk_id": "doc1#判据#1"}, {"chunk_id": "doc1#判据#2"}]}},
        ensure_ascii=False,
    )
    final = json.loads(_reply(_INSPECTOR_SYSTEM, [_BRIEF, detect_receipt, search_receipt]))["final"]
    assert final["level"] == "critical"  # level 取自观测，不泄露答案
    assert final["evidence_chunk_ids"] == ["doc1#判据#1", "doc1#判据#2"]  # 只引用观测过的 chunk


def test_inspector_normal_device_finals_directly() -> None:
    detect_receipt = json.dumps(
        {"tool": "detect_anomaly", "status": "ok", "result": {"device_id": "INV-001", "level": "normal", "anomalies": []}},
        ensure_ascii=False,
    )
    final = json.loads(_reply(_INSPECTOR_SYSTEM, [_BRIEF, detect_receipt]))["final"]
    assert final["level"] == "normal" and final["evidence_chunk_ids"] == []


# ---------- reporter ----------

def test_reporter_drafts_orders_for_received_targets() -> None:
    targets = json.dumps([{"device_id": "INV-005", "level": "critical", "summary": "判读 critical 级异常：cabinet_temp_c超阈值"}], ensure_ascii=False)
    orders = json.loads(_reply("你是工单起草员。为异常设备起草检修工单。", [targets]))["orders"]
    assert orders and orders[0]["device_id"] == "INV-005" and orders[0]["action"]
    # goal 要求工单注明设备编号与异常参数：标题同时含设备编号与指标名（均来自上游 finding）
    assert "INV-005" in orders[0]["title"] and "cabinet_temp_c" in orders[0]["title"]


# ---------- single（单 ReAct：无简报，遥测必须自取） ----------

_SINGLE_SYSTEM = "你是巡检智能体：独自完成全部设备的巡检。可用工具：…"
_REQUEST = json.dumps({"site": "SITE-A", "devices": ["INV-001", "INV-005"], "goal": "巡检并处置：异常设备按规程生成工单"}, ensure_ascii=False)


def test_single_fetches_telemetry_first_per_device() -> None:
    action = json.loads(_reply(_SINGLE_SYSTEM, [_REQUEST]))
    assert action == {"tool": "get_telemetry", "args": {"device_id": "INV-001"}}


def test_single_final_covers_all_devices_with_observed_levels_and_goal_gated_orders() -> None:
    receipts = [
        json.dumps({"tool": "get_telemetry", "status": "ok", "result": {"device_id": "INV-001"}}, ensure_ascii=False),
        json.dumps({"tool": "get_telemetry", "status": "ok", "result": {"device_id": "INV-005"}}, ensure_ascii=False),
        json.dumps({"tool": "detect_anomaly", "status": "ok", "result": {"device_id": "INV-001", "level": "normal", "anomalies": []}}, ensure_ascii=False),
        json.dumps({"tool": "detect_anomaly", "status": "ok", "result": {"device_id": "INV-005", "level": "warning", "anomalies": [{"metric": "cabinet_temp_c", "kind": "trend"}]}}, ensure_ascii=False),
        json.dumps({"tool": "search_procedures", "status": "ok", "result": {"chunks": [{"chunk_id": "c1"}]}}, ensure_ascii=False),
    ]
    final = json.loads(_reply(_SINGLE_SYSTEM, [_REQUEST] + receipts))["final"]
    assert {f["device_id"] for f in final["findings"]} == {"INV-001", "INV-005"}
    assert [f["level"] for f in final["findings"] if f["device_id"] == "INV-005"] == ["warning"]
    assert [o["device_id"] for o in final["orders"]] == ["INV-005"]  # 只给观测到的异常设备开工单
    order = final["orders"][0]
    assert "INV-005" in order["title"] and "cabinet_temp_c" in order["title"]  # 设备编号+异常指标

    readonly = json.dumps({**json.loads(_REQUEST), "goal": "只诊断不生成工单：输出异常结论与依据，禁止创建工单。"}, ensure_ascii=False)
    final_ro = json.loads(_reply(_SINGLE_SYSTEM, [readonly] + receipts))["final"]
    assert final_ro["orders"] == []


# ---------- 计量（费用估算基线） ----------

def test_factory_counts_calls_and_token_estimates() -> None:
    factory = CompliantLLMFactory()
    model = factory.build()
    import asyncio

    asyncio.run(model.ainvoke([SystemMessage("你是巡检规划器。"), _msg(json.dumps({"devices": ["INV-001"]}))]))
    assert factory.calls == 1 and factory.calls_by_role["planner"] == 1
    snap = factory.snapshot()
    assert snap["input_tokens_est"] > 0 and snap["output_tokens_est"] > 0
