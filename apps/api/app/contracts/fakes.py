"""契约替身（计划 §5.3）：三个子智能体互不等待的开发基础。

- B 用 InMemoryRunStore + StaticRetriever 开发编排；
- C 用 ScriptedAgentRunner 开发评测框架；
- A 用 InMemoryRunStore 对照语义，再实现 SQLite 版。

InMemoryRunStore 是 RunStore 语义的参考实现：租约认领、fencing token 单调递增、
CAS 写入、终态吸收、seq 事务内分配。A 的 SQLite 实现行为必须与它一致。
fencing 精确语义：写入校验"token 是当前值且 status=running"——租约过期但未被
重新认领时 token 仍是当前值，合法完成写入不误拒；被重新认领后旧执行者一律被拒。
"""

import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from app.contracts.agent import Finding, InspectionReport, InspectionRequest
from app.contracts.events import NewEvent, RunEvent
from app.contracts.knowledge import Chunk, Retriever
from app.contracts.runtime import (
    Claim,
    LedgerEntry,
    LeaseLost,
    RunRecord,
    TerminalStatus,
    ToolLedger,
)

Clock = Callable[[], datetime]

_TERMINAL_EVENTS = {
    "completed": "run.completed",
    "failed": "run.failed",
    "cancelled": "run.cancelled",
    "partial": "run.partial",
}


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _lease_valid(run: RunRecord, now: datetime) -> bool:
    return run.lease_expires_at is None or run.lease_expires_at > now


class InMemoryRunStore:
    """线程安全的内存 RunStore。clock 注入使过期/续租可测（conftest 冻结时钟）。"""

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or _utcnow
        self._runs: dict[str, RunRecord] = {}
        self._events: dict[str, list[RunEvent]] = {}
        self._lock = threading.Lock()

    def create_run(self, kind: str, input: dict, idempotency_key: str | None = None) -> RunRecord:
        with self._lock:
            if idempotency_key:
                for run in self._runs.values():
                    if run.idempotency_key == idempotency_key:
                        return run  # 幂等：同一 key 返回同一 run
            now = self._clock()
            run = RunRecord(
                run_id=uuid.uuid4().hex,
                kind=kind,
                status="queued",
                input=input,
                idempotency_key=idempotency_key,
                created_at=now,
                updated_at=now,
            )
            self._runs[run.run_id] = run
            self._events[run.run_id] = []
            self._append_locked(run, NewEvent(type="run.queued"), attempt=0)
            return run

    def claim(self, worker_id: str, lease_s: int) -> Claim | None:
        with self._lock:
            now = self._clock()
            for run in self._runs.values():  # 创建序近似 FIFO
                if run.status == "queued" or (
                    run.status == "running" and not _lease_valid(run, now)
                ):
                    resumed = run.status == "running"
                    run.status = "running"
                    run.attempt += 1
                    run.fencing_token += 1
                    run.lease_owner = worker_id
                    run.lease_expires_at = now + timedelta(seconds=lease_s)
                    run.updated_at = now
                    self._append_locked(
                        run, NewEvent(type="run.resumed" if resumed else "run.started")
                    )
                    return Claim(
                        run_id=run.run_id,
                        fencing_token=run.fencing_token,
                        attempt=run.attempt,
                        kind=run.kind,
                        input=run.input,
                    )
            return None

    def heartbeat(self, run_id: str, token: int, lease_s: int) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            now = self._clock()
            if (
                run is None
                or run.status != "running"
                or run.fencing_token != token
                or not _lease_valid(run, now)
            ):
                return False
            run.lease_expires_at = now + timedelta(seconds=lease_s)
            run.updated_at = now
            return True

    def append_event(self, run_id: str, token: int, event: NewEvent) -> RunEvent:
        with self._lock:
            run = self._require(run_id)
            self._require_lease(run, token)
            return self._append_locked(run, event)

    def finish(
        self,
        run_id: str,
        token: int,
        status: TerminalStatus,
        result: dict | None = None,
        error: str | None = None,
    ) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            now = self._clock()
            if run is None or run.status != "running" or run.fencing_token != token:
                return False
            run.status = status
            run.result = result
            run.error = error
            run.lease_owner = None
            run.lease_expires_at = None
            run.updated_at = now
            self._append_locked(
                run, NewEvent(type=_TERMINAL_EVENTS[status], payload={"error": error})
            )
            return True

    def request_cancel(self, run_id: str) -> RunRecord:
        with self._lock:
            run = self._require(run_id)
            if run.status in ("queued", "running"):
                run.cancel_requested = True
                run.updated_at = self._clock()
            return run  # 终态幂等返回原记录

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            run = self._runs.get(run_id)
            return run is not None and run.cancel_requested

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            return self._runs.get(run_id)

    def events_after(self, run_id: str, after_seq: int) -> list[RunEvent]:
        with self._lock:
            return [e for e in self._events.get(run_id, []) if e.seq > after_seq]

    # -- 内部 --

    def _require(self, run_id: str) -> RunRecord:
        run = self._runs.get(run_id)
        if run is None:
            raise KeyError(run_id)
        return run

    def _require_lease(self, run: RunRecord, token: int) -> None:
        if run.status != "running" or run.fencing_token != token:
            raise LeaseLost(f"run {run.run_id} 不再接受 token={token} 的写入")

    def _append_locked(self, run: RunRecord, event: NewEvent, attempt: int | None = None) -> RunEvent:
        events = self._events[run.run_id]
        run_event = RunEvent(
            **event.model_dump(),
            run_id=run.run_id,
            seq=len(events) + 1,  # 严格递增无空洞；锁内分配，等价于事务内 MAX(seq)+1
            attempt=run.attempt if attempt is None else attempt,
            ts=self._clock(),
        )
        events.append(run_event)
        return run_event


