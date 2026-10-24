"""契约镜像：与 packages/shared/src/contracts.ts（zod）逐字段对齐。

智能体事件与证据已由 apps/agent（TS）直接用 zod 契约校验，本文件只保留
Python 仍在使用的任务、订单与场景部分（ADR-0003 / WP9）。
两端以 fixtures/contract-example.json 同源对账（tests/test_contracts.py）；
fixture 中事件与 chatRequest 段现在只由 TS 侧校验（只追加不删除原则）。
"""
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class RecommendScene(StrEnum):
    DAILY = "daily"
    GIFT = "gift"
    COMFORT = "comfort"


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


# -- 巡检多智能体契约（I6） --
# Python 侧单一事实源在 app/contracts/**（pydantic）；此处 re-export 供契约对账
# （tests/test_contracts.py 与 packages/shared/src/contracts.test.ts 同源解析
# fixtures/contract-example.json 的 runEvent*/inspectionReport 样例）。
# JSON 字段为 camelCase（alias_generator=to_camel，I6 集成补丁，ADR-0008）。
from app.contracts.agent import (  # noqa: E402
    Finding as InspectionFinding,
    InspectionReport as InspectionReportModel,
    InspectionRequest as InspectionRequestModel,
    WorkOrder as InspectionWorkOrder,
)
from app.contracts.events import RunEvent as RunEventModel  # noqa: E402
from app.contracts.runtime import RunRecord as RunRecordModel  # noqa: E402
