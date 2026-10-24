"""B6 上下文隔离：子任务只接收"任务简报 + 该设备的数据摘要"，不接收全局消息历史。

计划 §7.2-B6：断言子任务输入 Token 上限；断言子任务看不到其他设备数据；
Supervisor 只汇总结构化结果（图状态里没有消息历史）。
"""

import json

from langchain_core.messages import AIMessage

from app.agents.briefs import BRIEF_TOKEN_LIMIT, build_subtask_brief, load_brief
from app.agents.graph import build_inspection_graph
from app.contracts.agent import InspectionRequest
from app.contracts.fakes import StaticRetriever
from app.contracts.knowledge import Chunk
from app.tools.budget import BudgetTracker, estimate_tokens
from app.tools.devices import load_devices
from b_testkit import FIXTURES, make_ctx, recording_llm_factory

REQUEST = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-002", "INV-003"])

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
                {"final": {"summary": "ok", "level": level, "evidence_chunk_ids": [], "metrics": {}}},
                ensure_ascii=False,
            )
        ),
    ]


def test_brief_token_cap_holds_for_all_devices() -> None:
    """全部 20 台设备的简报都在 Token 上限内（上限为代码常量，CI 保证）。"""
    for device in load_devices(FIXTURES):
        brief = load_brief(device.device_id, ["遥测比对", "柜温趋势"], fixture_root=FIXTURES)
        tokens = estimate_tokens(json.dumps(brief, ensure_ascii=False))
        assert tokens < BRIEF_TOKEN_LIMIT, (device.device_id, tokens)


def test_brief_truncation_limits_apply() -> None:
    """简报字段收敛：checks 截断、遥测指标截断（防御异常输入）。"""
    from app.tools.devices import load_telemetry, telemetry_summary

    devices = {d.device_id: d for d in load_devices(FIXTURES)}
    series = load_telemetry("INV-001", FIXTURES)
    brief = build_subtask_brief(
        "INV-001",
        [f"检查{i}" for i in range(20)],  # 超 MAX_CHECKS
        devices["INV-001"],
        telemetry_summary(series),
    )
    assert len(brief["checks"]) <= 6
    assert len(brief["telemetry_summary"]) <= 12
    assert "points" not in json.dumps(brief)  # 完整序列永不进简报


async def test_inspector_prompts_contain_only_own_device() -> None:
    """整图运行：inspector 的模型输入只含本设备，看不到其他设备数据。"""
    scripts = [
        json.dumps(
            {
                "steps": [
                    {"device_id": "INV-001", "checks": ["遥测比对"]},
                    {"device_id": "INV-002", "checks": ["遥测比对"]},
                    {"device_id": "INV-003", "checks": ["遥测比对"]},
                ]
            },
            ensure_ascii=False,
        ),
        _script_for("INV-001", "normal"),
        _script_for("INV-002", "normal"),
        _script_for("INV-003", "normal"),
    ]
    factory, received = recording_llm_factory(scripts)
    ctx, _ = make_ctx(llm=factory, retriever=StaticRetriever([CHUNK]))
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "iso1"}})

    assert result["report"]["status"] == "completed"
    # inspector 首轮输入 = [System, Human(简报)]（消息数恰为 2），简报特征字段 telemetry_summary
    inspector_first_inputs = []
    for msgs in received:
        humans = [m for m in msgs if m.type == "human"]
        if len(msgs) == 2 and humans and "telemetry_summary" in str(humans[0].content):
            inspector_first_inputs.append(str(humans[0].content))
    assert len(inspector_first_inputs) == 3  # 每台设备一次首提
    all_ids = {"INV-001", "INV-002", "INV-003"}
    for text in inspector_first_inputs:
        own = next(d for d in all_ids if d in text)
        leaked = all_ids - {own}
        assert not any(other in text for other in leaked), f"{own} 的简报泄漏了他机数据"


async def test_graph_state_has_no_message_history() -> None:
    """图状态只含结构化字段：无全局 messages 历史（Supervisor 只汇总结构化结果）。"""
    scripts = [
        json.dumps(
            {"steps": [{"device_id": "INV-001", "checks": ["遥测比对"]}]}, ensure_ascii=False
        ),
        _script_for("INV-001", "normal"),
    ]
    factory, received = recording_llm_factory(scripts)
    ctx, _ = make_ctx(llm=factory, retriever=StaticRetriever([CHUNK]))
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke(
        {"request": InspectionRequest(site="SITE-A", device_ids=["INV-001"]).model_dump()},
        {"configurable": {"thread_id": "iso2"}},
    )
    assert "messages" not in result  # 无消息历史字段
    # findings 是结构化 Finding dict（有 device_id/level），不是自由文本
    for finding in result["findings"]:
        assert set(finding) >= {"device_id", "level", "summary", "evidence_chunk_ids", "metrics"}
    for failure in result.get("failures") or []:
        assert set(failure) >= {"device_id", "reason"}
