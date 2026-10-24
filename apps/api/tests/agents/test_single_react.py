"""B11 单智能体基线（供消融）：同样的工具，一个 ReAct 循环做完全部。

计划 §7.2-B11：不规划、不分工、无子任务隔离、无审批中断（消融变量）；
但运行时强制仍在同一执行器上（权限/预算/账本/审批凭证），结论 level 仍以
确定性判读为准、证据仍须真实观测（§9 不变量不消融）。
"""

import json

from langchain_core.messages import AIMessage

from app.agents.single_react import SingleReActRunner
from app.contracts.agent import InspectionRequest
from app.contracts.fakes import FakeLLM, StaticRetriever
from app.contracts.knowledge import Chunk
from app.tools.budget import BudgetTracker
from b_testkit import FIXTURES, make_ctx

CHUNK = Chunk(
    chunk_id="doc1#异常判据#1",
    doc_id="doc1",
    title="异常判据",
    text="柜温超限上报",
    source_path="corpus/doc1.md",
)

REQUEST = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-005", "INV-002"])


def _act(tool: str, **args) -> AIMessage:
    return AIMessage(content=json.dumps({"tool": tool, "args": args}, ensure_ascii=False))


def _runner(script, **kwargs):
    ctx, collector = make_ctx(llm=FakeLLM.factory(script), retriever=StaticRetriever([CHUNK]), **kwargs)
    tracker = BudgetTracker(ctx.budget)
    runner = SingleReActRunner(fixture_root=FIXTURES)
    return runner, ctx, collector, tracker


async def test_single_agent_covers_devices_in_one_loop() -> None:
    script = [
        _act("detect_anomaly", device_id="INV-001"),
        _act("detect_anomaly", device_id="INV-005"),
        _act("search_procedures", query="异常判据"),
        AIMessage(
            content=json.dumps(
                {
                    "final": {
                        "findings": [
                            {"device_id": "INV-001", "level": "normal", "summary": "平稳", "evidence_chunk_ids": [], "metrics": {}},
                            {
                                "device_id": "INV-005",
                                "level": "critical",
                                "summary": "柜温漂移",
                                "evidence_chunk_ids": ["doc1#异常判据#1"],
                                "metrics": {},
                            },
                        ],
                        "orders": [
                            {"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}
                        ],
                        "notes": "单循环完成",
                    }
                },
                ensure_ascii=False,
            )
        ),
    ]
    runner, ctx, collector, tracker = _runner(script)
    report = await runner.run("run-single-1", REQUEST, ctx, tracker)
    assert report.status == "partial"  # 只巡检了 2/3 台设备
    assert abs(report.coverage - 2 / 3) < 5e-4
    levels = {f.device_id: f.level for f in report.findings}
    assert levels == {"INV-001": "normal", "INV-005": "critical"}
    assert report.findings[1].evidence_chunk_ids == ["doc1#异常判据#1"]
    assert len(report.work_orders) == 1 and report.work_orders[0].device_id == "INV-005"
    # 全部事件挂在 supervisor 节点（单智能体无分工）
    assert collector.events and all(e.node == "supervisor" for e in collector.events)


async def test_level_still_forced_by_deterministic_judgement() -> None:
    """运行时不变量不消融：模型谎报 level 也被确定性判读校正。"""
    script = [
        AIMessage(
            content=json.dumps(
                {
                    "final": {
                        "findings": [
                            {"device_id": "INV-005", "level": "normal", "summary": "没事", "evidence_chunk_ids": [], "metrics": {}}
                        ],
                        "orders": [],
                    }
                },
                ensure_ascii=False,
            )
        ),
    ]
    runner, ctx, _, tracker = _runner(script)
    report = await runner.run("run-single-2", REQUEST, ctx, tracker)
    assert report.findings[0].level == "critical"


async def test_dispatch_denied_without_approval_channel() -> None:
    """单智能体无审批中断：dispatch_drone 被 approval_required 拒绝并记入 notes。"""
    script = [
        _act("dispatch_drone", device_id="INV-005", reason="r"),
        AIMessage(
            content=json.dumps(
                {"final": {"findings": [], "orders": [], "notes": "尝试派机"}},
                ensure_ascii=False,
            )
        ),
    ]
    runner, ctx, collector, tracker = _runner(script)
    report = await runner.run("run-single-3", REQUEST, ctx, tracker)
    denied = [
        e for e in collector.of_type("tool.denied") if e.payload.get("reason") == "approval_required"
    ]
    assert denied and denied[0].payload["tool"] == "dispatch_drone"
    assert report.coverage == 0.0  # 没有产出 findings


async def test_step_budget_exhausted_yields_partial() -> None:
    script = [_act("list_devices", site="SITE-A")] * 10
    runner, ctx, collector, tracker = _runner(script)
    tracker2 = BudgetTracker(ctx.budget.model_copy(update={"max_steps": 3}))
    report = await runner.run("run-single-4", REQUEST, ctx, tracker2)
    assert report.status == "partial"
    assert "步数预算" in (report.notes or "")
