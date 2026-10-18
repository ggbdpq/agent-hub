"""FastAPI 装配：异步任务 / 支付沙箱 / 场景路由。

智能体对话与证据接口已迁移到 apps/agent（TS，:8809）：POST /api/agent/chat、GET /api/evidence/:id
由 Vite 代理分流，本服务不再提供（ADR-0003）。
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app import orders, tasks
from app.schemas import (
    Order,
    PaymentChannel,
    PaymentNotify,
    RecommendScene,
    TaskSnapshot,
)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tasks.restore_snapshots()  # 读回快照、续单号，并重新调度 queued/running 任务续跑
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
