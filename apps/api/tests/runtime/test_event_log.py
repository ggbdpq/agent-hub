"""A6：事件日志与续传——并发追加 seq 无空洞无重复，events_after 支撑 Last-Event-ID。

seq 在事务内 MAX(seq)+1 分配（随 A4 append_event 落地），本文件是并发不变量的
专项验证：单实例多协程 + 多实例（模拟多进程）两档压力。
"""

import asyncio
import threading

from app.contracts.events import NewEvent

from app.runtime import SqliteRunStore


def test_event_seq_monotonic_and_resumable(sqlite_store) -> None:
    """移植 contracts-v1：10 个并发追加（to_thread 挤同一实例），seq 严格 1..N。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None

    async def append_ten() -> None:
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    sqlite_store.append_event,
                    claim.run_id,
                    claim.fencing_token,
                    NewEvent(type="node.completed", payload={"i": i}),
                )
                for i in range(10)
            )
        )

    asyncio.run(append_ten())
    # seq1=run.queued, seq2=run.started，10 个并发追加为 seq3..12
    events = sqlite_store.events_after(claim.run_id, 0)
    assert [e.seq for e in events] == list(range(1, 13))
    # run.queued 是 attempt=0 的系统事件，其余全部归属本次执行（attempt=1）
    assert [e.seq for e in events if e.attempt == 1] == list(range(2, 13))
    # Last-Event-ID 续传：从 6 之后拿增量
    assert [e.seq for e in sqlite_store.events_after(claim.run_id, 6)] == [7, 8, 9, 10, 11, 12]


def test_event_seq_no_holes_across_instances(clock, sqlite_path) -> None:
    """4 个独立连接并发追加（模拟 4 进程写同一库）：busy_timeout 下 seq 仍无空洞无重复。"""
    creator = SqliteRunStore(sqlite_path, clock=clock)
    created = creator.create_run("inspection", {"site": "s1"})
    claim = creator.claim("w1", lease_s=15)
    assert claim is not None

    errors: list[Exception] = []

    def append_many(store: SqliteRunStore) -> None:
        try:
            for i in range(5):
                store.append_event(
                    claim.run_id,
                    claim.fencing_token,
                    NewEvent(type="tool.result", node="inspector", payload={"w": id(store), "i": i}),
                )
        except Exception as exc:  # noqa: BLE001 - 测试收集所有失败定性
            errors.append(exc)

    stores = [SqliteRunStore(sqlite_path, clock=clock) for _ in range(4)]
    threads = [threading.Thread(target=append_many, args=(s,)) for s in stores]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for s in stores:
        s.close()

    assert errors == []
    seqs = [e.seq for e in creator.events_after(created.run_id, 0)]
    # 2 条系统事件 + 4 实例 x 5 条并发追加 = 22 条，无空洞无重复
    assert seqs == list(range(1, 23))
    payloads = [e.payload for e in creator.events_after(created.run_id, 2)]
    assert len(payloads) == 20  # 每条追加都真实落库


def test_events_after_resumed_run_carries_new_attempt(sqlite_store, clock) -> None:
    """续跑后事件按归属 attempt 记录：复盘可区分两次执行。"""
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim1 = sqlite_store.claim("w1", lease_s=15)
    assert claim1 is not None
    sqlite_store.append_event(
        claim1.run_id, claim1.fencing_token, NewEvent(type="node.started", node="inspector")
    )
    clock.advance(20)
    claim2 = sqlite_store.claim("w2", lease_s=15)
    assert claim2 is not None
    event = sqlite_store.append_event(
        claim2.run_id, claim2.fencing_token, NewEvent(type="node.started", node="inspector")
    )
    assert event.attempt == 2  # 新执行记录
    types = [e.type for e in sqlite_store.events_after(created.run_id, 0)]
    assert types == ["run.queued", "run.started", "node.started", "run.resumed", "node.started"]
    attempts = [e.attempt for e in sqlite_store.events_after(created.run_id, 0)]
    assert attempts == [0, 1, 1, 2, 2]
