"""C8：评测框架——只依赖 AgentRunner 契约；指标手算样例 + 双 runner 端到端。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402

from evals.agent_eval import (  # noqa: E402
    EventTrace,
    aggregate,
    grade_case,
    order_satisfied,
    tool_calls_of,
)
from app.contracts.agent import Budget, Finding, InspectionReport, InspectionRequest  # noqa: E402
from app.contracts.events import NewEvent  # noqa: E402
from app.contracts.fakes import InMemoryLedger, ScriptedAgentRunner  # noqa: E402
from evals.agent_eval import run_case  # noqa: E402


# ---------- 指标手算 ----------

def test_order_satisfied_hand_computed() -> None:
    types = ["tool.called", "tool.called", "tool.result"]
    tools = ["detect_anomaly", "create_work_order", "create_work_order"]
    assert order_satisfied(types, tools, "detect_anomaly", "create_work_order") is True
    assert order_satisfied(types, tools, "create_work_order", "detect_anomaly") is False
    assert order_satisfied([], [], "a", "b") is False  # a 未出现 → 约束不满足


def test_tool_calls_of_extracts_sequence() -> None:
    trace = EventTrace(
        [
            NewEvent(type="node.started", node="inspector"),
            NewEvent(type="tool.called", payload={"tool": "get_telemetry", "args": {"device_id": "INV-001"}}),
            NewEvent(type="tool.called", payload={"tool": "detect_anomaly", "args": {"device_id": "INV-001"}}),
        ]
    )
    calls = tool_calls_of(trace)
    assert [c.tool for c in calls] == ["get_telemetry", "detect_anomaly"]
    assert calls[0].args == {"device_id": "INV-001"}


def test_grade_case_metrics_hand_computed() -> None:
    """异常识别 P=2/3、R=1/2；工具调用完全命中；参数准确率 1/2。"""
    report = InspectionReport(
        findings=[
            Finding(device_id="INV-005", level="critical", summary="x"),
            Finding(device_id="INV-006", level="critical", summary="y"),  # 误报
            Finding(device_id="INV-001", level="normal", summary="ok"),
        ],
        work_orders=[],
        status="completed",
        coverage=1.0,
    )
    case = {
        "cid": "T1",
        "input": {"site": "SITE-A", "device_ids": ["INV-005", "INV-006", "INV-001"]},
        "expected": {
            "anomaly_devices": ["INV-005", "INV-002"],
            "tools_expected": ["get_telemetry", "detect_anomaly"],
            "forbidden_tools": ["dispatch_drone"],
            "work_orders": [],
            "status_allowed": ["completed"],
            "tools_order_required": [],
            "evidence_required": False,
        },
        "fault": None,
    }
    events = [
        NewEvent(type="tool.called", payload={"tool": "get_telemetry", "args": {"device_id": "INV-005"}}),
        NewEvent(type="tool.called", payload={"tool": "detect_anomaly", "args": {"device_id": "INV-005"}}),
    ]
    result = grade_case(case, report=report, events=events, latency_ms=10.0, crashed=False)
    m = result["metrics"]
    assert m["anomaly_precision"] == pytest.approx(1 / 2)  # 预测 {INV-005, INV-006} 命中 1
    assert m["anomaly_recall"] == pytest.approx(1 / 2)  # 期望 {INV-005, INV-002} 命中 1
    assert m["tool_precision"] == pytest.approx(1.0)
    assert m["tool_recall"] == pytest.approx(1.0)
    assert m["forbidden_calls"] == 0
    assert m["success"] == 1.0
    assert m["steps"] == 2


# ---------- 端到端：契约 ScriptedAgentRunner ----------

async def test_scripted_runner_end_to_end_records_events() -> None:
    from evals.datasets import load_agent_cases

    case = next(c for c in load_agent_cases()["cases"] if c["cid"] == "E01")
    runner = ScriptedAgentRunner()
    outcome = await run_case(runner, case, retriever=None)
    assert outcome["report"].status == "completed"
    assert outcome["metrics"]["success"] == 1.0
    # 契约替身不发工具事件：工具召回诚实为 0（管道口径，不虚构）
    assert outcome["metrics"]["tool_recall"] == 0.0
    assert len(outcome["events"]) >= 2  # node.started/node.completed 已记录
    assert outcome["events"][0]["type"] == "node.started"


async def test_crashed_run_counts_as_failure() -> None:
    from evals.datasets import load_agent_cases

    case = next(c for c in load_agent_cases()["cases"] if c["cid"] == "E01")
    runner = ScriptedAgentRunner(exc=RuntimeError("boom"))
    outcome = await run_case(runner, case, retriever=None)
    assert outcome["crashed"] is True
    assert outcome["metrics"]["success"] == 0.0


# ---------- 端到端：确定性工具型脚本 runner ----------

async def test_scripted_tools_runner_detects_and_orders() -> None:
    from evals.datasets import load_agent_cases
    from evals.scripted_tools import ScriptedToolAgentRunner
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.retriever import HybridRetriever
    from app.knowledge.splitting import split_corpus
    from evals.datasets import corpus_root

    retriever = HybridRetriever(split_corpus(corpus_root()), embedder=HashingEmbedder(dim=512))
    case = next(c for c in load_agent_cases()["cases"] if c["cid"] == "M01")  # INV-005 单异常
    outcome = await run_case(ScriptedToolAgentRunner(), case, retriever=retriever)
    m = outcome["metrics"]
    assert m["anomaly_recall"] == 1.0
    assert m["anomaly_precision"] == 1.0
    assert m["tool_recall"] == 1.0
    assert m["order_satisfaction"] == 1.0  # detect_anomaly → create_work_order
    assert m["forbidden_calls"] == 0
    assert outcome["report"].work_orders, "异常设备应有工单"
    assert outcome["report"].findings[0].evidence_chunk_ids, "证据应来自真实检索"


async def test_scripted_tools_fault_timeout_degrades_to_partial() -> None:
    from evals.datasets import load_agent_cases
    from evals.scripted_tools import ScriptedToolAgentRunner

    case = next(c for c in load_agent_cases()["cases"] if c["cid"] == "H01")  # tool_timeout
    outcome = await run_case(ScriptedToolAgentRunner(), case, retriever=None)
    assert outcome["report"].status in case["expected"]["status_allowed"]
    assert outcome["metrics"]["success"] == 1.0  # 流程成功：按白名单降级而非崩溃
    writes = [c for c in tool_calls_of(EventTrace(outcome["events"])) if c.tool == "create_work_order"]
    assert len(writes) <= 1


# ---------- X1：E1 工具 P/R 双口径（process-strict vs goal-oriented） ----------

def _goal_case(**overrides) -> dict:
    """双口径用例模板：INV-005 期望异常，get_telemetry/detect_anomaly 必调。"""
    expected = {
        "anomaly_devices": ["INV-005"],
        "tools_expected": ["get_telemetry", "detect_anomaly"],
        "forbidden_tools": ["create_work_order", "dispatch_drone"],
        "work_orders": [],
        "status_allowed": ["completed"],
        "tools_order_required": [],
        "evidence_required": False,
    }
    expected.update(overrides.pop("expected", {}))
    return {
        "cid": "G1",
        "input": {"site": "SITE-A", "device_ids": ["INV-005"]},
        "expected": expected,
        "fault": None,
        **overrides,
    }


def _goal_report() -> InspectionReport:
    return InspectionReport(
        findings=[Finding(device_id="INV-005", level="warning", summary="结论已出")],
        work_orders=[],
        status="completed",
        coverage=1.0,
    )


def test_goal_oriented_recall_satisfied_by_conclusion() -> None:
    """get_telemetry 未调用但结论覆盖设备（简报注入路径）：process-strict 漏召回，goal-oriented 不计漏调。"""
    case = _goal_case()
    events = [NewEvent(type="tool.called", payload={"tool": "detect_anomaly", "args": {"device_id": "INV-005"}})]
    m = grade_case(case, report=_goal_report(), events=events, latency_ms=1.0, crashed=False)["metrics"]
    assert m["tool_recall"] == pytest.approx(0.5)  # process-strict：get_telemetry 漏调
    assert m["tool_recall_goal"] == pytest.approx(1.0)  # goal-oriented：信息目标已由结论达成
    assert m["tool_precision_goal"] == pytest.approx(1.0)


def test_goal_oriented_recall_miss_without_conclusion() -> None:
    """get_telemetry 未调用且设备无结论：两口径都计漏调（信息目标未达成）。"""
    case = _goal_case()
    report = InspectionReport(findings=[], work_orders=[], status="partial", coverage=0.0)
    events = [NewEvent(type="tool.called", payload={"tool": "detect_anomaly", "args": {"device_id": "INV-005"}})]
    m = grade_case(case, report=report, events=events, latency_ms=1.0, crashed=False)["metrics"]
    assert m["tool_recall"] == pytest.approx(0.5)
    assert m["tool_recall_goal"] == pytest.approx(0.5)


def test_goal_oriented_list_devices_satisfied_by_conclusion() -> None:
    """list_devices（站点级清单）同样按信息目标判定：结论覆盖 → 不计漏调。"""
    case = _goal_case(expected={"tools_expected": ["get_telemetry", "detect_anomaly", "list_devices"]})
    events = [
        NewEvent(type="tool.called", payload={"tool": "get_telemetry", "args": {"device_id": "INV-005"}}),
        NewEvent(type="tool.called", payload={"tool": "detect_anomaly", "args": {"device_id": "INV-005"}}),
    ]
    m = grade_case(case, report=_goal_report(), events=events, latency_ms=1.0, crashed=False)["metrics"]
    assert m["tool_recall"] == pytest.approx(2 / 3)
    assert m["tool_recall_goal"] == pytest.approx(1.0)
    assert m["drone_policy_ok"] is None  # 期望集不含 dispatch_drone


def test_goal_oriented_drone_default_deny_not_called() -> None:
    """dispatch_drone 旧口径按'批准'写入期望；默认拒绝口径期望不调用：未调用 → 合规。"""
    case = _goal_case(
        expected={"tools_expected": ["get_telemetry", "detect_anomaly", "create_work_order", "search_procedures", "dispatch_drone"]}
    )
    events = [
        NewEvent(type="tool.called", payload={"tool": "get_telemetry", "args": {"device_id": "INV-005"}}),
        NewEvent(type="tool.called", payload={"tool": "detect_anomaly", "args": {"device_id": "INV-005"}}),
        NewEvent(type="tool.called", payload={"tool": "search_procedures", "args": {"query": "q"}}),
        NewEvent(type="tool.called", payload={"tool": "create_work_order", "args": {"device_id": "INV-005"}}),
    ]
    m = grade_case(case, report=_goal_report(), events=events, latency_ms=1.0, crashed=False)["metrics"]
    assert m["tool_recall"] == pytest.approx(4 / 5)  # process-strict：dispatch_drone 漏调
    assert m["tool_recall_goal"] == pytest.approx(1.0)  # goal-oriented：默认拒绝口径下期望即不调用
    assert m["tool_precision_goal"] == pytest.approx(1.0)
    assert m["drone_policy_ok"] is True


def test_goal_oriented_drone_called_is_violation() -> None:
    """默认拒绝口径下调用了 dispatch_drone：goal-oriented 精度受罚 + 派工合规标记 False。"""
    case = _goal_case(
        expected={"tools_expected": ["get_telemetry", "detect_anomaly", "create_work_order", "search_procedures", "dispatch_drone"]}
    )
    events = [
        NewEvent(type="tool.called", payload={"tool": "get_telemetry", "args": {"device_id": "INV-005"}}),
        NewEvent(type="tool.called", payload={"tool": "detect_anomaly", "args": {"device_id": "INV-005"}}),
        NewEvent(type="tool.called", payload={"tool": "search_procedures", "args": {"query": "q"}}),
        NewEvent(type="tool.called", payload={"tool": "create_work_order", "args": {"device_id": "INV-005"}}),
        NewEvent(type="tool.called", payload={"tool": "dispatch_drone", "args": {"device_id": "INV-005"}}),
    ]
    m = grade_case(case, report=_goal_report(), events=events, latency_ms=1.0, crashed=False)["metrics"]
    assert m["drone_policy_ok"] is False
    assert m["tool_precision_goal"] == pytest.approx(4 / 5)  # dispatch_drone 调用计为多余
    assert m["tool_recall_goal"] == pytest.approx(1.0)


def test_aggregate_includes_goal_columns() -> None:
    per_case = [
        {"metrics": {"tool_precision": 0.5, "tool_recall": 0.5, "tool_precision_goal": 1.0,
                     "tool_recall_goal": 1.0, "drone_policy_ok": True, "success": 1.0}},
        {"metrics": {"tool_precision": 0.5, "tool_recall": 0.5, "tool_precision_goal": 0.5,
                     "tool_recall_goal": 0.5, "drone_policy_ok": False, "success": 0.0}},
    ]
    agg = aggregate(per_case)
    assert agg["tool_precision_goal"] == pytest.approx(0.75)
    assert agg["tool_recall_goal"] == pytest.approx(0.75)
    assert agg["drone_policy_ok_rate"] == pytest.approx(0.5)


def test_aggregate_hand_computed() -> None:
    per_case = [
        {"metrics": {"success": 1.0, "anomaly_precision": 1.0, "anomaly_recall": 0.5,
                     "tool_precision": 1.0, "tool_recall": 1.0, "forbidden_calls": 0,
                     "steps": 4, "latency_ms": 10.0, "param_accuracy": 1.0}},
        {"metrics": {"success": 0.0, "anomaly_precision": 0.5, "anomaly_recall": 0.5,
                     "tool_precision": 0.5, "tool_recall": 0.5, "forbidden_calls": 2,
                     "steps": 6, "latency_ms": 30.0, "param_accuracy": 0.5}},
    ]
    agg = aggregate(per_case)
    assert agg["success_rate"] == pytest.approx(0.5)
    assert agg["anomaly_recall"] == pytest.approx(0.5)
    assert agg["forbidden_calls_total"] == 2
    assert agg["latency_ms_p50"] == pytest.approx(20.0)
    assert agg["latency_ms_p95"] == pytest.approx(29.0)
