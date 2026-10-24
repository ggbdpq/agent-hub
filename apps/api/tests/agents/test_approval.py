"""B7 报告 Worker + 人工审批：LangGraph interrupt 单点设计（ADR-0007）。

计划 §7.2-B7：工单结构化起草并可幂等执行（write 走账本）；critical 触发
interrupt → approval.required → Command(resume) 注入审批结果；审批拒绝不执行；
恢复重跑时工单因账本不重复执行（§9 不变量的图内验证）。
"""

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
    text="柜温超过 85 摄氏度立即上报",
    source_path="corpus/doc1.md",
)


def _script_for(device_id: str, level: str) -> list[AIMessage]:
    return [
        AIMessage(content=json.dumps({"tool": "detect_anomaly", "args": {"device_id": device_id}})),
        AIMessage(
            content=json.dumps(
                {"final": {"summary": f"{device_id} 判读完成", "level": level, "evidence_chunk_ids": [], "metrics": {}}},
                ensure_ascii=False,
            )
        ),
    ]


def _orders_json() -> str:
    return json.dumps(
        {"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热与柜体密封"}]},
        ensure_ascii=False,
    )


def _scripts() -> list:
    """planner 1 + inspector 3 + reporter 起草 ×2（interrupt 恢复重跑会再起草一次）。"""
    return [
        json.dumps(
            {
                "steps": [
                    {"device_id": "INV-001", "checks": ["遥测比对"]},
                    {"device_id": "INV-005", "checks": ["柜温趋势"]},
                    {"device_id": "INV-002", "checks": ["遥测比对"]},
                ]
            },
            ensure_ascii=False,
        ),
        _script_for("INV-001", "normal"),
        _script_for("INV-005", "critical"),
        _script_for("INV-002", "normal"),
        _orders_json(),
        _orders_json(),
    ]


def _build(**kwargs):
    kwargs.setdefault("retriever", StaticRetriever([CHUNK]))
    ctx, collector = make_ctx(llm=sequence_llm_factory(_scripts()), **kwargs)
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES, checkpointer=InMemorySaver())
    return graph, ctx, collector, tracker


async def test_critical_findings_interrupt_for_approval() -> None:
    graph, ctx, collector, tracker = _build()
    result = await graph.ainvoke(
        {"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "run-appr-1"}}
    )
    assert "__interrupt__" in result
    value = result["__interrupt__"][0].value
    assert "INV-005" in value["devices"]
    assert "工单" in json.dumps(value["workOrders"], ensure_ascii=False) or value["workOrders"]
    assert "approval.required" in collector.types()
    # 未审批不产出报告（reporter 中断在产出前）
    assert result.get("report") is None


async def test_resume_denied_no_drone_dispatch() -> None:
    graph, ctx, collector, tracker = _build()
    result = await graph.ainvoke(
        {"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "run-appr-2"}}
    )
    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "approval_id": "", "note": "风向不合适，暂不派无人机"}),
        {"configurable": {"thread_id": "run-appr-2"}},
    )
    report = resumed["report"]
    assert report["status"] == "completed"  # 拒绝派工不影响巡检任务完成
    assert "拒绝" in report["notes"]
    drones = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "dispatch_drone"]
    assert drones == []  # 审批拒绝：不执行 dangerous
    assert "approval.resolved" in collector.types()
    assert report["work_orders"]  # 工单已创建（draft）
    assert all(wo["status"] == "draft" for wo in report["work_orders"])


async def test_resume_approved_dispatches_with_approval_id() -> None:
    graph, ctx, collector, tracker = _build()
    await graph.ainvoke(
        {"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "run-appr-3"}}
    )
    resumed = await graph.ainvoke(
        Command(resume={"approved": True, "approvalId": "AP-9", "note": "同意派无人机复核"}),
        {"configurable": {"thread_id": "run-appr-3"}},
    )
    report = resumed["report"]
    drones = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "dispatch_drone"]
    assert len(drones) == 1
    assert drones[0].payload["args"]["approval_id"] == "AP-9"
    assert "AP-9" in report["notes"]


async def test_no_critical_no_interrupt() -> None:
    """全 normal：不中断直接出报告，无审批事件。"""
    scripts = [
        json.dumps(
            {"steps": [{"device_id": "INV-001", "checks": ["遥测比对"]}]}, ensure_ascii=False
        ),
        _script_for("INV-001", "normal"),
    ]
    retriever = StaticRetriever([CHUNK])
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), retriever=retriever)
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke(
        {"request": InspectionRequest(site="SITE-A", device_ids=["INV-001"]).model_dump()},
        {"configurable": {"thread_id": "run-appr-4"}},
    )
    assert "__interrupt__" not in result
    assert result["report"]["status"] == "completed"
    assert "approval.required" not in collector.types()


async def test_reporter_rerun_does_not_duplicate_work_orders() -> None:
    """interrupt 恢复重跑 reporter：工单因可复现账本键不重复执行（§9）。"""
    graph, ctx, collector, tracker = _build()
    await graph.ainvoke(
        {"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "run-appr-5"}}
    )
    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "approval_id": "", "note": "n"}),
        {"configurable": {"thread_id": "run-appr-5"}},
    )
    dedup = [e for e in collector.of_type("tool.deduplicated") if e.payload["tool"] == "create_work_order"]
    assert len(dedup) == 1  # 恢复重跑只发生一次去重（同 run + 同设备键）
    created = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "create_work_order"]
    assert len(created) == 1  # handler 实际只执行一次
