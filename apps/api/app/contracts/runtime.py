"""持久化运行时契约（ADR-0006）：RunStore 与 ToolLedger。

语义要点（fakes.InMemoryRunStore 是参考实现，子智能体 A 的 SQLite 版必须行为一致）：
- 租约：claim 单调递增 fencing_token 与 attempt；心跳续租；过期后心跳失败。
- fencing：写入校验 token 是当前值且 run 处于 running——被重新认领后，旧执行者的
  写入一律抛 LeaseLost / 返回 False；租约过期但未被接管时 token 仍是当前值，合法写入不误拒。
- 终态吸收：completed/failed/cancelled/partial 之后任何迁移与写入都被拒绝。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

# JSON 形状对齐仓库惯例（camelCase）；populate_by_name 保持内部名（I6 集成补丁）
_CAMEL = ConfigDict(alias_generator=to_camel, populate_by_name=True)

from app.contracts.events import NewEvent, RunEvent

RunStatus = Literal["queued", "running", "completed", "failed", "cancelled", "partial"]
TerminalStatus = Literal["completed", "failed", "cancelled", "partial"]
LedgerStatus = Literal["in_progress", "completed", "failed", "unknown"]


class LeaseLost(Exception):
    """fencing token 已被顶替（租约被新执行者接管）或 run 已终态，本次写入被拒。"""


class RunRecord(BaseModel):
    model_config = _CAMEL

    run_id: str
    kind: str
    status: RunStatus
    input: dict
    result: dict | None = None
    error: str | None = None
    attempt: int = 0
    fencing_token: int = 0
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    cancel_requested: bool = False
    idempotency_key: str | None = None
    created_at: datetime
    updated_at: datetime


class Claim(BaseModel):
    model_config = _CAMEL

    """认领成功凭据：worker 在租约内持 fencing_token 写入事件与终态。"""

    run_id: str
    fencing_token: int
    attempt: int
    kind: str
    input: dict


class RunStore(Protocol):
    def create_run(self, kind: str, input: dict, idempotency_key: str | None = None) -> RunRecord: ...

    def claim(self, worker_id: str, lease_s: int) -> Claim | None:
        """认领一个 queued（或租约过期的 running）run；无可认领返回 None。"""

    def heartbeat(self, run_id: str, token: int, lease_s: int) -> bool:
        """续租；False = 已失去租约（被顶替 / 过期 / 终态），调用方应立即停止。"""

    def append_event(self, run_id: str, token: int, event: NewEvent) -> RunEvent: ...

    def finish(
        self,
        run_id: str,
        token: int,
        status: TerminalStatus,
        result: dict | None = None,
        error: str | None = None,
    ) -> bool:
        """CAS 写终态；False = 写入被拒（token 失效或已被终态）。"""

    def request_cancel(self, run_id: str) -> RunRecord: ...

    def is_cancel_requested(self, run_id: str) -> bool: ...

    def get(self, run_id: str) -> RunRecord | None: ...

    def events_after(self, run_id: str, after_seq: int) -> list[RunEvent]: ...


class LedgerEntry(BaseModel):
    model_config = _CAMEL

    key: str  # run_id:node:step:tool_call_id，由编排层生成且续跑可复现
    tool: str
    args_hash: str
    status: LedgerStatus
    result: dict | None = None
    error: str | None = None
    retryable: bool | None = None
    updated_at: datetime


class ToolLedger(Protocol):
    """副作用工具幂等账本：同一键 begin 返回原记录，写工具不重复执行（不变量）。"""

    def begin(self, key: str, tool: str, args_hash: str) -> LedgerEntry: ...

    def complete(self, key: str, result: dict) -> None: ...

    def fail(self, key: str, error: str, retryable: bool) -> None:
        """retryable=True → status=unknown（可能已执行，禁止自动重试）；False → failed。"""