class InMemoryLedger(ToolLedger):
    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or _utcnow
        self._entries: dict[str, LedgerEntry] = {}
        self._lock = threading.Lock()

    def begin(self, key: str, tool: str, args_hash: str) -> LedgerEntry:
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                return existing  # 幂等：调用方对 completed 直接复用结果
            entry = LedgerEntry(
                key=key, tool=tool, args_hash=args_hash, status="in_progress",
                updated_at=self._clock(),
            )
            self._entries[key] = entry
            return entry

    def complete(self, key: str, result: dict) -> None:
        with self._lock:
            self._entries[key].status = "completed"
            self._entries[key].result = result
            self._entries[key].updated_at = self._clock()

    def fail(self, key: str, error: str, retryable: bool) -> None:
        with self._lock:
            entry = self._entries[key]
            entry.status = "unknown" if retryable else "failed"
            entry.error = error
            entry.retryable = retryable
            entry.updated_at = self._clock()


class StaticRetriever(Retriever):
    """返回固定 Chunk 集：query 的空白分词任一命中标题或正文即保留，确定性打分。"""

    def __init__(self, chunks: list[Chunk] | None = None) -> None:
        self._chunks = chunks or [
            Chunk(
                chunk_id="demo-doc#巡检准备#1",
                doc_id="demo-doc",
                title="巡检准备",
                text="巡检前应核对设备清单，确认遥测采集窗口与通信状态正常。",
                score=1.0,
                source_path="fixtures/inspection/corpus/demo-doc.md",
            )
        ]

    async def search(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        terms = query.split()
        hits = [
            c
            for c in self._chunks
            if not terms or any(t in c.title or t in c.text for t in terms)
        ]
        return hits[:k]


class ScriptedAgentRunner:
    """按脚本回放报告：C 评测框架与 A 续跑开发的替身。

    确定性输出：每个设备一条 normal finding；可注入 delay / 异常；
    尊重 ctx.cancelled（取消时产出 partial）并发 node 事件，供端到端续跑演示。
    """

    def __init__(
        self,
        delay_s: float = 0.0,
        exc: Exception | None = None,
        report: InspectionReport | None = None,
    ) -> None:
        self.delay_s = delay_s
        self.exc = exc
        self.report = report

    async def run(self, run_id: str, input: InspectionRequest, ctx) -> InspectionReport:
        import asyncio

        await ctx.emit(NewEvent(type="node.started", node="inspector"))
        done = 0
        findings: list[Finding] = []
        for device_id in input.device_ids:
            if self.delay_s:
                await asyncio.sleep(self.delay_s)
            if ctx.cancelled():
                break
            findings.append(
                Finding(device_id=device_id, level="normal", summary="遥测平稳，未见异常")
            )
            done += 1
        if self.exc is not None:
            raise self.exc
        coverage = done / len(input.device_ids) if input.device_ids else 0.0
        status = "completed" if done == len(input.device_ids) else "partial"
        await ctx.emit(NewEvent(type="node.completed", node="inspector"))
        return self.report or InspectionReport(
            findings=findings, work_orders=[], status=status, coverage=coverage
        )


class FakeLLM:
    """按脚本回放 AIMessage（可含 tool_calls）的 ChatModelFactory。

    复用 langchain_core 的 GenericFakeChatModel（不自写模型循环）；
    单个模型实例内脚本耗尽即停，多轮需要时由调用方多次调 factory()。
    """

    @staticmethod
    def factory(responses: list) -> Callable[[], GenericFakeChatModel]:
        messages = [r if isinstance(r, AIMessage) else AIMessage(content=r) for r in responses]

        def build() -> GenericFakeChatModel:
            return GenericFakeChatModel(messages=iter(messages))

        return build
