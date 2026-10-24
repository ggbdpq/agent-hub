"""契约替身的行为规范（contracts-v1）：A 的 SQLite 实现必须通过同一套测试。

覆盖计划 §9 的运行时不变量：并发认领单一胜者、被顶替写入被拒、终态吸收、
seq 无空洞可续传、幂等账本、取消标记。
"""

import asyncio
from datetime import UTC, datetime

import pytest

from app.contracts.events import NewEvent
from app.contracts.fakes import (
    FakeLLM,
    ScriptedAgentRunner,
    StaticRetriever,
)
from app.contracts.knowledge import Chunk
from app.contracts.runtime import LeaseLost
from app.contracts.agent import Budget, InspectionReport, InspectionRequest, RunContext


def _noop_ctx(ledger, retriever) -> RunContext:
    async def emit(event: NewEvent) -> None:
        pass

    return RunContext(
        emit=emit,
        cancelled=lambda: False,
        budget=Budget(),
        ledger=ledger,
        retriever=retriever,
        llm=FakeLLM.factory(["ok"]),
    )


def test_create_and_claim_assigns_token_and_attempt(store) -> None:
    store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    assert claim is not None and claim.fencing_token == 1 and claim.attempt == 1
    run = store.get(claim.run_id)
    assert run.status == "running" and run.lease_owner == "w1"


def test_idempotency_key_returns_same_run(store) -> None:
    a = store.create_run("inspection", {"site": "s1"}, idempotency_key="op-1")
    b = store.create_run("inspection", {"site": "s1"}, idempotency_key="op-1")
    assert a.run_id == b.run_id


def test_concurrent_claim_single_winner(store) -> None:
    store.create_run("inspection", {"site": "s1"})
    claims = [store.claim(f"w{i}", lease_s=15) for i in range(5)]
    winners = [c for c in claims if c is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 1


def test_stale_writer_rejected_after_takeover(store, clock) -> None:
    """核心不变量：worker1 暂停 → 租约过期被回收 → worker2 接管 → worker1 迟到写入被拒。"""
    created = store.create_run("inspection", {"site": "s1"})
    c1 = store.claim("w1", lease_s=15)
    assert c1 is not None
    clock.advance(20)  # w1 的租约过期
    c2 = store.claim("w2", lease_s=15)
    assert c2 is not None and c2.fencing_token == 2 and c2.attempt == 2

    with pytest.raises(LeaseLost):
        store.append_event(c1.run_id, c1.fencing_token, NewEvent(type="node.started"))
    assert store.finish(c1.run_id, c1.fencing_token, "completed", result={"by": "w1"}) is False
    assert store.finish(c2.run_id, c2.fencing_token, "completed", result={"by": "w2"}) is True
    assert store.get(created.run_id).result == {"by": "w2"}


def test_expired_lease_without_takeover_can_finish(store, clock) -> None:
    """fencing 精确语义：过期但未被接管时 token 仍是当前值，合法写入不误拒。"""
    created = store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    clock.advance(20)  # 过期，但无人认领
    # seq1=run.queued, seq2=run.started，本次追加应为 seq3
    appended = store.append_event(claim.run_id, claim.fencing_token, NewEvent(type="node.started"))
    assert appended.seq == 3
    assert store.finish(claim.run_id, claim.fencing_token, "completed") is True
    assert store.get(created.run_id).status == "completed"


def test_heartbeat_fails_after_expiry(store, clock) -> None:
    store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    assert claim is not None
    assert store.heartbeat(claim.run_id, claim.fencing_token, lease_s=15) is True
    clock.advance(20)
    assert store.heartbeat(claim.run_id, claim.fencing_token, lease_s=15) is False


def test_terminal_absorbs_all_writes(store) -> None:
    created = store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    assert store.finish(claim.run_id, claim.fencing_token, "completed") is True
    assert store.claim("w2", lease_s=15) is None  # 终态不可认领
    with pytest.raises(LeaseLost):
        store.append_event(claim.run_id, claim.fencing_token, NewEvent(type="node.started"))
    assert store.finish(claim.run_id, claim.fencing_token, "failed") is False
    assert store.get(created.run_id).status == "completed"


def test_event_seq_monotonic_and_resumable(store) -> None:
    """并发追加时 seq 无空洞无重复；events_after 支撑 Last-Event-ID 续传。"""
    created = store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)

    async def append_ten() -> None:
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    store.append_event,
                    claim.run_id,
                    claim.fencing_token,
                    NewEvent(type="node.completed", payload={"i": i}),
                )
                for i in range(10)
            )
        )

    asyncio.run(append_ten())
    # seq1=run.queued, seq2=run.started，10 个并发追加为 seq3..12
    events = store.events_after(claim.run_id, 0)
    assert [e.seq for e in events] == list(range(1, 13))
    assert [e.seq for e in store.events_after(claim.run_id, 6)] == [7, 8, 9, 10, 11, 12]


