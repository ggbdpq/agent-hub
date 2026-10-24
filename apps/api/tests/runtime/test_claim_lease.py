"""A3：租约认领与 fencing token（含 store.heartbeat 租约原语；心跳循环/回收器在 A5）。

移植 contracts-v1 语义：并发认领单一胜者、token/attempt 单调递增、过期 running
可被重新认领（run.resumed）、心跳续租与过期失败。
"""

import threading
from datetime import timedelta

from app.runtime import SqliteRunStore


def test_create_and_claim_assigns_token_and_attempt(sqlite_store, clock) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert claim.run_id == created.run_id
    assert claim.fencing_token == 1 and claim.attempt == 1
    assert claim.kind == "inspection" and claim.input == {"site": "s1"}

    run = sqlite_store.get(claim.run_id)
    assert run.status == "running"
    assert run.lease_owner == "w1"
    assert run.lease_expires_at == clock() + timedelta(seconds=15)


def test_concurrent_claim_single_winner(sqlite_store) -> None:
    """同实例多次认领（串行化后逐次判定）：只有一个 queued run，只有一个胜者。"""
    sqlite_store.create_run("inspection", {"site": "s1"})
    claims = [sqlite_store.claim(f"w{i}", lease_s=15) for i in range(5)]
    winners = [c for c in claims if c is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 1


def test_concurrent_claim_single_winner_across_instances(clock, sqlite_path) -> None:
    """四个独立连接（模拟四个进程）在 WAL+busy_timeout 下并发认领：仍只有一个胜者。"""
    creator = SqliteRunStore(sqlite_path, clock=clock)
    creator.create_run("inspection", {"site": "s1"})

    results: dict[int, object] = {}

    def attempt(i: int) -> None:
        store = SqliteRunStore(sqlite_path, clock=clock)
        results[i] = store.claim(f"w{i}", lease_s=15)
        store.close()

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    winners = [c for c in results.values() if c is not None]
    assert len(winners) == 1
    assert winners[0].fencing_token == 1


def test_claim_fifo_order(sqlite_store) -> None:
    """多个可认领 run 按创建序认领（created_at 相同冻结时钟下按插入序 rowid 决胜）。"""
    first = sqlite_store.create_run("inspection", {"site": "s1"})
    second = sqlite_store.create_run("inspection", {"site": "s2"})
    c1 = sqlite_store.claim("w1", lease_s=15)
    c2 = sqlite_store.claim("w2", lease_s=15)
    assert c1 is not None and c1.run_id == first.run_id
    assert c2 is not None and c2.run_id == second.run_id


def test_running_with_valid_lease_not_claimable(sqlite_store) -> None:
    sqlite_store.create_run("inspection", {"site": "s1"})
    assert sqlite_store.claim("w1", lease_s=15) is not None
    assert sqlite_store.claim("w2", lease_s=15) is None  # 租约仍有效


def test_expired_running_is_reclaimed_with_new_token(sqlite_store, clock) -> None:
    """核心续跑语义：过期 running 被重新认领 → attempt/token 递增、owner 更换。"""
    sqlite_store.create_run("inspection", {"site": "s1"})
    c1 = sqlite_store.claim("w1", lease_s=15)
    assert c1 is not None
    clock.advance(20)  # 租约过期，无人接管
    c2 = sqlite_store.claim("w2", lease_s=15)
    assert c2 is not None
    assert c2.run_id == c1.run_id
    assert c2.fencing_token == 2 and c2.attempt == 2
    run = sqlite_store.get(c1.run_id)
    assert run.lease_owner == "w2"


def test_heartbeat_extends_lease(sqlite_store, clock) -> None:
    sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    clock.advance(5)
    assert sqlite_store.heartbeat(claim.run_id, claim.fencing_token, lease_s=15) is True
    run = sqlite_store.get(claim.run_id)
    assert run.lease_expires_at == clock() + timedelta(seconds=15)
    assert run.lease_owner == "w1"  # 心跳不换主


def test_heartbeat_fails_after_expiry(sqlite_store, clock) -> None:
    sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.heartbeat(claim.run_id, claim.fencing_token, lease_s=15) is True
    clock.advance(20)
    assert sqlite_store.heartbeat(claim.run_id, claim.fencing_token, lease_s=15) is False


def test_heartbeat_fails_on_missing_run_or_wrong_token_or_queued(sqlite_store) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    assert sqlite_store.heartbeat("no-such-run", 1, lease_s=15) is False  # 不存在的 run
    assert sqlite_store.heartbeat(created.run_id, 999, lease_s=15) is False  # queued：无租约
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.heartbeat(claim.run_id, claim.fencing_token + 1, lease_s=15) is False  # token 不符
