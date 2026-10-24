"""B8 检查点与续跑：LangGraph SQLite 检查点，thread_id = run_id。

计划 §7.2-B8：在 reporter（审批等待）之前 kill → 恢复后 planner / inspector 不重跑
（检查点），已完成写工具因账本不重复执行（§9 不变量）。U1 探针已验证机制，
本文件在真实巡检图上回归。
"""

import json

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
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
        {"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}]},
        ensure_ascii=False,
    )


def _scripts() -> list:
    # planner 1 + inspector 3 + reporter 起草 ×2（恢复重跑 reporter 再起草一次）
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


async def test_crash_before_approval_resumes_without_rerunning_planner_inspector(tmp_path) -> None:
    """第一次进程跑到审批中断后"崩溃"（关闭 saver 连接）；重开同一 db 续跑。

    断言：planner/inspector 不重跑（事件计数）、工单不重复执行（账本去重）、
    恢复时图停在 reporter（get_state.next）。
    """
    db = str(tmp_path / "inspection.sqlite")
    config = {"configurable": {"thread_id": "run-8"}}  # thread_id = run_id
    collector_types: list[str] = []
    node_started_counts: dict[str, int] = {}

    ctx, collector = make_ctx(llm=sequence_llm_factory(_scripts()), retriever=StaticRetriever([CHUNK]))

    def _count() -> None:
        """全量重算（非累加）：每次调用反映当前事件的完整计数。"""
        node_started_counts.clear()
        for e in collector.events:
            if e.type == "node.started":
                node_started_counts[e.node] = node_started_counts.get(e.node, 0) + 1

    # —— 第一次"进程"：跑到审批中断 ——
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        tracker = BudgetTracker(ctx.budget)
        graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES, checkpointer=saver)
        result = await graph.ainvoke({"request": REQUEST.model_dump()}, config)
        assert result.get("report") is None
        state = await graph.aget_state(config)
        assert state.next == ("reporter",)  # 卡在审批
        _count()
        assert node_started_counts.get("planner") == 1
        assert node_started_counts.get("inspector") == 3

    # —— "崩溃重启"：重开同一 db ——
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        tracker = BudgetTracker(ctx.budget)
        graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES, checkpointer=saver)
        resumed = await graph.ainvoke(
            Command(resume={"approved": False, "approvalId": "", "note": "重启后人工复核：暂不派机"}),
            config,
        )

    assert resumed["report"]["status"] == "completed"
    assert resumed["report"]["work_orders"]
    # planner / inspector 不重跑：整轮只发生一次
    _count()
    assert node_started_counts.get("planner") == 1, "planner 被重跑"
    assert node_started_counts.get("inspector") == 3, "inspector 被重跑"
    # 工单不重复执行：create_work_order 实际执行一次，恢复重跑走账本去重
    created = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "create_work_order"]
    deduped = [e for e in collector.of_type("tool.deduplicated") if e.payload["tool"] == "create_work_order"]
    assert len(created) == 1 and len(deduped) == 1
    # 审批结果落报告
    assert "重启后人工复核" in resumed["report"]["notes"]
