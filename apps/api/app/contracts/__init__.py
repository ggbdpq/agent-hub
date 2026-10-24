"""巡检多智能体契约包（第 0 阶段冻结，contracts-v1）。

五个接口 + RunEvent + 替身。变更规则：任何契约变更需升 contracts-v2，
并在 feat/runtime、feat/agents、feat/knowledge-eval 三个分支同步 rebase。
"""

from app.contracts.agent import (
    AgentRunner,
    Budget,
    ChatModelFactory,
    Finding,
    InspectionReport,
    InspectionRequest,
    RunContext,
    WorkOrder,
)
from app.contracts.events import EventType, NewEvent, RunEvent
from app.contracts.knowledge import Chunk, Retriever
from app.contracts.runtime import (
    Claim,
    LedgerEntry,
    LedgerStatus,
    LeaseLost,
    RunRecord,
    RunStatus,
    RunStore,
    TerminalStatus,
    ToolLedger,
)
from app.contracts.tools import ToolEffect, ToolRetry, ToolSpec

__all__ = [
    "AgentRunner",
    "Budget",
    "ChatModelFactory",
    "Chunk",
    "Claim",
    "EventType",
    "Finding",
    "InspectionReport",
    "InspectionRequest",
    "LedgerEntry",
    "LedgerStatus",
    "LeaseLost",
    "NewEvent",
    "Retriever",
    "RunContext",
    "RunEvent",
    "RunRecord",
    "RunStatus",
    "RunStore",
    "TerminalStatus",
    "ToolEffect",
    "ToolLedger",
    "ToolRetry",
    "ToolSpec",
    "WorkOrder",
]
