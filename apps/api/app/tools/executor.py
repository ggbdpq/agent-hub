"""B2：工具执行器——所有运行时约束的强制点（ADR-0009：约束在代码，不在提示词）。

统一入口 ``ToolExecutor.call``，顺序：
权限（registry）→ 白名单（Skill）→ 预算 → 取消 → 参数校验 → dangerous 审批凭证
→ write/dangerous 走幂等账本 → 发 tool.called → asyncio.wait_for 超时
→ 结果 Schema 校验 → 账本落账 → 发 tool.result。

denial 一律发 ``tool.denied`` 事件返回可解释结果，不以异常崩溃；
超时/异常按 ``ToolSpec.retry`` 给出 retryable 标记，副作用工具一律
fail(retryable=True)（副作用可能已发生 → 账本 unknown，禁止自动重试，§9 不变量）。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.contracts.agent import RunContext
from app.contracts.events import NewEvent
from app.contracts.runtime import LedgerEntry
from app.tools.budget import BudgetTracker, args_hash
from app.tools.registry import ToolRegistry, check_call

OutcomeStatus = Literal["ok", "denied", "error", "deduplicated"]


@dataclass(frozen=True)
class ToolOutcome:
    tool: str
    status: OutcomeStatus
    result: BaseModel | None = None
    error: str | None = None
    reason: str | None = None  # denied / error 原因
    retryable: bool | None = None  # error 时给出：safe→True；never/ledger→False
    elapsed_s: float = 0.0


_SIDE_EFFECT_EFFECTS = ("write", "dangerous")


def _event(node: str | None, event_type: str, payload: dict) -> NewEvent:
    return NewEvent(type=event_type, node=node, payload=payload)


class ToolExecutor:
    """绑定（registry, ctx, node, tracker）的工具调用入口；allowed 为 Skill 白名单。"""

    def __init__(
        self,
        registry: ToolRegistry,
        ctx: RunContext,
        *,
        node: str,
        tracker: BudgetTracker,
        allowed: set[str] | None = None,
    ) -> None:
        self._registry = registry
        self._ctx = ctx
        self.node = node
        self._tracker = tracker
        self._allowed = allowed
        self._begun: set[str] = set()  # 本实例已 begin 的账本键（fresh/既有判定）

    async def call(self, name: str, args: dict, *, ledger_key: str | None = None) -> ToolOutcome:
        started = time.monotonic()

        def out(status: OutcomeStatus, **kw) -> ToolOutcome:
            return ToolOutcome(tool=name, status=status, elapsed_s=time.monotonic() - started, **kw)

        # 1) Skill 白名单（B10）：声明之外的工具不可用
        if self._allowed is not None and name not in self._allowed:
            await self._deny(name, "not_in_allowlist", f"{name} 不在当前 Skill 白名单内")
            return out("denied", reason="not_in_allowlist")

        # 2) 注册表权限（未注册/越权 → 已发 tool.denied）
        check = await check_call(self._registry, self._ctx, node=self.node, name=name)
        if check.denied:
            return out("denied", reason=check.reason, error=check.detail)

        # 3) 预算：工具次数与截止时间（超限拒绝，不靠提示词）
        allowed, reason = self._tracker.allow_tool_call()
        if not allowed:
            await self._deny(name, reason, f"预算拒绝: {reason}")
            return out("denied", reason=reason)

        # 4) 取消检查
        if self._ctx.cancelled():
            await self._deny(name, "cancelled", "run 已请求取消")
            return out("denied", reason="cancelled")

        # 5) 参数 Schema 校验
        spec = check.spec
        try:
            validated = spec.input_schema.model_validate(args)
        except ValidationError as exc:
            await self._deny(name, "invalid_args", f"参数校验失败: {exc.errors()[0]['msg']}")
            return out("denied", reason="invalid_args", error=str(exc))

        # 6) dangerous 必须携带人工审批凭证（B7 interrupt 产物）
        if spec.effect == "dangerous" and not getattr(validated, "approval_id", ""):
            await self._deny(name, "approval_required", "dangerous 工具缺少 approval_id")
            return out("denied", reason="approval_required")

        # 7) 副作用工具走幂等账本（写工具不重复执行，§9）
        # fresh 判定：本实例未曾 begin 过该键。begin 对新键也返回 in_progress，
        # 只有"本实例已 begin 且未收尾"（并发/上次未落账）或既有 unknown 才拒绝。
        entry: LedgerEntry | None = None
        if spec.effect in _SIDE_EFFECT_EFFECTS:
            if not ledger_key:
                return out(
                    "error",
                    reason="ledger_key_required",
                    error="副作用工具必须提供可复现 ledger_key（由编排层按 run_id:node:step 生成）",
                )
            seen = ledger_key in self._begun
            self._begun.add(ledger_key)
            entry = self._ctx.ledger.begin(ledger_key, name, args_hash(validated))
            if entry.status == "completed":
                result = spec.output_schema.model_validate(entry.result)
                await self._ctx.emit(
                    _event(self.node, "tool.deduplicated", {"tool": name, "ledger_key": ledger_key})
                )
                return out("deduplicated", result=result)
            crashed_or_pending = entry.status == "unknown" or (entry.status == "in_progress" and seen)
            if crashed_or_pending:
                await self._deny(
                    name,
                    "ledger_state_unknown",
                    f"账本状态 {entry.status}——状态未知禁止自动重试（§9 不变量）",
                )
                return out("denied", reason="ledger_state_unknown")
            # failed（明确失败可重试）与 fresh in_progress（本次新建）继续执行

        # 8) 发 tool.called 后带超时执行
        await self._ctx.emit(
            _event(
                self.node,
                "tool.called",
                {"tool": name, "effect": spec.effect, "args": validated.model_dump()},
            )
        )
        retryable = spec.retry == "safe"
        try:
            result = await asyncio.wait_for(spec.handler(validated), timeout=spec.timeout_s)
        except TimeoutError:
            self._fail_entry(entry, "timeout", retryable=True)
            return await self._error(
                out, name, f"timeout after {spec.timeout_s:.1f}s", spec, retryable, started
            )
        except Exception as exc:  # handler 自身异常：按策略标记
            self._fail_entry(entry, str(exc), retryable=True)
            return await self._error(out, name, f"handler error: {exc}", spec, retryable, started)

        # 9) 结果 Schema 校验（畸形结果视为失败）
        if not isinstance(result, spec.output_schema):
            self._fail_entry(entry, "output schema mismatch", retryable=True)
            return await self._error(
                out,
                name,
                f"结果类型 {type(result).__name__} 不符合 {spec.output_schema.__name__}",
                spec,
                retryable,
                started,
            )

        # 10) 落账与 tool.result
        if entry is not None:
            self._ctx.ledger.complete(ledger_key, result.model_dump())
        await self._ctx.emit(
            _event(
                self.node,
                "tool.result",
                {"tool": name, "ok": True, "elapsed_ms": int((time.monotonic() - started) * 1000)},
            )
        )
        return out("ok", result=result)

    # -- 内部 --

    async def _deny(self, name: str, reason: str, detail: str) -> None:
        await self._ctx.emit(
            _event(self.node, "tool.denied", {"tool": name, "reason": reason, "detail": detail})
        )

    async def _error(self, out, name: str, error: str, spec, retryable: bool, started: float) -> ToolOutcome:
        await self._ctx.emit(
            _event(
                self.node,
                "tool.result",
                {"tool": name, "ok": False, "error": error, "retryable": retryable},
            )
        )
        return out("error", error=error, retryable=retryable)

    def _fail_entry(self, entry: LedgerEntry | None, error: str, *, retryable: bool) -> None:
        # retryable=True → 账本 unknown（副作用可能已发生）；False → failed
        if entry is not None:
            self._ctx.ledger.fail(entry.key, error, retryable=retryable)
