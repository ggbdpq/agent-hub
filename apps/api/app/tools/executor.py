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
import json
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

# P1a 步数临界保护的豁免面：detect_anomaly 是结论 level 的确定性事实来源，
# 预算临界时必须仍可得（inspector 收尾抢救 P1c 依赖它兜底出结论）。
_LAST_STEP_EXEMPT_TOOLS = frozenset({"detect_anomaly"})
_LAST_STEP_RESERVE_DETAIL = "步数预算只剩最后 1 步：拒绝再调用读工具，必须基于已观测数据立即给出结论"

# P1b 重复调用引导语：连续两次同工具同参数的读调用，第二遍只是重复观测
_DUPLICATE_CALL_DETAIL = "连续两次相同工具名+参数：拒绝重复调用——请更换工具或基于已观测数据给出结论"


def _args_fingerprint(args: dict) -> str:
    """调用参数指纹（P1b 连续重复判定）：规范化 JSON，与幂等账本的 args_hash 口径一致。"""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):  # 不可序列化参数按字符串形态比对（退化不崩溃）
        return repr(args)


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
        self._last_read_call: tuple[str, str] | None = None  # P1b：最近一次成功读调用指纹（连续重复判定）

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

        # 2.5) 步数临界保护（P1a）：只剩最后 1 步时拒绝再执行读工具——把最后一步
        # 留给结论；拒绝事件带明示引导，模型仍可基于已观测数据出结论。只在
        # read 工具上生效：写/高危工具不拦（reporter 在步数耗尽后仍须能落工单）。
        if (
            check.spec.effect == "read"
            and name not in _LAST_STEP_EXEMPT_TOOLS
            and self._tracker.steps_left() <= 1
        ):
            await self._deny(name, "last_step_reserve", _LAST_STEP_RESERVE_DETAIL)
            return out("denied", reason="last_step_reserve", error=_LAST_STEP_RESERVE_DETAIL)

        # 2.6) 连续重复保护（P1b）：同一执行器实例上，紧接上一次成功调用之后
        # 的同工具名+同参数读调用直接拒绝——重复观测只浪费步数与工具预算。
        # 只在 read 生效：写/高危的重复由幂等账本去重（§9 语义不变）；
        # 被拒/出错的调用不记指纹（出错后重试同一调用仍然合法）。
        fingerprint = (name, _args_fingerprint(args))
        if check.spec.effect == "read" and fingerprint == self._last_read_call:
            await self._deny(name, "duplicate_call", _DUPLICATE_CALL_DETAIL)
            return out("denied", reason="duplicate_call", error=_DUPLICATE_CALL_DETAIL)

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
        if check.spec.effect == "read":
            self._last_read_call = fingerprint  # P1b：仅在成功执行后记指纹（连续重复的基准）
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
