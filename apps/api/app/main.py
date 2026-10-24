"""FastAPI 装配：异步任务 / 支付沙箱 / 场景路由 + 巡检多智能体运行时（I5）。

- 花语对话与证据接口在 apps/agent（TS，:8809），本服务只保留任务/订单/支付（ADR-0003）。
- 巡检多智能体走新路由 /api/runs（绞杀者模式）：POST 创建（支持 Idempotency-Key 头）、
  GET 详情、GET events（SSE，支持 Last-Event-ID 续传）、POST cancel、POST approvals。
- lifespan 中装配 SQLite RunStore + 工具账本 + WorkerPool + Reclaimer + AsyncSqliteSaver
  检查点 + 审批中心（approver 经 Future 等待 HTTP 决定，超时默认拒绝）。
"""

import asyncio
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel

from app import orders, tasks
from app.agents.models import env_chat_model_factory
from app.agents.runner import InspectionAgentRunner
from app.contracts.agent import InspectionRequest
from app.knowledge.embedding import HashingEmbedder
from app.knowledge.retriever import HybridRetriever
from app.knowledge.splitting import split_corpus
from app.runtime.reclaimer import Reclaimer
from app.runtime.store import SqliteLedger, SqliteRunStore
from app.runtime.worker import WorkerPool
from app.schemas import (
    Order,
    PaymentChannel,
    PaymentNotify,
    RecommendScene,
    TaskSnapshot,
)
from app.tools.devices import DEFAULT_FIXTURE_ROOT

# SSE 终态事件集合（收到即完成流）
_TERMINAL_EVENTS: set[str] = {"run.completed", "run.failed", "run.cancelled", "run.partial"}
_APPROVAL_TIMEOUT_S = 600.0


# SSE 终态判定用（与 RunStatus 终态字符串一致）
_TERMINAL_STATUSES: set[str] = {"completed", "failed", "cancelled", "partial"}


class ApprovalHub:
    """进程内审批中心：runner 的 approver 回调 await Future，HTTP 端点 resolve。

    - 审批结果只被消费一次：resolve 后即摘除，重复 POST 返回 409；
    - 决定先行：POST 早于编排到达审批点时缓冲决定，approver 领取后立即返回
      （消除"事件已发、Future 未注册"窗口内的竞态拒绝）。
    """

    _DECIDED_TTL_S = 600.0  # 决定先行缓冲的存活期（审查 R4：防永不被领取的泄漏）

    def __init__(self, timeout_s: float = _APPROVAL_TIMEOUT_S) -> None:
        self._pending: dict[str, asyncio.Future[dict]] = {}
        self._decided: dict[str, tuple[dict, float]] = {}  # decision + 存入时刻（monotonic）
        self._consumed: set[str] = set()  # 已送达 runner 的决定（重复 POST 一律 409）
        self._timeout_s = timeout_s

    def _expire_decided(self) -> None:
        import time as _time

        now = _time.monotonic()
        stale = [k for k, (_, ts) in self._decided.items() if now - ts > self._DECIDED_TTL_S]
        for k in stale:
            self._decided.pop(k, None)

    async def approver(self, payload: dict) -> dict:
        approval_id = str(payload.get("approvalId") or payload.get("approval_id") or "")
        if not approval_id:
            return {"approved": False, "approval_id": "", "note": "payload 缺 approval_id，默认拒绝"}
        self._expire_decided()
        if approval_id in self._decided:
            self._consumed.add(approval_id)
            decision, _ = self._decided.pop(approval_id)  # 决定已先行到达
            return decision
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[approval_id] = fut
        try:
            decision = await asyncio.wait_for(fut, timeout=self._timeout_s)
            self._consumed.add(approval_id)
            return decision
        except TimeoutError:
            self._consumed.add(approval_id)  # 审批窗口已关闭，迟到决定应被拒
            return {
                "approved": False,
                "approval_id": approval_id,
                "note": f"审批等待超时（{self._timeout_s:.0f}s），安全缺省拒绝",
            }
        finally:
            self._pending.pop(approval_id, None)

    def resolve(self, approval_id: str, decision: dict) -> bool:
        if approval_id in self._consumed or approval_id in self._decided:
            return False  # 已送达或已缓冲待领：结果只消费一次
        fut = self._pending.get(approval_id)
        if fut is not None:
            if fut.done():
                return False
            fut.set_result(decision)
            self._consumed.add(approval_id)
            return True
        import time as _time

        self._decided[approval_id] = (decision, _time.monotonic())  # 决定先行：缓冲待领
        return True

    def shutdown(self) -> None:
        """收尾：未决审批一律按拒绝解除，让等待中的 runner 尽快落终态。"""
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"approved": False, "approvalId": "", "note": "服务关闭，默认拒绝"})
        self._pending.clear()
        self._decided.clear()
        self._consumed.clear()


