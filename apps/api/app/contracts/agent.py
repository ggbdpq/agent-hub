"""智能体契约：AgentRunner 与 RunContext（评测、Worker、编排共同依赖的唯一入口）。

RunContext 用 dataclass 而非 pydantic：它承载 emit/cancelled 等 callable，
校验无价值（对计划 §5.2 的偏差，已记入 ADR-0008）。
时间统一 aware UTC。
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, Field

from app.contracts.events import NewEvent
from app.contracts.knowledge import Retriever
from app.contracts.runtime import ToolLedger

# 返回 langchain BaseChatModel（真实 OpenAI 兼容模型或 FakeLLM）；B 在 agents/models.py 定型实现
ChatModelFactory = Callable[[], BaseChatModel]


class Budget(BaseModel):
    """运行时强制的执行预算（B2 扣减，超限拒绝，不靠提示词自觉）。"""

    max_steps: int = 24
    max_tool_calls: int = 40
    max_tokens: int = 100_000
    deadline: datetime | None = None


class InspectionRequest(BaseModel):
    site: str
    device_ids: list[str] = Field(min_length=1)
    window: str | None = None  # 巡检时间窗（演示用自由文本）
    goal: str | None = None


class Finding(BaseModel):
    device_id: str
    level: Literal["normal", "warning", "critical"]
    summary: str
    evidence_chunk_ids: list[str] = Field(default_factory=list)  # 必须引用真实存在的 chunk（B4 校验）
    metrics: dict[str, Any] = Field(default_factory=dict)


class WorkOrder(BaseModel):
    device_id: str
    title: str
    action: str
    status: Literal["draft", "approved", "rejected", "executed"] = "draft"
    approval_id: str | None = None


class InspectionReport(BaseModel):
    findings: list[Finding]
    work_orders: list[WorkOrder] = Field(default_factory=list)
    status: Literal["completed", "partial", "failed"]
    coverage: float  # 0-1：已完成子任务 / 计划子任务（B5 降级时可解释）
    notes: str | None = None


@dataclass
class RunContext:
    """运行时注入给编排层的能力。B 只经它使用运行时，禁止 import app.runtime。"""

    emit: Callable[[NewEvent], Awaitable[None]]
    cancelled: Callable[[], bool]
    budget: Budget
    ledger: ToolLedger
    retriever: Retriever
    llm: ChatModelFactory


class AgentRunner(Protocol):
    async def run(self, run_id: str, input: InspectionRequest, ctx: RunContext) -> InspectionReport: ...
