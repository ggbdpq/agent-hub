"""B5 Supervisor 与并行扇出：LangGraph StateGraph，失败子任务降级 partial + 覆盖率。

计划 §7.2-B5：supervisor → planner → (Send 扇出多个 inspector) → 汇总 → reporter；
每个子任务 asyncio.wait_for 超时；失败子任务记录原因并继续，最终状态 partial、
覆盖率可解释。Send 走 add_conditional_edges（U1 实测，ADR-0007）。
"""

import asyncio
import json

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

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


def _orders_json() -> str:
    return json.dumps(
        {"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}]},
        ensure_ascii=False,
    )


def _build(**kwargs):
    """构造图与收集器；脚本默认：planner + 3 台 inspector + reporter 起草 ×2。"""
    scripts = kwargs.pop(
        "scripts",
        [
            _plan_json(),
            _insp_script("INV-001", "normal", "平稳"),
            _insp_script("INV-005", "critical", "柜温漂移", with_evidence=True),
            _insp_script("INV-002", "normal", "平稳"),
            _orders_json(),
            _orders_json(),
        ],
    )
    kwargs.setdefault("retriever", StaticRetriever([CHUNK]))
    kwargs.setdefault("checkpointer", InMemorySaver())
    checkpointer = kwargs.pop("checkpointer")
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), **kwargs)
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, checkpointer=checkpointer, fixture_root=FIXTURES)
    return graph, ctx, collector, tracker


async def test_full_flow_completed_with_coverage_one() -> None:
    """INV-005 critical → interrupt 审批（拒绝派工）→ 报告 completed、覆盖率 1。"""
    graph, ctx, collector, tracker = _build()
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t1"}})
    assert "__interrupt__" in result  # critical 触发审批（B7）

    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "approval_id": "", "note": "无需无人机"}),
        {"configurable": {"thread_id": "t1"}},
    )
    report = resumed["report"]
    assert report["status"] == "completed"
    assert report["coverage"] == 1.0
    assert len(report["findings"]) == 3
    levels = {f["device_id"]: f["level"] for f in report["findings"]}
    assert levels == {"INV-001": "normal", "INV-005": "critical", "INV-002": "normal"}
    assert any("INV-005" in wo["device_id"] for wo in report["work_orders"])
    assert "拒绝" in report["notes"]
    # 事件序列：plan.created + 各节点 started/completed + inspector 证据事件 + 审批事件
    assert "plan.created" in collector.types()
    started = [e for e in collector.of_type("node.started")]
    assert any(e.node == "supervisor" for e in started)
    assert sum(1 for e in started if e.node == "inspector") == 3
    assert collector.of_type("evidence.attached")  # INV-005 检索证据入档
    assert "approval.required" in collector.types() and "approval.resolved" in collector.types()


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
    from app.tools.inspection_tools import build_registry

    async def slow_handler(args):
        await asyncio.sleep(10)

    # 先建 ctx 用于注册表构造，再建图（覆盖 get_telemetry 为慢调用）
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]))
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
    scripts = [
        json.dumps({"steps": [{"device_id": "INV-001", "checks": ["遥测比对"]}]}, ensure_ascii=False),
        _insp_script("INV-001", "normal", "平稳"),
    ]
    ctx, _collector = make_ctx(
        llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]), cancelled=True
    )
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t3"}})
    assert result["report"]["status"] == "partial"
    assert result["report"]["coverage"] == 0.0


async def test_planner_fallback_plan_used_on_garbage() -> None:
    """planner 三次都输出垃圾 → 兜底计划照常扇出（流程不中断）。

    用全 normal 设备（INV-001/002/003），避免确定性判读 critical 触发审批中断。
    """
    request = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-002", "INV-003"])
    scripts = ["垃圾", "垃圾", "垃圾"] + [
        _insp_script("INV-001", "normal", "平稳"),
        _insp_script("INV-002", "normal", "平稳"),
        _insp_script("INV-003", "normal", "平稳"),
    ]
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]))
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": request.model_dump()}, {"configurable": {"thread_id": "t4"}})
    assert result["plan"]["note"].startswith("默认全量检查计划")
    assert result["report"]["status"] == "completed"
    assert len(result["report"]["findings"]) == 3
