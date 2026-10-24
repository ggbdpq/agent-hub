"""B5 Supervisor 与并行扇出：LangGraph StateGraph，失败子任务降级 partial + 覆盖率。

计划 §7.2-B5：supervisor → planner → (Send 扇出多个 inspector) → 汇总 → reporter；
每个子任务 asyncio.wait_for 超时；失败子任务记录原因并继续，最终状态 partial、
覆盖率可解释。Send 走 add_conditional_edges（U1 实测，ADR-0007）。
"""

import asyncio
import json

from langchain_core.messages import AIMessage

from app.agents.graph import build_inspection_graph
from app.contracts.agent import InspectionRequest
from app.contracts.fakes import StaticRetriever
from app.contracts.knowledge import Chunk
from app.tools.budget import BudgetTracker
from b_testkit import FIXTURES, make_ctx, sequence_llm_factory

REQUEST = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-005", "INV-002"])

CHUNK = Chunk(
    chunk_id="doc1#异常判据#1",
    doc_id="doc1",
    title="异常判据",
    text="柜温超过 85 摄氏度立即上报并生成工单",
    source_path="corpus/doc1.md",
)


def _plan_json() -> str:
    return json.dumps(
        {
            "steps": [
                {"device_id": "INV-001", "checks": ["遥测比对"]},
                {"device_id": "INV-005", "checks": ["柜温趋势"]},
                {"device_id": "INV-002", "checks": ["遥测比对"]},
            ]
        },
        ensure_ascii=False,
    )


def _insp_script(device_id: str, level: str, summary: str, with_evidence: bool = False) -> list[AIMessage]:
    """单设备 inspector 的多轮脚本：判读 → （可选检索）→ 结论。"""
    turns = [AIMessage(content=json.dumps({"tool": "detect_anomaly", "args": {"device_id": device_id}}))]
    if with_evidence:
        turns.append(AIMessage(content=json.dumps({"tool": "search_procedures", "args": {"query": "异常判据 柜温"}})))
    final = {
        "summary": summary,
        "level": level,
        "evidence_chunk_ids": ["doc1#异常判据#1"] if with_evidence else [],
        "metrics": {},
    }
    turns.append(AIMessage(content=json.dumps({"final": final}, ensure_ascii=False)))
    return turns


def _build(**kwargs):
    """构造图与收集器；脚本默认：planner + 3 台 inspector（各 2-3 轮）。"""
    scripts = kwargs.pop(
        "scripts",
        [
            _plan_json(),
            _insp_script("INV-001", "normal", "平稳"),
            _insp_script("INV-005", "critical", "柜温漂移", with_evidence=True),
            _insp_script("INV-002", "normal", "平稳"),
        ],
    )
    kwargs.setdefault("retriever", StaticRetriever([CHUNK]))
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), **kwargs)
    return ctx, collector


async def test_full_flow_completed_with_coverage_one() -> None:
    ctx, collector = _build()
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t1"}})

    report = result["report"]
    assert report["status"] == "completed"
    assert report["coverage"] == 1.0
    assert len(report["findings"]) == 3
    levels = {f["device_id"]: f["level"] for f in report["findings"]}
    assert levels == {"INV-001": "normal", "INV-005": "critical", "INV-002": "normal"}
    # 事件序列：plan.created + 各节点 started/completed + inspector 证据事件
    assert "plan.created" in collector.types()
    started = [e for e in collector.of_type("node.started")]
    assert any(e.node == "supervisor" for e in started)
    assert sum(1 for e in started if e.node == "inspector") == 3
    assert collector.of_type("evidence.attached")  # INV-005 检索证据入档


async def test_subtask_timeout_degrades_to_partial_with_coverage() -> None:
    """1 台超时 → 报告 partial、覆盖率 2/3、原因可读（§9 不变量）。"""
    scripts = [
        _plan_json(),
        _insp_script("INV-001", "normal", "平稳"),
        # INV-005 第一步就调 get_telemetry（被覆盖为慢 handler）→ 节点级 wait_for 超时
        [
            AIMessage(content=json.dumps({"tool": "get_telemetry", "args": {"device_id": "INV-005"}})),
            _insp_script("INV-005", "critical", "柜温漂移")[0],
        ],
        _insp_script("INV-002", "normal", "平稳"),
    ]
    ctx, collector = _build(scripts=scripts)
    # 让 INV-005 的 get_telemetry 慢到必超时（节点级 wait_for 先触发）
    from app.tools.inspection_tools import build_registry

    async def slow_handler(args):
        await asyncio.sleep(10)

    base = build_registry(ctx, fixture_root=FIXTURES)
    slow_spec = base.get("get_telemetry").model_copy(update={"handler": slow_handler})
    registry = build_registry(ctx, fixture_root=FIXTURES, spec_overrides={"get_telemetry": slow_spec})
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(
        ctx, tracker, registry=registry, fixture_root=FIXTURES, task_timeout_s=0.2
    )
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t2"}})

    report = result["report"]
    assert report["status"] == "partial"
    assert abs(report["coverage"] - 2 / 3) < 5e-4  # 报告覆盖率保留 4 位小数
    assert "INV-005" in report["notes"] and "timeout" in report["notes"]
    assert len(report["findings"]) == 2
    # 失败子任务发 node.failed 事件
    failed = collector.of_type("node.failed")
    assert any(e.payload.get("device_id") == "INV-005" for e in failed)


async def test_cancelled_run_yields_partial_report() -> None:
    ctx, _ = _build(cancelled=True)
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t3"}})
    assert result["report"]["status"] == "partial"
    assert result["report"]["coverage"] == 0.0


async def test_planner_fallback_plan_used_on_garbage() -> None:
    """planner 三次都输出垃圾 → 兜底计划照常扇出（流程不中断）。"""
    scripts = ["垃圾", "垃圾", "垃圾"] + [
        _insp_script("INV-001", "normal", "平稳"),
        _insp_script("INV-005", "normal", "平稳"),
        _insp_script("INV-002", "normal", "平稳"),
    ]
    ctx, collector = _build(scripts=scripts)
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t4"}})
    assert result["plan"]["note"].startswith("默认全量检查计划")
    assert result["report"]["status"] == "completed"
    assert len(result["report"]["findings"]) == 3
