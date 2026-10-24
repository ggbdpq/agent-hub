"""A5：心跳循环与回收器（进程内部分）。

- 心跳间隔 = 租约 / 3（默认租约 15s → 心跳 5s）；
- 心跳失败（被顶替/过期/终态）置 lost 事件 → 编排层经 ctx.cancelled() 停下；
- 回收器启动时与每 interval 秒扫描过期租约；同一 (run, attempt) 只上报一次。
"""

import asyncio

from app.runtime import HeartbeatLoop, Reclaimer


async def test_heartbeat_loop_interval_is_lease_over_three(sqlite_store) -> None:
    store = sqlite_store
    store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    assert claim is not None
    loop = HeartbeatLoop(store, claim.run_id, claim.fencing_token, lease_s=15)
    assert loop.interval_s == 5.0  # 15 / 3


async def test_heartbeat_loop_extends_lease_while_healthy(sqlite_store, clock) -> None:
    store = sqlite_store
    store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    assert claim is not None
    lost = asyncio.Event()
    loop = HeartbeatLoop(store, claim.run_id, claim.fencing_token, lease_s=15, lost=lost, interval_s=0.05)
    task = loop.start()
    try:
        clock.advance(5)  # 手动推进：下一次心跳应续到 advance 后 +15s
        await asyncio.sleep(0.2)
        assert not lost.is_set()
        run = store.get(claim.run_id)
        assert run.lease_owner == "w1"  # 心跳不换主，租约一直有效
    finally:
        loop.stop()
        await task


async def test_heartbeat_loop_signals_lost_on_expiry(sqlite_store, clock) -> None:
    """租约过期后的下一次心跳失败 → lost 事件置位（编排层据此停止）。"""
    store = sqlite_store
    store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=1)
    assert claim is not None
    lost = asyncio.Event()
    loop = HeartbeatLoop(store, claim.run_id, claim.fencing_token, lease_s=1, lost=lost, interval_s=0.05)
    task = loop.start()
    try:
        clock.advance(2)  # 租约过期且无人接管
        await asyncio.sleep(0.2)
        assert lost.is_set()
    finally:
        loop.stop()
        await task


async def test_reclaimer_scans_expired_once_per_attempt(sqlite_store, clock) -> None:
    store = sqlite_store
    created = store.create_run("inspection", {"site": "s1"})
    claim = store.claim("w1", lease_s=15)
    assert claim is not None
    clock.advance(20)  # 租约过期

    seen: list = []
    reclaimer = Reclaimer(store, interval_s=10.0, on_expired=seen.append)
    first = reclaimer.scan_once()
    assert [r.run_id for r in first] == [created.run_id]
    assert len(seen) == 1
    # 同一 (run, attempt) 不重复上报，避免回收计数虚增
    assert reclaimer.scan_once() == []


async def test_reclaimer_run_forever_scans_at_startup(sqlite_store, clock) -> None:
    store = sqlite_store
    created = store.create_run("inspection", {"site": "s1"})
    store.claim("w1", lease_s=15)
    clock.advance(20)

    seen: list = []
    reclaimer = Reclaimer(store, interval_s=10.0, on_expired=seen.append)
    stop = asyncio.Event()
    task = asyncio.create_task(reclaimer.run_forever(stop))
    await asyncio.sleep(0.05)  # 启动即扫描一次（不等第一个 interval）
    assert len(seen) == 1 and seen[0].run_id == created.run_id
    stop.set()
    await asyncio.wait_for(task, timeout=2)


async def test_reclaimer_sees_reclaimed_run_again_after_new_attempt_expires(sqlite_store, clock) -> None:
    """被接管后的新 attempt 再过期时，作为新的 (run, attempt) 上报。"""
    store = sqlite_store
    created = store.create_run("inspection", {"site": "s1"})
    store.claim("w1", lease_s=15)
    clock.advance(20)
    reclaimer = Reclaimer(store, interval_s=10.0)
    assert len(reclaimer.scan_once()) == 1

    store.claim("w2", lease_s=15)  # w2 接管（attempt=2）
    clock.advance(20)  # w2 也失联过期
    expired = reclaimer.scan_once()
    assert [r.run_id for r in expired] == [created.run_id]
    assert expired[0].attempt == 2