def _default_retriever() -> HybridRetriever:
    return HybridRetriever(
        split_corpus(DEFAULT_FIXTURE_ROOT / "corpus"), embedder=HashingEmbedder(dim=512)
    )


def _offline_llm_requested() -> bool:
    return os.environ.get("AGENT_HUB_LLM", "").strip().lower() in ("scripted", "fake", "offline")


def scripted_llm_factory(evidence_chunk_id: str):
    """离线确定性脚本工厂（I8）：复用 demo 的脚本与逐条推进实现（同包内协作）。"""
    from app.agents.demo import _scripts, _sequence_factory

    return _sequence_factory(_scripts(evidence_chunk_id))


def create_app(
    *,
    runs_db_path: str | None = None,
    checkpoints_path: str | None = None,
    llm_factory: Any = None,
    worker_count: int = 2,
    retriever: HybridRetriever | None = None,
    approval_timeout_s: float = _APPROVAL_TIMEOUT_S,
) -> FastAPI:
    """装配应用。测试注入 runs_db_path / llm_factory / worker_count=0。"""

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        tasks.restore_snapshots()  # 读回快照、续单号，并重新调度 queued/running 任务续跑

        db = runs_db_path or os.environ.get(
            "AGENT_HUB_RUNS_DB", str(Path(tempfile.gettempdir()) / "agent-hub-runs.sqlite")
        )
        ckpt = checkpoints_path or os.environ.get(
            "AGENT_HUB_CHECKPOINTS", str(Path(tempfile.gettempdir()) / "agent-hub-checkpoints.sqlite")
        )
        store = SqliteRunStore(db)
        ledger = SqliteLedger(db)
        hub = ApprovalHub(timeout_s=approval_timeout_s)
        state = SimpleNamespace(store=store, ledger=ledger, hub=hub)
        stop = asyncio.Event()
        pool_task: asyncio.Task | None = None
        reclaimer_task: asyncio.Task | None = None

        async with AsyncSqliteSaver.from_conn_string(ckpt) as saver:
            runner = InspectionAgentRunner(checkpointer=saver, approver=hub.approver)
            if worker_count > 0:
                if llm_factory is not None:
                    factory = llm_factory
                elif _offline_llm_requested():
                    # AGENT_HUB_LLM=scripted：离线确定性档（E2E/演示），与花语离线剧本同哲学
                    hits = await (retriever or _default_retriever()).search("柜温 告警阈值", k=1)
                    chunk_id = hits[0].chunk_id if hits else ""
                    factory = scripted_llm_factory(chunk_id)
                else:
                    factory = env_chat_model_factory()
                pool = WorkerPool(
                    store,
                    ledger,
                    runner,
                    count=worker_count,
                    retriever=retriever or _default_retriever(),
                    llm_factory=factory,
                )
                reclaimer = Reclaimer(store, interval_s=10.0)
                pool_task = asyncio.create_task(pool.run_forever(stop), name="runs-worker-pool")
                reclaimer_task = asyncio.create_task(
                    reclaimer.run_forever(stop), name="runs-reclaimer"
                )
            _.state.runs = state
            try:
                yield
            finally:
                hub.shutdown()  # 先解除未决审批，再停池收尾
                stop.set()
                pending = [t for t in (pool_task, reclaimer_task) if t is not None]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title="agent-hub 花语智能体 API", lifespan=lifespan)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:8888", "http://127.0.0.1:8888"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # -- 花语（任务 / 订单 / 支付 / 场景） --

    class OrderRequest(BaseModel):
        flowerName: str
        amountFen: int
        channel: PaymentChannel

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "service": "agent-hub-api"}

    @app.post("/api/tasks", response_model=TaskSnapshot)
    async def create_task() -> TaskSnapshot:
        """创建养护方案生成任务（异步长任务演示）。"""
        return await tasks.create_task()

    @app.get("/api/tasks/{task_id}", response_model=TaskSnapshot)
    def get_task(task_id: str) -> TaskSnapshot:
        task = tasks.get_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.post("/api/tasks/{task_id}/cancel", response_model=TaskSnapshot)
    def cancel_task(task_id: str) -> TaskSnapshot:
        task = tasks.cancel_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.post("/api/tasks/{task_id}/confirm", response_model=TaskSnapshot)
    def confirm_task(task_id: str) -> TaskSnapshot:
        """前端确认保存后归档。"""
        task = tasks.confirm_task(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="task not found")
        return task

    @app.post("/api/orders", response_model=Order)
    def create_order(body: OrderRequest) -> Order:
        """下单：从推荐卡片 CTA 进入。金额以分为单位。"""
        return orders.create_order(body.flowerName, body.amountFen, body.channel)

    @app.post("/api/orders/{order_id}/cashier", response_model=Order)
    def open_cashier(order_id: str) -> Order:
        """进入模拟收银台：created → paying。真实场景此处生成微信/支付宝收银参数。"""
        order = orders.mark_paying(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    @app.post("/api/orders/{order_id}/notify")
    def payment_notify(order_id: str, body: PaymentNotify) -> dict:
        """模拟微信/支付宝异步回调：验签 + 幂等 + 状态机。"""
        if body.orderId != order_id:
            raise HTTPException(status_code=400, detail="orderId mismatch")
        result = orders.handle_payment_notify(order_id, body.channel, body.sign)
        if not result["ok"]:
            raise HTTPException(status_code=409, detail=result["reason"])
        order = result["order"]
        return {"duplicate": result["duplicate"], "orderId": order.orderId, "status": order.status.value}

    @app.post("/api/orders/{order_id}/close", response_model=Order)
    def close_order(order_id: str) -> Order:
        """超时未支付关单（状态机闭环演示）。"""
        order = orders.close_order(order_id)
        if order is None:
            raise HTTPException(status_code=404, detail="order not found")
        return order

    @app.get("/api/orders", response_model=list[Order])
    def list_orders() -> list[Order]:
        return orders.list_orders()

    @app.get("/api/scenes", response_model=list[RecommendScene])
    def list_scenes() -> list[RecommendScene]:
        return list(RecommendScene)

    # -- 巡检多智能体（/api/runs，I5） --

    @app.post("/api/runs", status_code=201)
    def create_run(body: InspectionRequest, request: Request) -> dict:
        """创建巡检 run；Idempotency-Key 头相同则由 RunStore 返回同一 run（幂等重放）。"""
        state = request.app.state.runs
        idem = request.headers.get("Idempotency-Key")
        record = state.store.create_run("inspection", body.model_dump(by_alias=True), idempotency_key=idem)
        return record.model_dump(mode="json", by_alias=True)

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request) -> dict:
        state = request.app.state.runs
        record = state.store.get(run_id)
        if record is None:
            raise HTTPException(status_code=404, detail="run not found")
        return record.model_dump(mode="json", by_alias=True)

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request) -> StreamingResponse:
        """SSE 事件流（CLAUDE.md 铁律 4：单行完整 JSON）；Last-Event-ID 头断点续传。"""
        state = request.app.state.runs
        if state.store.get(run_id) is None:
            raise HTTPException(status_code=404, detail="run not found")
        try:
            after_seq = int(request.headers.get("Last-Event-ID", "0") or 0)
        except (ValueError, OverflowError):
            after_seq = 0
        # SQLite 整数绑定上限 2^63-1（审查 R3：畸形头防 OverflowError）
        after_seq = max(0, min(after_seq, 2**63 - 1))

        async def gen():
            seq = after_seq
            while True:
                if await request.is_disconnected():
                    return
                # 审查 R3：store 是同步 SQLite（RLock 等锁可达 busy_timeout），
                # 在 async gen 内必须经 to_thread，避免阻塞事件循环（心跳/其他 SSE）
                events = await asyncio.to_thread(state.store.events_after, run_id, seq)
                for event in events:
                    yield f"id: {event.seq}\nevent: {event.type}\ndata: {event.model_dump_json(by_alias=True)}\n\n"
                    seq = event.seq
                record = await asyncio.to_thread(state.store.get, run_id)
                if record is None or record.status in _TERMINAL_STATUSES:
                    return
                await asyncio.sleep(0.2)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str, request: Request) -> dict:
        """协作式取消：只置标记，终态由持有租约的 worker 写入（ADR-0006）。"""
        state = request.app.state.runs
        try:
            record = state.store.request_cancel(run_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="run not found") from None
        return record.model_dump(mode="json", by_alias=True)

    class ApprovalDecision(BaseModel):
        approved: bool
        note: str | None = None

    @app.post("/api/runs/{run_id}/approvals/{approval_id}")
    async def resolve_approval(run_id: str, approval_id: str, body: ApprovalDecision, request: Request) -> dict:
        state = request.app.state.runs
        decision = {"approved": body.approved, "approvalId": approval_id, "note": body.note}
        if not state.hub.resolve(approval_id, decision):
            raise HTTPException(status_code=409, detail="审批单不存在或已消费（结果只消费一次）")
        return {"ok": True, "approval_id": approval_id, "approved": body.approved}

    return app


app = create_app()
