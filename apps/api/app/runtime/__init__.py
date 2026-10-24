"""持久化运行时（子智能体 A）：租约 + fencing token + CAS + 事件日志 + 幂等账本。

对外只暴露契约接口的实现（app/contracts/runtime.py）；B 禁止 import 本包，
一律经 RunContext 使用运行时能力。A 自身禁止 import langgraph（ADR-0007 职责分离）。
"""

from app.contracts.runtime import Claim, LeaseLost
from app.runtime.heartbeat import HeartbeatLoop
from app.runtime.reclaimer import Reclaimer
from app.runtime.state_machine import TERMINAL_STATUSES, VALID_TRANSITIONS, can_transition
from app.runtime.store import Clock, SqliteLedger, SqliteRunStore, from_us, to_us

__all__ = [
    "TERMINAL_STATUSES",
    "VALID_TRANSITIONS",
    "Claim",
    "Clock",
    "HeartbeatLoop",
    "LeaseLost",
    "Reclaimer",
    "SqliteLedger",
    "SqliteRunStore",
    "can_transition",
    "from_us",
    "to_us",
]
