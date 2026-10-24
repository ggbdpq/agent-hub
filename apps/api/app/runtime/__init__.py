"""持久化运行时（子智能体 A）：租约 + fencing token + CAS + 事件日志 + 幂等账本。

对外只暴露契约接口的实现（app/contracts/runtime.py）；B 禁止 import 本包，
一律经 RunContext 使用运行时能力。A 自身禁止 import langgraph（ADR-0007 职责分离）。
"""

from app.runtime.store import Clock, SqliteRunStore, from_us, to_us

__all__ = ["Clock", "SqliteRunStore", "from_us", "to_us"]
