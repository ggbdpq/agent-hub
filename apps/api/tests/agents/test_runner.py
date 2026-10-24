"""B12 InspectionAgentRunner：对外唯一入口（AgentRunner 协议）+ InMemoryRunStore 全流程。

计划 §7.2-B12：通过 RunContext 使用运行时能力，不直接 import runtime；
审批经注入的 approver 自动续跑（interrupt 循环）；结束时发 metric.usage。
"""

import json

from langchain_core.messages import AIMessage

from app.agents.runner import InspectionAgentRunner, build_context_from_claim
from app.contracts.agent import Budget, InspectionRequest
from app.contracts.fakes import InMemoryLedger, InMemoryRunStore, StaticRetriever
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


def _scripts() -> list:
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
        json.dumps({"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}]}, ensure_ascii=False),
        json.dumps({"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}]}, ensure_ascii=False),
    ]


def _test_scripts_factory():
    return sequence_llm_factory(_scripts())


async def test_runner_implements_agent_runner_protocol() -> None:
    runner = InspectionAgentRunner()
    assert callable(runner.run)
    from app.contracts.agent import AgentRunner

    p: AgentRunner = runner  # 协议即类型（结构化类型，无显式继承要求）
    assert hasattr(p, "run")


async def test_default_approver_denies_and_completes() -> None:
    """默认审批策略 = 拒绝（安全缺省）：流程完成、报告注明拒绝、不派工。"""
    ctx, collector = make_ctx(llm=_test_scripts_factory(), retriever=StaticRetriever([CHUNK]))
    runner = InspectionAgentRunner()  # 未注入 approver
    report = await runner.run("run-b12-1", REQUEST, ctx)
    assert report.status == "completed"
    assert "拒绝" in (report.notes or "")
    assert all(e.payload["tool"] != "dispatch_drone" for e in collector.of_type("tool.called"))
    assert "metric.usage" in collector.types()


async def test_injected_approver_auto_resumes_and_dispatches() -> None:
    ctx, collector = make_ctx(llm=_test_scripts_factory(), retriever=StaticRetriever([CHUNK]))
    decisions: list[dict] = []

    def approver(payload: dict) -> dict:
        decisions.append(payload)
        return {"approved": True, "approvalId": "AP-B12", "note": "自动审批：同意派机"}

    runner = InspectionAgentRunner(approver=approver)
    report = await runner.run("run-b12-2", REQUEST, ctx)
    assert len(decisions) == 1  # interrupt 精确一次
    assert "INV-005" in decisions[0]["devices"]
    assert "AP-B12" in (report.notes or "")
    assert report.work_orders and report.work_orders[0].device_id == "INV-005"


async def test_full_flow_with_in_memory_run_store() -> None:
    """InMemoryRunStore 跑通完整流程：create_run → claim → run → finish，事件序列完整。"""
    store = InMemoryRunStore()
    ledger = InMemoryLedger()
    record = store.create_run("inspection", REQUEST.model_dump(), idempotency_key="demo-key-1")
    claim = store.claim("worker-b12", lease_s=300)
    assert claim is not None and claim.run_id == record.run_id

    ctx = build_context_from_claim(
        store,
        claim,
        ledger=ledger,
        retriever=StaticRetriever([CHUNK]),
        llm_factory=_test_scripts_factory(),
        budget=Budget(max_steps=32, max_tool_calls=64, max_tokens=100_000),
    )
    runner = InspectionAgentRunner(approver=lambda p: {"approved": False, "approval_id": "", "note": "评测默认拒绝"})
    report = await runner.run(claim.run_id, REQUEST, ctx)
    assert report.status == "completed"

    finished = store.finish(claim.run_id, claim.fencing_token, "completed", result=report.model_dump())
    assert finished is True
    # 终态吸收：再次 finish 被拒
    assert store.finish(claim.run_id, claim.fencing_token, "failed") is False

    events = store.events_after(claim.run_id, 0)
    types = [e.type for e in events]
    assert types[0] == "run.queued" and "run.started" in types and types[-1] == "run.completed"
    assert "plan.created" in types
    assert "tool.called" in types and "tool.result" in types
    assert "approval.required" in types and "approval.resolved" in types
    assert "metric.usage" in types
    # 事件 seq 严格递增无空洞（§9，RunStore 保证——编排只消费）
    seqs = [e.seq for e in events]
    assert seqs == list(range(1, len(seqs) + 1))
    usage_event = next(e for e in events if e.type == "metric.usage")
    assert usage_event.payload["tool_calls"] > 0
    # 重复输入键幂等：同一 idempotency_key 返回同一 run
    again = store.create_run("inspection", REQUEST.model_dump(), idempotency_key="demo-key-1")
    assert again.run_id == record.run_id
