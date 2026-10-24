"""运行事件契约（ADR-0008）：跨模块唯一事件格式。

- ``NewEvent``：调用方提交的事件，run_id / seq / attempt / ts 由 RunStore 分配；
- ``RunEvent``：持久化后的完整事件，seq 在每个 run 内严格递增、无空洞，支撑 SSE Last-Event-ID 续传。
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "run.queued",
    "run.started",
    "run.resumed",
    "run.completed",
    "run.failed",
    "run.cancelled",
    "run.partial",
    "node.started",
    "node.completed",
    "node.failed",
    "tool.called",
    "tool.result",
    "tool.denied",
    "tool.deduplicated",
    "plan.created",
    "evidence.attached",
    "approval.required",
    "approval.resolved",
    "metric.usage",
]


class NewEvent(BaseModel):
    type: EventType
    node: str | None = None  # supervisor / planner / inspector / reporter
    payload: dict[str, Any] = Field(default_factory=dict)


class RunEvent(NewEvent):
    run_id: str
    seq: int
    attempt: int  # 第几次执行（续跑后递增）
    ts: datetime