def test_cancel_is_cooperative_flag_only(store) -> None:
    created = store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    assert store.is_cancel_requested(created.run_id) is False
    store.request_cancel(created.run_id)
    assert store.is_cancel_requested(created.run_id) is True
    # 取消不改变状态与租约：终态由持有租约的 worker 写入
    assert store.get(created.run_id).status == "running"
    assert store.finish(created.run_id, claim.fencing_token, "cancelled") is True


def test_ledger_dedup_and_unknown_state(ledger) -> None:
    first = ledger.begin("r1:inspector:1:call-1", "create_work_order", args_hash="h1")
    assert first.status == "in_progress"
    second = ledger.begin("r1:inspector:1:call-1", "create_work_order", args_hash="h1")
    assert second is first  # 同键返回原记录
    ledger.complete("r1:inspector:1:call-1", {"order_id": "DD001"})
    done = ledger.begin("r1:inspector:1:call-1", "create_work_order", args_hash="h1")
    assert done.status == "completed" and done.result == {"order_id": "DD001"}

    ledger.begin("r1:inspector:2:call-2", "create_work_order", args_hash="h2")
    ledger.fail("r1:inspector:2:call-2", "上游超时，执行状态未知", retryable=True)
    assert ledger.begin("r1:inspector:2:call-2", "x", "h2").status == "unknown"


def test_static_retriever_returns_empty_when_no_match() -> None:
    retriever = StaticRetriever(
        [
            Chunk(
                chunk_id="d#巡检准备#1",
                doc_id="d",
                title="巡检准备",
                text="巡检前核对设备清单",
                source_path="corpus/d.md",
            )
        ]
    )
    assert asyncio.run(retriever.search("直流", k=5)) == []  # 空结果不编造
    assert len(asyncio.run(retriever.search("巡检 设备", k=5))) == 1


def test_scripted_runner_partial_on_cancel(store, ledger, retriever) -> None:
    runner = ScriptedAgentRunner()
    request = InspectionRequest(site="s1", device_ids=["DE-001", "DE-002", "DE-003"])
    seen: list[NewEvent] = []

    async def emit(event: NewEvent) -> None:
        seen.append(event)

    polls = {"n": 0}

    def cancelled() -> bool:
        # 替身每处理一台设备前轮询一次取消；第二次轮询起返回 True → 1 台完成、2 台跳过
        polls["n"] += 1
        return polls["n"] > 1

    ctx = RunContext(
        emit=emit,
        cancelled=cancelled,
        budget=Budget(),
        ledger=ledger,
        retriever=retriever,
        llm=FakeLLM.factory(["ok"]),
    )
    report = asyncio.run(runner.run("r1", request, ctx))
    assert report.status == "partial" and report.coverage == pytest.approx(1 / 3)
    assert any(e.type == "node.completed" for e in seen)


def test_scripted_runner_completed(store, ledger, retriever) -> None:
    runner = ScriptedAgentRunner()
    request = InspectionRequest(site="s1", device_ids=["DE-001", "DE-002"])
    report: InspectionReport = asyncio.run(runner.run("r1", request, _noop_ctx(ledger, retriever)))
    assert report.status == "completed" and report.coverage == 1.0
    assert [f.device_id for f in report.findings] == ["DE-001", "DE-002"]


def test_fake_llm_replays_script() -> None:
    from langchain_core.messages import AIMessage, ToolCall

    build = FakeLLM.factory(
        [
            AIMessage(
                content="",
                tool_calls=[
                    ToolCall(name="get_telemetry", args={"device_id": "DE-001"}, id="c1")
                ],
            ),
            "结论：正常",
        ]
    )
    model = build()
    first = asyncio.run(model.ainvoke("检查"))
    assert first.tool_calls[0]["name"] == "get_telemetry"
    second = asyncio.run(model.ainvoke("继续"))
    assert second.content == "结论：正常"


def test_run_event_roundtrip_schema() -> None:
    from app.contracts import RunEvent

    event = RunEvent(
        type="tool.result", node="inspector", payload={"ok": True},
        run_id="r1", seq=3, attempt=1, ts=datetime.now(UTC),
    )
    parsed = RunEvent.model_validate_json(event.model_dump_json())
    assert parsed == event


def test_tool_spec_requires_schema_and_permission() -> None:
    from pydantic import BaseModel

    from app.contracts import ToolSpec

    class Args(BaseModel):
        device_id: str

    class Out(BaseModel):
        ok: bool

    async def handler(args: Args) -> Out:
        return Out(ok=True)

    spec = ToolSpec(
        name="get_telemetry",
        description="读取遥测",
        input_schema=Args,
        output_schema=Out,
        effect="read",
        allowed_nodes={"inspector"},
        handler=handler,
    )
    assert spec.effect == "read"
    with pytest.raises(ValueError):
        ToolSpec(  # effect 超出 Literal 枚举必须被拒
            name="bad", description="d", input_schema=Args, output_schema=Out,
            effect="bananas", allowed_nodes={"inspector"}, handler=handler,
        )
