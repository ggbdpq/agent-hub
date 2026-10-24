"""A10（P1）：结构化日志与运行指标。

- 每条运行日志带 run_id / attempt / seq / node 字段（缺失的字段不出现，不编造）；
- get_metrics()：在途数、回收次数、租约丢失次数、平均时长；
- worker / reclaimer 接线后指标随之变化。
"""

import json
import logging

from app.contracts.fakes import ScriptedAgentRunner
from app.runtime import (
    Reclaimer,
    RuntimeMetrics,
    RuntimeWorker,
    WorkerConfig,
    configure_run_logging,
    get_metrics,
)
from app.runtime.structured_log import RUN_LOG_NAME, RunLogFormatter, log_event

# -- get_metrics --


def test_get_metrics_empty_store(sqlite_store) -> None:
    snap = get_metrics(sqlite_store)
    assert snap["in_flight"] == 0
    assert snap["avg_duration_s"] is None  # 无终态样本时不编造平均时长
    assert snap["claims"] == 0 and snap["lease_lost"] == 0 and snap["reclaimed"] == 0


def test_stats_reflect_run_lifecycle(sqlite_store, clock) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    assert sqlite_store.stats()["in_flight"] == 0  # queued 不算在途
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    assert sqlite_store.stats()["in_flight"] == 1
    clock.advance(5)
    assert sqlite_store.finish(claim.run_id, claim.fencing_token, "completed") is True
    stats = sqlite_store.stats()
    assert stats["in_flight"] == 0
    assert stats["avg_duration_s"] == 5.0  # created_at → updated_at（终态时刻）


def test_metrics_counters_snapshot() -> None:
    metrics = RuntimeMetrics()
    metrics.claims += 2
    metrics.lease_lost += 1
    metrics.reclaimed += 3
    metrics.finished += 2
    assert metrics.snapshot() == {
        "claims": 2,
        "finished": 2,
        "lease_lost": 1,
        "reclaimed": 3,
    }


def test_reclaimer_increments_reclaimed_metric(sqlite_store, clock) -> None:
    created = sqlite_store.create_run("inspection", {"site": "s1"})
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    clock.advance(20)
    metrics = RuntimeMetrics()
    reclaimer = Reclaimer(sqlite_store, interval_s=10.0, metrics=metrics)
    reclaimer.scan_once()
    reclaimer.scan_once()  # 同一 (run, attempt) 不重复计数
    snap = get_metrics(sqlite_store, metrics)
    assert snap["reclaimed"] == 1


# -- worker 接线 --


async def test_worker_updates_metrics(sqlite_store, sqlite_ledger, clock) -> None:
    created = sqlite_store.create_run(
        "inspection", {"site": "s1", "device_ids": ["DE-001"]}
    )
    metrics = RuntimeMetrics()
    worker = RuntimeWorker(
        sqlite_store,
        sqlite_ledger,
        ScriptedAgentRunner(),
        config=WorkerConfig(worker_id="w1"),
        metrics=metrics,
    )
    assert await worker.run_one() is True
    snap = get_metrics(sqlite_store, metrics)
    assert snap["claims"] == 1 and snap["finished"] == 1
    assert snap["in_flight"] == 0 and snap["lease_lost"] == 0


