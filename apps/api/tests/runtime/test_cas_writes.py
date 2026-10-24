"""A4：CAS 写入——append_event / finish 带 fencing 校验，过期执行者写入一律被拒。

移植 contracts-v1 核心语义：worker1 暂停 → 租约过期 → worker2 接管 → worker1
迟到写入被拒，最终结果是 worker2 的；以及 fencing 精确语义（过期未被接管不误拒）。
"""

import pytest
from app.contracts.events import NewEvent
from app.contracts.runtime import LeaseLost


def test_stale_writer_rejected_after_takeover(sqlite_store, clock) -> None:
    """核心不变量：旧执行者被接管后，事件与终态写入一律被拒；结果不被覆盖。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    c1 = sqlite_store.claim("w1", lease_s=15)
    assert c1 is not None
    clock.advance(20)  # w1 暂停，租约过期
    c2 = sqlite_store.claim("w2", lease_s=15)
    assert c2 is not None and c2.fencing_token == 2 and c2.attempt == 2

    with pytest.raises(LeaseLost):
        sqlite_store.append_event(c1.run_id, c1.fencing_token, NewEvent(type="node.started"))
    assert sqlite_store.finish(c1.run_id, c1.fencing_token, "completed", result={"by": "w1"}) is False
    assert sqlite_store.finish(c2.run_id, c2.fencing_token, "completed", result={"by": "w2"}) is True
    final = sqlite_store.get(created.run_id)
    assert final.result == {"by": "w2"} and final.status == "completed"


def test_expired_lease_without_takeover_can_finish(sqlite_store, clock) -> None:
    """fencing 精确语义：过期但未被接管时 token 仍是当前值，合法写入不误拒。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    clock.advance(20)  # 过期，但无人认领
    appended = sqlite_store.append_event(
        claim.run_id, claim.fencing_token, NewEvent(type="node.started")
    )
    assert appended.seq == 3  # seq1=run.queued, seq2=run.started
    assert sqlite_store.finish(claim.run_id, claim.fencing_token, "completed") is True
    assert sqlite_store.get(created.run_id).status == "completed"


def test_terminal_absorbs_all_writes(sqlite_store) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.finish(claim.run_id, claim.fencing_token, "completed") is True
    assert sqlite_store.claim("w2", lease_s=15) is None  # 终态不可认领
    with pytest.raises(LeaseLost):
        sqlite_store.append_event(claim.run_id, claim.fencing_token, NewEvent(type="node.started"))
    assert sqlite_store.finish(claim.run_id, claim.fencing_token, "failed") is False
    assert sqlite_store.get(created.run_id).status == "completed"


def test_append_event_requires_running_and_current_token(sqlite_store) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    with pytest.raises(LeaseLost):
        sqlite_store.append_event(created.run_id, 0, NewEvent(type="node.started"))  # queued：无租约
    with pytest.raises(KeyError):
        sqlite_store.append_event("no-such-run", 1, NewEvent(type="node.started"))
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    with pytest.raises(LeaseLost):
        sqlite_store.append_event(claim.run_id, claim.fencing_token + 1, NewEvent(type="node.started"))


def test_finish_requires_running_and_current_token(sqlite_store) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    assert sqlite_store.finish(created.run_id, 0, "completed") is False  # queued
    assert sqlite_store.finish("no-such-run", 1, "completed") is False
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.finish(claim.run_id, claim.fencing_token + 5, "completed") is False
    assert sqlite_store.get(created.run_id).status == "running"  # 被拒不落终态


def test_finish_rejects_non_terminal_status(sqlite_store) -> None:
    sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    with pytest.raises(ValueError):
        sqlite_store.finish(claim.run_id, claim.fencing_token, "running")  # running 不是终态
    with pytest.raises(ValueError):
        sqlite_store.finish(claim.run_id, claim.fencing_token, "queued")


def test_finish_clears_lease_and_sets_result_error(sqlite_store) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.finish(
        claim.run_id, claim.fencing_token, "failed", error="boom"
    ) is True
    run = sqlite_store.get(created.run_id)
    assert run.status == "failed" and run.error == "boom"
    assert run.lease_owner is None and run.lease_expires_at is None  # 租约随终态释放


def test_append_event_returns_full_run_event(sqlite_store) -> None:
    """append_event 返回持久化后的 RunEvent：seq/attempt/ts 由 store 分配。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    event = sqlite_store.append_event(
        claim.run_id, claim.fencing_token,
        NewEvent(type="node.started", node="inspector", payload={"k": "v"}),
    )
    assert event.run_id == created.run_id
    assert event.seq == 3 and event.attempt == 1  # 前 2 条是 run.queued / run.started
    assert event.node == "inspector" and event.payload == {"k": "v"}
    assert event.ts.tzinfo is not None  # aware UTC


def test_terminal_event_recorded_with_error_payload(sqlite_store) -> None:
    """finish 落对应终态事件，payload 带 error（与 fakes 一致）。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.finish(claim.run_id, claim.fencing_token, "partial", error="2/3") is True
    events = sqlite_store.events_after(created.run_id, 0)
    assert [e.type for e in events] == ["run.queued", "run.started", "run.partial"]
    assert events[-1].attempt == 1 and events[-1].payload == {"error": "2/3"}


def test_events_after_slices_for_last_event_id(sqlite_store) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    for i in range(3):
        sqlite_store.append_event(
            claim.run_id, claim.fencing_token, NewEvent(type="node.completed", payload={"i": i})
        )
    assert [e.seq for e in sqlite_store.events_after(created.run_id, 0)] == [1, 2, 3, 4, 5]
    assert [e.seq for e in sqlite_store.events_after(created.run_id, 3)] == [4, 5]
    assert sqlite_store.events_after("no-such-run", 0) == []
