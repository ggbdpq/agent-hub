"""B3 规划 Worker（Plan-and-Execute）：结构化 Plan、Schema 校验失败重试 2 次、确定性兜底。

计划 §7.2-B3：FakeLLM 返回非法 JSON 时走兜底；未知设备剔除；Token 预算超限停止。
"""

import json

from langchain_core.messages import AIMessage

from app.agents.planner import Plan, PlanResult, default_plan, make_plan
from app.contracts.agent import Budget, InspectionRequest
from app.contracts.fakes import FakeLLM
from app.tools.budget import BudgetTracker
from b_testkit import make_ctx, sequence_llm_factory

REQUEST = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-005", "WT-002"])


def _plan_json() -> str:
    return json.dumps(
        {
            "steps": [
                {"device_id": "INV-001", "checks": ["外观", "遥测比对"]},
                {"device_id": "INV-005", "checks": ["柜温趋势", "散热"]},
                {"device_id": "WT-002", "checks": ["振动"]},
            ]
        },
        ensure_ascii=False,
    )


async def test_valid_json_plan_parsed() -> None:
    ctx, _ = make_ctx(llm=FakeLLM.factory([_plan_json()]))
    tracker = BudgetTracker(ctx.budget)
    result: PlanResult = await make_plan(REQUEST, ctx, tracker)
    assert result.fallback is False
    assert result.attempts == 1
    assert [s.device_id for s in result.plan.steps] == ["INV-001", "INV-005", "WT-002"]
    assert tracker.tokens_used > 0


async def test_fenced_json_plan_parsed() -> None:
    fenced = f"```json\n{_plan_json()}\n```"
    ctx, _ = make_ctx(llm=FakeLLM.factory([fenced]))
    result = await make_plan(REQUEST, ctx, BudgetTracker(ctx.budget))
    assert result.fallback is False
    assert len(result.plan.steps) == 3


async def test_invalid_json_falls_back_after_two_retries() -> None:
    """Schema 校验失败最多重试 2 次（共 3 次），仍失败 → 全量默认检查计划。"""
    ctx, _ = make_ctx(llm=FakeLLM.factory(["这不是JSON", "也不是", "依然不是"]))
    tracker = BudgetTracker(ctx.budget)
    result = await make_plan(REQUEST, ctx, tracker)
    assert result.fallback is True
    assert result.attempts == 3
    assert len(result.errors) == 3  # errors 只记失败原因，兜底说明在 plan.note
    assert [s.device_id for s in result.plan.steps] == REQUEST.device_ids
    assert all(s.checks for s in result.plan.steps)


async def test_second_attempt_recovers() -> None:
    ctx, _ = make_ctx(llm=sequence_llm_factory(["坏输出", _plan_json()]))
    result = await make_plan(REQUEST, ctx, BudgetTracker(ctx.budget))
    assert result.fallback is False
    assert result.attempts == 2


async def test_unknown_device_steps_dropped_all_invalid_falls_back() -> None:
    """计划含未知设备：剔除非法步骤；全部非法 → 兜底。"""
    bad = json.dumps({"steps": [{"device_id": "XX-999", "checks": ["x"]}]}, ensure_ascii=False)
    ctx, _ = make_ctx(llm=sequence_llm_factory([bad, bad, bad]))
    result = await make_plan(REQUEST, ctx, BudgetTracker(ctx.budget))
    assert result.fallback is True
    assert any("计划无有效步骤" in e for e in result.errors)


async def test_partial_invalid_steps_filtered() -> None:
    mixed = json.dumps(
        {
            "steps": [
                {"device_id": "INV-005", "checks": ["柜温"]},
                {"device_id": "XX-999", "checks": ["x"]},
            ]
        },
        ensure_ascii=False,
    )
    ctx, _ = make_ctx(llm=FakeLLM.factory([mixed]))
    result = await make_plan(REQUEST, ctx, BudgetTracker(ctx.budget))
    assert result.fallback is False
    assert [s.device_id for s in result.plan.steps] == ["INV-005"]


async def test_token_budget_exceeded_falls_back() -> None:
    """Token 预算耗尽：不再尝试，直接确定性兜底（可解释）。"""
    ctx, _ = make_ctx(llm=FakeLLM.factory([_plan_json(), _plan_json()]), budget=Budget(max_tokens=1))
    tracker = BudgetTracker(ctx.budget)
    result = await make_plan(REQUEST, ctx, tracker)
    assert result.fallback is True
    assert any("token" in e for e in result.errors)
    assert result.attempts == 1  # 第一次就超限，不再重试


async def test_cancelled_falls_back_immediately() -> None:
    ctx, _ = make_ctx(llm=FakeLLM.factory([_plan_json()]), cancelled=True)
    result = await make_plan(REQUEST, ctx, BudgetTracker(ctx.budget))
    assert result.fallback is True
    assert result.attempts == 0


def test_default_plan_deterministic_and_covers_all() -> None:
    p1 = default_plan(REQUEST)
    p2 = default_plan(REQUEST)
    assert p1 == p2
    assert [s.device_id for s in p1.steps] == REQUEST.device_ids


def test_plan_model_roundtrip() -> None:
    plan = Plan.model_validate_json(_plan_json())
    assert Plan.model_validate_json(plan.model_dump_json()) == plan


def test_usage_metadata_tokens_preferred_over_estimate() -> None:
    """AIMessage 带 usage_metadata 时优先使用（脚本可注入精确用量）。"""
    from app.tools.budget import usage_from_message

    msg = AIMessage(
        content="hi",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )
    assert usage_from_message(msg) == 15
