"""B2 工具执行器：权限→校验→预算→超时→账本→结果校验→事件的运行时强制层。

计划 §7.2-B2 四类必测：超预算、超时、参数非法、重复写入；
外加 dangerous 审批凭证、账本状态未知不自动重试（不变量 §9）。
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel

from app.contracts.agent import Budget
from app.tools.executor import ToolExecutor, ToolOutcome
from app.tools.inspection_tools import build_registry
from b_testkit import FIXTURES, make_ctx


def _executor(**kwargs) -> tuple[ToolExecutor, object, list]:
    from app.tools.budget import BudgetTracker

    ctx, collector = make_ctx(**kwargs)
    registry = build_registry(ctx, fixture_root=FIXTURES)
    tracker = BudgetTracker(ctx.budget)
    return ToolExecutor(registry, ctx, node="inspector", tracker=tracker), ctx, collector


def _node_executor(node: str, **kwargs) -> tuple[ToolExecutor, object, list]:
    from app.tools.budget import BudgetTracker

    ctx, collector = make_ctx(**kwargs)
    registry = build_registry(ctx, fixture_root=FIXTURES)
    return ToolExecutor(registry, ctx, node=node, tracker=BudgetTracker(ctx.budget)), ctx, collector


async def test_happy_path_read_tool_emits_called_and_result() -> None:
    ex, _, collector = _executor()
    outcome = await ex.call("get_telemetry", {"device_id": "INV-001"})
    assert outcome.status == "ok"
    assert outcome.result.device_id == "INV-001"
    types = collector.types()
    assert types == ["tool.called", "tool.result"]
    assert collector.of_type("tool.result")[0].payload["ok"] is True


async def test_budget_tool_calls_exhausted_denies() -> None:
    """超预算：max_tool_calls=1，第二次调用被拒且发 tool.denied。"""
    ex, _, collector = _executor(budget=Budget(max_tool_calls=1))
    first = await ex.call("list_devices", {})
    assert first.status == "ok"
    second = await ex.call("list_devices", {})
    assert second.status == "denied"
    assert second.reason == "budget_tool_calls"
    assert collector.of_type("tool.denied")[-1].payload["reason"] == "budget_tool_calls"


async def test_budget_deadline_exceeded_denies() -> None:
    """超预算：deadline 已过 → 拒绝执行。"""
    past = datetime.now(UTC) - timedelta(seconds=1)
    ex, _, _ = _executor(budget=Budget(deadline=past))
    outcome = await ex.call("list_devices", {})
    assert outcome.status == "denied"
    assert outcome.reason == "deadline_exceeded"


async def test_timeout_read_tool_is_retryable() -> None:
    """超时（read/safe）：outcome error 且可重试。"""

    async def slow(_: BaseModel) -> BaseModel:
        await asyncio.sleep(1)
        raise AssertionError("不应执行到这里")  # pragma: no cover

    ctx, collector = make_ctx()
    base = build_registry(ctx, fixture_root=FIXTURES)
    slow_spec = base.get("get_telemetry").model_copy(update={"handler": slow, "timeout_s": 0.05})
    registry = build_registry(ctx, fixture_root=FIXTURES, spec_overrides={"get_telemetry": slow_spec})
    from app.tools.budget import BudgetTracker

    ex = ToolExecutor(registry, ctx, node="inspector", tracker=BudgetTracker(ctx.budget))
    outcome = await ex.call("get_telemetry", {"device_id": "INV-001"})
    assert outcome.status == "error"
    assert "timeout" in (outcome.error or "")
    assert outcome.retryable is True


async def test_timeout_write_tool_marks_ledger_unknown() -> None:
    """超时（write/ledger）：账本记 unknown，retryable=False，不自动重试（§9 不变量）。"""

    async def slow(_: BaseModel) -> BaseModel:
        await asyncio.sleep(1)

    ctx, collector = make_ctx()
    base = build_registry(ctx, fixture_root=FIXTURES)
    slow_spec = base.get("create_work_order").model_copy(update={"handler": slow, "timeout_s": 0.05})
    registry = build_registry(ctx, fixture_root=FIXTURES, spec_overrides={"create_work_order": slow_spec})
    from app.tools.budget import BudgetTracker

    ex = ToolExecutor(registry, ctx, node="reporter", tracker=BudgetTracker(ctx.budget))
    outcome = await ex.call(
        "create_work_order",
        {"device_id": "INV-005", "title": "t", "action": "a"},
        ledger_key="run1:reporter:wo:INV-005",
    )
    assert outcome.status == "error"
    assert outcome.retryable is False
    entry = ctx.ledger.begin("run1:reporter:wo:INV-005", "create_work_order", "x")
    assert entry.status == "unknown"
    # 同键再调：拒绝执行（状态未知禁止自动重试）
    again = await ex.call(
        "create_work_order",
        {"device_id": "INV-005", "title": "t", "action": "a"},
        ledger_key="run1:reporter:wo:INV-005",
    )
    assert again.status == "denied"
    assert again.reason == "ledger_state_unknown"


async def test_invalid_args_denied_without_execution() -> None:
    """参数非法：denied invalid_args，handler 不执行。"""
    ex, _, collector = _executor()
    outcome = await ex.call("get_telemetry", {})  # 缺 device_id
    assert outcome.status == "denied"
    assert outcome.reason == "invalid_args"
    assert "tool.called" not in collector.types()


async def test_duplicate_write_deduplicated_handler_runs_once() -> None:
    """重复写入：同 ledger_key 第二次 deduplicated，handler 只执行一次（§9 不变量）。"""
    calls = {"n": 0}

    async def counting_handler(args: BaseModel) -> BaseModel:
        calls["n"] += 1
        from app.tools.inspection_tools import CreateWorkOrderArgs, CreateWorkOrderOut
        from app.contracts.agent import WorkOrder

        assert isinstance(args, CreateWorkOrderArgs)
        return CreateWorkOrderOut(work_order=WorkOrder(device_id=args.device_id, title=args.title, action=args.action))

    ctx, collector = make_ctx()
    base = build_registry(ctx, fixture_root=FIXTURES)
    spec = base.get("create_work_order").model_copy(update={"handler": counting_handler})
    registry = build_registry(ctx, fixture_root=FIXTURES, spec_overrides={"create_work_order": spec})
    from app.tools.budget import BudgetTracker

    ex = ToolExecutor(registry, ctx, node="reporter", tracker=BudgetTracker(ctx.budget))
    key = "run1:reporter:wo:INV-005"
    args = {"device_id": "INV-005", "title": "柜温漂移", "action": "检查散热"}
    first = await ex.call("create_work_order", args, ledger_key=key)
    second = await ex.call("create_work_order", args, ledger_key=key)
    assert first.status == "ok"
    assert second.status == "deduplicated"
    assert second.result.work_order.device_id == "INV-005"
    assert calls["n"] == 1
    assert "tool.deduplicated" in collector.types()


async def test_write_requires_reproducible_ledger_key() -> None:
    """write 工具缺 ledger_key → error 不执行（可复现键由编排层生成）。"""
    ex, _, _ = _node_executor("reporter")
    outcome = await ex.call("create_work_order", {"device_id": "d", "title": "t", "action": "a"})
    assert outcome.status == "error"
    assert outcome.reason == "ledger_key_required"


async def test_dangerous_tool_requires_approval_id() -> None:
    """dangerous：无 approval_id 拒绝；有则放行（仅模拟）。"""
    ex, _, _ = _node_executor("reporter")
    denied = await ex.call("dispatch_drone", {"device_id": "INV-005", "reason": "r"})
    assert denied.status == "denied"
    assert denied.reason == "approval_required"
    ok = await ex.call(
        "dispatch_drone", {"device_id": "INV-005", "reason": "r", "approval_id": "AP-1"},
        ledger_key="run1:reporter:drone:INV-005",
    )
    assert ok.status == "ok"
    assert ok.result.status == "simulated"


async def test_allowlist_narrows_skill_scope() -> None:
    """Skill 白名单（B10 前置）：allowed 之外的注册工具也拒绝。"""
    from app.tools.budget import BudgetTracker

    ctx, collector = make_ctx()
    registry = build_registry(ctx, fixture_root=FIXTURES)
    ex = ToolExecutor(registry, ctx, node="inspector", tracker=BudgetTracker(ctx.budget), allowed={"list_devices"})
    outcome = await ex.call("get_telemetry", {"device_id": "INV-001"})
    assert outcome.status == "denied"
    assert outcome.reason == "not_in_allowlist"


async def test_cancelled_context_denies() -> None:
    ex, _, _ = _executor(cancelled=True)
    outcome = await ex.call("list_devices", {})
    assert outcome.status == "denied"
    assert outcome.reason == "cancelled"


def test_tool_outcome_shape() -> None:
    o = ToolOutcome(tool="x", status="ok")
    assert o.result is None and o.retryable is None
