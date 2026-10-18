"""契约镜像：与 packages/shared/src/contracts.ts（zod）逐字段对齐。

两端以 fixtures/contract-example.json 同源对账（tests/test_contracts.py）。
修改契约时必须三处同步：本文件、contracts.ts、fixture。
"""
from enum import StrEnum
from typing import Literal, Union

from pydantic import BaseModel, Field


class EvidenceSourceKind(StrEnum):
    PURCHASE_HISTORY = "purchase_history"
    PREFERENCE_SURVEY = "preference_survey"
    CARE_RECORD = "care_record"


class Evidence(BaseModel):
    id: str
    source: str
    sourceKind: EvidenceSourceKind
    credibility: int = Field(ge=0, le=100)
    quote: str
    analyzedAt: str


class EvidenceRef(BaseModel):
    evidenceId: str
    label: str


class RecommendScene(StrEnum):
    DAILY = "daily"
    GIFT = "gift"
    COMFORT = "comfort"


class FlowerItem(BaseModel):
    id: str
    name: str
    priceFen: int = Field(ge=0)
    typeLabel: str
    emoji: str


class FlowerCardPayload(BaseModel):
    scene: RecommendScene
    title: str
    reason: str
    items: list[FlowerItem] = Field(min_length=1)
    evidenceRefs: list[EvidenceRef]


class ThoughtEvent(BaseModel):
    type: Literal["thought"]
    seq: int = Field(ge=1)
    text: str
    final: bool


class TokenEvent(BaseModel):
    type: Literal["token"]
    text: str


class CardEvent(BaseModel):
    type: Literal["card"]
    payload: FlowerCardPayload


class EvidenceEvent(BaseModel):
    type: Literal["evidence"]
    evidence: Evidence


class DoneEvent(BaseModel):
    type: Literal["done"]
    replyId: str


class ErrorEvent(BaseModel):
    type: Literal["error"]
    code: Literal["protocol", "server", "aborted"]
    message: str


AgentStreamEvent = Union[
    ThoughtEvent, TokenEvent, CardEvent, EvidenceEvent, DoneEvent, ErrorEvent
]


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    ARCHIVED = "archived"


class TaskSection(BaseModel):
    heading: str
    body: str


class TaskResult(BaseModel):
    title: str
    sections: list[TaskSection]


class TaskSnapshot(BaseModel):
    taskId: str
    taskNo: str
    kind: Literal["care_plan"]
    status: TaskStatus
    progress: int = Field(ge=0, le=100)
    result: TaskResult | None = None
    failReason: str | None = None


class PaymentChannel(StrEnum):
    WECHAT = "wechat"
    ALIPAY = "alipay"


class OrderStatus(StrEnum):
    CREATED = "created"
    PAYING = "paying"
    PAID = "paid"
    CLOSED = "closed"


class Order(BaseModel):
    orderId: str
    orderNo: str
    flowerName: str
    amountFen: int = Field(gt=0)
    channel: PaymentChannel
    status: OrderStatus
    paidAt: str | None = None


class PaymentNotify(BaseModel):
    orderId: str
    channel: PaymentChannel
    sign: str


class ChatRequest(BaseModel):
    message: str
    # 迁移期 TS 智能体（apps/agent）的会话标识：py 后端接收但忽略（WP9 随事件镜像一起删除）
    threadId: str | None = None
