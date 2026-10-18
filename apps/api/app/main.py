"""FastAPI 装配：Agent SSE / 异步任务 / 支付沙箱三组路由。"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app import agent, llm, orders, tasks
from app.evidence import get_evidence
from app.schemas import (
    ChatRequest,
    Order,
    PaymentChannel,
    PaymentNotify,
    RecommendScene,
    TaskSnapshot,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks.restore_snapshots()  # 只读回快照与续号，不重新调度未完成任务
    yield


app = FastAPI(title="agent-hub 花语智能体 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8888", "http://127.0.0.1:8888"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class OrderRequest(BaseModel):
    flowerName: str
    amountFen: int
    channel: PaymentChannel


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "service": "agent-hub-api"}


@app.post("/api/agent/chat")
async def agent_chat(body: ChatRequest) -> StreamingResponse:
    """花语智能体对话：SSE 事件流（thought/token/card/evidence/done）。

    配置了 OPENAI_API_KEY 时走真实大模型（OpenAI 兼容协议：deepseek/kimi/glm/openai），
    否则回退确定性剧本——前端与事件协议完全一致。
    """
    provider = llm.resolve_provider()
    stream = llm.llm_stream(body.message, provider) if provider else agent.script_stream(body.message)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/evidence/{evidence_id}")
def evidence_detail(evidence_id: str):
    """依据抽屉数据源：按 id 取依据（结论与依据分离）。"""
    evidence = get_evidence(evidence_id)
    if evidence is None:
        raise HTTPException(status_code=404, detail="evidence not found")
    return evidence


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
