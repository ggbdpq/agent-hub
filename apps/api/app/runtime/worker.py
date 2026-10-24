"""RuntimeWorker（A9）：认领 → 构造 RunContext → AgentRunner.run → CAS 写终态。

职责边界（ADR-0007）：本类只管"谁有权执行"（租约/fencing）与"写入是否有效"
（CAS）；图状态检查点由 B 在编排层用 LangGraph SQLite Saver 负责。开发期 runner
用 contracts.fakes.ScriptedAgentRunner；B 的真实 runner 实现同一 AgentRunner 契约，
本类无需改动。

终态映射假设（编排层 runner 返回 report.status 之外的运行时信号）：
- report.status == completed → completed；
- report.status == partial 且（取消标记被请求或心跳已失联）→ cancelled（因中断而未跑完）；
- report.status == partial（自然降级，如子任务超时跳过）→ partial；
- runner 抛异常 → failed（error 记录异常摘要）。
CAS 失败（finish False）即被顶替或已终态，本次写入作废——fencing 兜底。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from app.contracts.agent import Budget, InspectionRequest, RunContext
from app.contracts.events import NewEvent
from app.contracts.runtime import Claim, ToolLedger
from app.runtime.heartbeat import HeartbeatLoop
from app.runtime.metrics import RuntimeMetrics
from app.runtime.store import SqliteRunStore
from app.runtime.structured_log import log_event

logger = logging.getLogger("agent-hub.runtime.worker")


@dataclass
class WorkerConfig:
    worker_id: str = "worker"
    lease_s: int = 15  # 默认租约 15s → 心跳 5s（计划 §7.1-A5）
    poll_interval_s: float = 0.2
    cancel_poll_s: float = 0.2


class CancelMonitor:
    """轮询取消标记的后台任务：cancelled() 读本地事件，不阻塞事件循环。"""

    def __init__(self, store: SqliteRunStore, run_id: str, poll_s: float = 0.2) -> None:
        self._store = store
        self._run_id = run_id
        self._poll_s = poll_s
        self._flag = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is not None:
            raise RuntimeError("CancelMonitor 已启动")
        self._task = asyncio.create_task(self._poll(), name=f"cancel:{self._run_id}")
        return self._task

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()

    @property
    def requested(self) -> bool:
        return self._flag.is_set()

    async def _poll(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._poll_s)
                if await asyncio.to_thread(self._store.is_cancel_requested, self._run_id):
                    self._flag.set()
                    return
        except asyncio.CancelledError:
            pass  # stop() 的取消即正常退出


class RuntimeWorker:
    """单个 worker 协程：run_forever 循环认领，或 run_one 只处理一个 run。"""

    def __init__(
        self,
        store: SqliteRunStore,
        ledger: ToolLedger,
        runner,  # AgentRunner 契约（避免 import 契约 Protocol 以外的耦合，鸭子类型即可）
        *,
        config: WorkerConfig | None = None,
        retriever=None,  # Retriever 契约；None 时用 fakes 开发期默认
        llm_factory=None,  # ChatModelFactory；None 时用 fakes 开发期默认
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        self._store = store
        self._ledger = ledger
        self._runner = runner
        self._cfg = config or WorkerConfig()
        self._retriever = retriever
        self._llm_factory = llm_factory
        self._metrics = metrics

    @property
    def worker_id(self) -> str:
        return self._cfg.worker_id

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """循环认领执行；stop 置位后在当前 run 结束后的下一次轮询前退出。"""
        stop = stop if stop is not None else asyncio.Event()
        while not stop.is_set():
            worked = await self.run_one()
            if not worked:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self._cfg.poll_interval_s)
                except TimeoutError:
                    pass

    async def run_one(self) -> bool:
        """认领并执行一个 run；无可认领返回 False（调用方可据此休眠）。"""
        claim = await asyncio.to_thread(self._store.claim, self._cfg.worker_id, self._cfg.lease_s)
        if claim is None:
            return False
        if self._metrics is not None:
            self._metrics.claims += 1
        log_event(
            logger, logging.INFO, "run.claimed",
            run_id=claim.run_id, attempt=claim.attempt, worker_id=self._cfg.worker_id,
        )
        await self._execute(claim)
        return True

    # -- 内部 --

    async def _execute(self, claim: Claim) -> None:
        run_id, token = claim.run_id, claim.fencing_token
        lost = asyncio.Event()
        heartbeat = HeartbeatLoop(self._store, run_id, token, self._cfg.lease_s, lost=lost)
        hb_task = heartbeat.start()
        cancel = CancelMonitor(self._store, run_id, poll_s=self._cfg.cancel_poll_s)
        cancel_task = cancel.start()

        def cancelled() -> bool:
            # 心跳失联（被顶替/过期）或外部请求取消 → 编排层在节点边界停下
            return lost.is_set() or cancel.requested

        async def emit(event: NewEvent) -> None:
            run_event = await asyncio.to_thread(self._store.append_event, run_id, token, event)
            log_event(
                logger, logging.DEBUG, "run.event",
                run_id=run_event.run_id, attempt=run_event.attempt,
                seq=run_event.seq, node=run_event.node, event=run_event.type,
            )

        ctx = RunContext(
            emit=emit,
            cancelled=cancelled,
            budget=Budget(),
            ledger=self._ledger,
            retriever=self._retriever if self._retriever is not None else self._dev_retriever(),
            llm=self._llm_factory if self._llm_factory is not None else self._dev_llm_factory(),
        )
        status, result, error = "failed", None, None
        try:
            report = await self._runner.run(
                run_id, InspectionRequest.model_validate(claim.input), ctx
            )
            if report.status == "completed":
                status = "completed"
            elif report.status == "partial" and cancelled():
                status = "cancelled"  # 因取消/失联中断 → cancelled；自然降级才是 partial
            else:
                status = report.status
            # I7 集成补丁（ADR-0008）：终态 result 经 API/SSE 输出，JSON 形状统一 camelCase
            result = report.model_dump(by_alias=True)
        except Exception as exc:  # noqa: BLE001 - 运行时兜底：任何异常都落 failed 终态
            error = f"{type(exc).__name__}: {exc}"
        finally:
            cancel.stop()
            heartbeat.stop()
            await asyncio.gather(hb_task, cancel_task, return_exceptions=True)

        finished = await asyncio.to_thread(
            self._store.finish, run_id, token, status, result, error
        )
        if self._metrics is not None:
            if not finished or lost.is_set():
                # 租约关系已断：终态被拒（被顶替/终态）或心跳失联后勉强写入
                self._metrics.lease_lost += 1
            else:
                self._metrics.finished += 1
        if finished:
            log_event(
                logger, logging.INFO, "run.finished",
                run_id=run_id, attempt=claim.attempt, status=status,
            )
        else:
            # 被顶替或已终态：写入作废。任务仍会被接管者结束——"任务最终结束"不变量
            # 不依赖本 worker。
            log_event(
                logger, logging.WARNING, "run.finish_rejected",
                run_id=run_id, attempt=claim.attempt, status=status,
            )

    def _dev_retriever(self):
        from app.contracts.fakes import StaticRetriever

        return StaticRetriever()

    def _dev_llm_factory(self):
        from app.contracts.fakes import FakeLLM

        return FakeLLM.factory(["ok"])  # 开发期默认；集成期由 main.py 注入真实依赖


class WorkerPool:
    """N 个 RuntimeWorker 共享同一 store/ledger，池级 stop 事件统一收尾。"""

    def __init__(
        self,
        store: SqliteRunStore,
        ledger: ToolLedger,
        runner,
        count: int = 2,
        *,
        config: WorkerConfig | None = None,
        retriever=None,
        llm_factory=None,
        metrics: RuntimeMetrics | None = None,
    ) -> None:
        if count < 1:
            raise ValueError("WorkerPool 至少需要 1 个 worker")
        base = config or WorkerConfig()
        self._workers = tuple(
            RuntimeWorker(
                store,
                ledger,
                runner,
                config=replace(base, worker_id=f"{base.worker_id}-{i}"),
                retriever=retriever,
                llm_factory=llm_factory,
                metrics=metrics,
            )
            for i in range(count)
        )

    @property
    def worker_ids(self) -> tuple[str, ...]:
        return tuple(w.worker_id for w in self._workers)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        """阻塞直到 stop 置位，随后等待全部 worker 收尾。"""
        stop = stop if stop is not None else asyncio.Event()
        tasks = [
            asyncio.create_task(w.run_forever(stop), name=f"pool:{w.worker_id}")
            for w in self._workers
        ]
        await stop.wait()
        await asyncio.gather(*tasks)
