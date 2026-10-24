"""A9：Worker 池与续跑入口（开发期用 ScriptedAgentRunner，B 的真实 runner 实现同一契约）。

覆盖：认领 → 构造 RunContext → runner 执行 → CAS 写终态 的完整链路；
进程内崩溃注入的续跑（attempt 递增 + run.resumed）；池化消费队列；
取消与失败两条终态路径。
"""

import asyncio

from app.contracts.fakes import ScriptedAgentRunner

from app.runtime import RuntimeWorker, WorkerConfig, WorkerPool


async def test_worker_runs_queued_run_to_completion(sqlite_store, sqlite_ledger) -> None:
    created = sqlite_store.create_run(
        "inspection", {"site": "s1", "device_ids": ["DE-001", "DE-002"]}
    )
    worker = RuntimeWorker(
        sqlite_store, sqlite_ledger, ScriptedAgentRunner(), config=WorkerConfig(worker_id="w1")
    )
    assert await worker.run_one() is True
    assert await worker.run_one() is False  # 队列已空

    run = sqlite_store.get(created.run_id)
    assert run.status == "completed" and run.attempt == 1
    assert run.result["status"] == "completed" and run.result["coverage"] == 1.0
    types = [e.type for e in sqlite_store.events_after(created.run_id, 0)]
    assert types == ["run.queued", "run.started", "node.started", "node.completed", "run.completed"]


async def test_worker_resumes_crashed_run(sqlite_store, sqlite_ledger, clock) -> None:
    """崩溃注入（进程内）：旧 worker 占坑不终态 → 租约过期 → 新 worker 续跑完成。"""
    created = sqlite_store.create_run(
        "inspection", {"site": "s1", "device_ids": ["DE-001", "DE-002", "DE-003"]}
    )
    claim = sqlite_store.claim("crashed-worker", lease_s=15)
    assert claim is not None
    from app.contracts.events import NewEvent

    sqlite_store.append_event(
        claim.run_id, claim.fencing_token, NewEvent(type="node.started", node="inspector")
    )
    # —— crashed-worker 从此消失（不 finish、不心跳）——
    clock.advance(20)  # 租约过期

    worker = RuntimeWorker(
        sqlite_store, sqlite_ledger, ScriptedAgentRunner(), config=WorkerConfig(worker_id="replacement")
    )
    assert await worker.run_one() is True

    run = sqlite_store.get(created.run_id)
    assert run.status == "completed"
    assert run.attempt == 2 and run.fencing_token == 2  # 续跑痕迹
    types = [e.type for e in sqlite_store.events_after(created.run_id, 0)]
    assert types == [
        "run.queued",
        "run.started",
        "node.started",
        "run.resumed",  # 新 worker 接管
        "node.started",
        "node.completed",
        "run.completed",
    ]
    attempts = [e.attempt for e in sqlite_store.events_after(created.run_id, 0)]
    assert attempts == [0, 1, 1, 2, 2, 2, 2]


async def test_worker_pool_drains_queue(sqlite_store, sqlite_ledger) -> None:
    """2 个 worker 的池并发消费 3 个 run，全部完成。"""
    runs = [
        sqlite_store.create_run("inspection", {"site": "s1", "device_ids": ["DE-001"]})
        for _ in range(3)
    ]
    stop = asyncio.Event()
    pool = WorkerPool(
        sqlite_store,
        sqlite_ledger,
        ScriptedAgentRunner(delay_s=0.01),
        count=2,
        config=WorkerConfig(worker_id="pool", poll_interval_s=0.02),
    )
    task = asyncio.create_task(pool.run_forever(stop))

    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        statuses = [sqlite_store.get(r.run_id).status for r in runs]
        if all(s == "completed" for s in statuses):
            break
        await asyncio.sleep(0.02)

    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert all(sqlite_store.get(r.run_id).status == "completed" for r in runs)


async def test_worker_maps_cancel_requested_partial_to_cancelled(sqlite_store, sqlite_ledger) -> None:
    """协作式取消贯通：标记 → ctx.cancelled() → runner 提前收尾 → 终态 cancelled。"""
    created = sqlite_store.create_run(
        "inspection", {"site": "s1", "device_ids": ["DE-001", "DE-002", "DE-003"]}
    )
    sqlite_store.request_cancel(created.run_id)  # 先置标记，worker 认领后即看到
    worker = RuntimeWorker(
        sqlite_store,
        sqlite_ledger,
        ScriptedAgentRunner(delay_s=0.01),
        config=WorkerConfig(worker_id="w1", cancel_poll_s=0.02),
    )
    assert await worker.run_one() is True

    run = sqlite_store.get(created.run_id)
    assert run.status == "cancelled"  # 请求取消 + 未跑完 → cancelled（见 worker 注释的映射假设）
    types = [e.type for e in sqlite_store.events_after(created.run_id, 0)]
    assert types == ["run.queued", "run.started", "node.started", "node.completed", "run.cancelled"]


async def test_worker_runner_exception_maps_to_failed(sqlite_store, sqlite_ledger) -> None:
    created = sqlite_store.create_run(
        "inspection", {"site": "s1", "device_ids": ["DE-001"]}
    )
    worker = RuntimeWorker(
        sqlite_store,
        sqlite_ledger,
        ScriptedAgentRunner(exc=RuntimeError("boom")),
        config=WorkerConfig(worker_id="w1"),
    )
    assert await worker.run_one() is True  # 失败也是"处理完成"（终态落盘）
    run = sqlite_store.get(created.run_id)
    assert run.status == "failed" and run.error is not None and "boom" in run.error
    types = [e.type for e in sqlite_store.events_after(created.run_id, 0)]
    assert types == ["run.queued", "run.started", "node.started", "run.failed"]
