"""A8：协作式取消——request_cancel 只置标记，不改状态与租约；终态由持有租约的
worker CAS 写入；取消与完成竞争时以先成功 CAS 者为准，另一方被拒。
"""

import threading

import pytest
from app.contracts.events import NewEvent
from app.runtime import LeaseLost


def test_cancel_is_cooperative_flag_only(sqlite_store) -> None:
    """移植 contracts-v1：标记不动状态，cancelled 终态由持租约者写入。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.is_cancel_requested(created.run_id) is False
    sqlite_store.request_cancel(created.run_id)
    assert sqlite_store.is_cancel_requested(created.run_id) is True
    # 取消不改变状态与租约：终态由持有租约的 worker 写入
    run = sqlite_store.get(created.run_id)
    assert run.status == "running" and run.lease_owner == "w1"
    assert run.cancel_requested is True
    assert sqlite_store.finish(created.run_id, claim.fencing_token, "cancelled") is True
    assert sqlite_store.get(created.run_id).status == "cancelled"


def test_cancel_queued_does_not_block_claim(sqlite_store) -> None:
    """queued 也可置标记；认领不因标记而阻塞——取消是协作式（编排层自查）。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    sqlite_store.request_cancel(created.run_id)
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.is_cancel_requested(created.run_id) is True


def test_request_cancel_idempotent_on_terminal(sqlite_store) -> None:
    """终态请求取消：幂等返回原记录，不置标记（终态不可被取消污染）。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.finish(claim.run_id, claim.fencing_token, "completed") is True
    record = sqlite_store.request_cancel(created.run_id)
    assert record.status == "completed"
    assert record.cancel_requested is False
    assert sqlite_store.is_cancel_requested(created.run_id) is False


def test_request_cancel_missing_run_raises(sqlite_store) -> None:
    with pytest.raises(KeyError):
        sqlite_store.request_cancel("no-such-run")


def test_is_cancel_requested_missing_run_is_false(sqlite_store) -> None:
    assert sqlite_store.is_cancel_requested("no-such-run") is False


def test_cancel_and_finish_race_first_cas_wins(sqlite_store) -> None:
    """取消与完成竞争：两个 worker 同时 CAS 写终态，恰好一个成功、一个被拒。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    sqlite_store.request_cancel(created.run_id)

    results: dict[str, bool] = {}

    def finish_as(name: str, status: str) -> None:
        results[name] = sqlite_store.finish(
            created.run_id, claim.fencing_token, status, result={"by": name}
        )

    t1 = threading.Thread(target=finish_as, args=("canceller", "cancelled"))
    t2 = threading.Thread(target=finish_as, args=("completer", "completed"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert sum(results.values()) == 1  # 恰好一个 CAS 成功
    final = sqlite_store.get(created.run_id)
    expected_status = "cancelled" if results.get("canceller") else "completed"
    assert final.status == expected_status
    assert final.result == {"by": "canceller" if results.get("canceller") else "completer"}
    # 败者再写被拒（终态吸收）
    with pytest.raises(LeaseLost):
        sqlite_store.append_event(created.run_id, claim.fencing_token, NewEvent(type="node.started"))


def test_finish_cancelled_emits_run_cancelled_event(sqlite_store) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    sqlite_store.request_cancel(created.run_id)
    assert sqlite_store.finish(claim.run_id, claim.fencing_token, "cancelled") is True
    types = [e.type for e in sqlite_store.events_after(created.run_id, 0)]
    assert types == ["run.queued", "run.started", "run.cancelled"]