async def test_worker_counts_lease_lost_when_superseded(
    sqlite_store, sqlite_ledger, clock
) -> None:
    """runner 执行期间被第三方接管 → 迟到写入 LeaseLost → finish 被拒 → lease_lost 计数。"""
    from app.contracts.events import NewEvent

    created = sqlite_store.create_run(
        "inspection", {"site": "s1", "device_ids": ["DE-001"]}
    )

    class HijackRunner:
        async def run(self, run_id, input, ctx):
            await ctx.emit(NewEvent(type="node.started"))  # 正常写入
            clock.advance(20)  # 模拟 worker 卡顿：租约过期
            hijack = sqlite_store.claim("hijacker", lease_s=15)  # 被顶替
            assert hijack is not None and hijack.run_id == run_id
            await ctx.emit(NewEvent(type="node.completed"))  # 旧 token → LeaseLost
            raise AssertionError("不应到达这里")  # emit 已抛异常

    metrics = RuntimeMetrics()
    worker = RuntimeWorker(
        sqlite_store,
        sqlite_ledger,
        HijackRunner(),
        config=WorkerConfig(worker_id="victim"),
        metrics=metrics,
    )
    assert await worker.run_one() is True  # 兜底路径：写入被拒不崩溃

    snap = get_metrics(sqlite_store, metrics)
    assert snap["lease_lost"] == 1
    assert snap["finished"] == 0  # 终态写入被拒，不计 finished
    # run 仍被 hijacker 持有（victim 的终态写入作废），终局由接管者写
    final = sqlite_store.get(created.run_id)
    assert final.status == "running" and final.lease_owner == "hijacker"
    assert sqlite_store.finish(created.run_id, 2, "completed") is True


# -- 结构化日志 --


def test_log_event_carries_run_fields() -> None:
    logger = logging.getLogger("runtime.test.log_event")
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger.addHandler(Capture())
    logger.setLevel(logging.DEBUG)
    log_event(
        logger, logging.INFO, "run.finished",
        run_id="r-1", attempt=2, seq=7, node="inspector",
    )
    record = records[-1]
    assert record.run_id == "r-1" and record.attempt == 2
    assert record.seq == 7 and record.node == "inspector"


def test_formatter_emits_single_line_json() -> None:
    logger = logging.getLogger("runtime.test.formatter")
    logger.propagate = False
    line_holder: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            line_holder.append(self.format(record))

    handler = Capture()
    handler.setFormatter(RunLogFormatter())
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    log_event(
        logger, logging.INFO, "run.event",
        run_id="r-2", attempt=1, seq=3, node="planner", event="plan.created",
    )
    payload = json.loads(line_holder[-1])
    assert payload["message"] == "run.event"
    assert payload["level"] == "INFO"
    assert payload["run_id"] == "r-2"
    assert payload["attempt"] == 1 and payload["seq"] == 3
    assert payload["node"] == "planner" and payload["event"] == "plan.created"
    assert payload["ts"].startswith("20")  # ISO 时间戳
    assert line_holder[-1].count("\n") == 0  # 单行


def test_worker_and_reclaimer_write_structured_logs(
    sqlite_store, sqlite_ledger, clock
) -> None:
    created = sqlite_store.create_run(
        "inspection", {"site": "s1", "device_ids": ["DE-001"]}
    )
    claim = sqlite_store.claim("w1", lease_s=15)
    assert claim is not None
    clock.advance(20)

    lines: list[str] = []
    logger = logging.getLogger(RUN_LOG_NAME)
    logger.propagate = False
    logger.setLevel(logging.DEBUG)

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = Capture()
    handler.setFormatter(RunLogFormatter())
    logger.addHandler(handler)
    try:
        configure_run_logging(level=logging.DEBUG)  # 幂等：不重复挂 handler
        metrics = RuntimeMetrics()
        worker = RuntimeWorker(
            sqlite_store,
            sqlite_ledger,
            ScriptedAgentRunner(),
            config=WorkerConfig(worker_id="w2"),
            metrics=metrics,
        )
        reclaimer = Reclaimer(sqlite_store, interval_s=10.0, metrics=metrics)
        reclaimer.scan_once()  # w1 的过期租约 → lease.expired 日志

        import asyncio

        asyncio.run(worker.run_one())

        assert lines, "worker/reclaimer 应产生结构化日志"
        payloads = [json.loads(line) for line in lines]
        events = {p["message"] for p in payloads}
        assert "lease.expired" in events
        assert "run.claimed" in events and "run.finished" in events
        claimed = next(p for p in payloads if p["message"] == "run.claimed")
        assert claimed["run_id"] == created.run_id and claimed["attempt"] == 2
        finished = next(p for p in payloads if p["message"] == "run.finished")
        assert finished["run_id"] == created.run_id and finished["status"] == "completed"
    finally:
        logger.removeHandler(handler)
