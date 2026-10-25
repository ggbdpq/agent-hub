"""B4：巡检分析 Worker（ReAct）——每台设备一个子任务。

与 langgraph 预置 agent 的差异：全部工具调用走 B2 执行器（预算/权限/账本强制），
结论由代码校验——level 以 detect_anomaly 确定性判读为准（LLM 只做解释），
evidence_chunk_ids 剔除未观测到的引用（§9：结论必须引用真实存在的证据）。

ReAct 协议（纯 JSON，不依赖原生 tool_calls——FakeLLM 确定性回放友好）：
模型每轮输出 ``{"tool": name, "args": {...}}`` 或 ``{"final": {...}}``。
设备型工具的 device_id 由代码覆盖为子任务设备（模型无法越界读他机数据）。
"""

import json
import re
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.agents.briefs import brief_json, load_brief
from app.contracts.agent import Finding, RunContext
from app.contracts.events import NewEvent
from app.skills.base import Skill, resolve_allowed_tools, skill_prompt_fragments
from app.tools.anomaly import AnomalyResult
from app.tools.budget import BudgetTracker, usage_from_message
from app.tools.executor import ToolExecutor, ToolOutcome
from app.tools.registry import ToolRegistry

DEFAULT_MAX_STEPS = 6
DEVICE_SCOPED_TOOLS = {"get_telemetry", "detect_anomaly"}

_SYSTEM_TEMPLATE = """你是设备巡检分析员，负责一台设备的异常判读与解释。数值判断已由确定性工具完成，你只做解释与决策。

可用工具（只读）：{tools}

每轮只输出一个 JSON 对象（不要多余文字）：
- 调工具：{{"tool": "工具名", "args": {{...}}}}
- 给结论：{{"final": {{"summary": "一句话结论", "level": "normal|warning|critical", "evidence_chunk_ids": ["引用的规程 chunk_id"], "metrics": {{}}}}}}

要求：
- level 必须以 detect_anomaly 的判读结果为准；
- evidence_chunk_ids 只能引用 search_procedures 返回过的 chunk_id；
- 最多 {max_steps} 轮。"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


@dataclass
class InspectorResult:
    device_id: str
    finding: Finding | None = None
    failure_reason: str | None = None
    steps_used: int = 0
    errors: list[str] = field(default_factory=list)


async def run_inspection(
    device_id: str,
    checks: list[str],
    ctx: RunContext,
    registry: ToolRegistry,
    tracker: BudgetTracker,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
    fixture_root=None,
    skills: list[Skill] | None = None,
) -> InspectorResult:
    """单个设备子任务的 ReAct 循环。永不抛异常：失败转 failure_reason（B5 记 partial）。

    超时强制在图节点层（asyncio.wait_for，B5）；skills（B10）：白名单取各 Skill
    allowed_tools 的并集（声明之外不可用），系统提示追加 Skill 片段；
    不传则全量读工具可用（行为同 B4）。
    """
    result = InspectorResult(device_id=device_id)
    executor = ToolExecutor(
        registry,
        ctx,
        node="inspector",
        tracker=tracker,
        allowed=resolve_allowed_tools(skills),
    )

    observed_chunks: set[str] = set()
    anomaly: AnomalyResult | None = None
    steps_used = 0
    unparseable_seen = False  # 模型试图输出结论但不可解析（抢救路径的条件）

    if ctx.cancelled():
        result.failure_reason = "cancelled"
        return result

    brief = load_brief(device_id, checks, fixture_root=fixture_root)
    tools_desc = json.dumps(
        [
            {"name": s.name, "description": s.description}
            for s in registry.for_node("inspector")  # 权限面即工具面（R3：list_devices 不在 inspector 节点）
        ],
        ensure_ascii=False,
    )
    system = _SYSTEM_TEMPLATE.format(tools=tools_desc, max_steps=max_steps)
    fragments = skill_prompt_fragments(skills)
    if fragments:
        system = f"{system}\n\n{fragments}"
    messages: list = [SystemMessage(system), HumanMessage(brief_json(brief))]

    try:
        model = ctx.llm()
    except Exception as exc:  # 模型工厂失败：子任务失败
        result.failure_reason = f"llm_factory_error: {exc}"
        return result

    for _ in range(max_steps):
        if ctx.cancelled():
            result.failure_reason = "cancelled"
            return result
        steps_used += 1
        if not tracker.consume_step():
            return await _rescue_or_fail(
                result, "step_budget_exhausted", anomaly, observed_chunks, steps_used, executor, ctx, device_id, unparseable_seen, fixture_root
            )
        try:
            reply = await model.ainvoke(messages)
        except Exception as exc:
            return await _rescue_or_fail(
                result, f"llm_error: {exc}", anomaly, observed_chunks, steps_used, executor, ctx, device_id, unparseable_seen, fixture_root
            )
        if not tracker.consume_tokens(usage_from_message(reply)):
            return await _rescue_or_fail(
                result, "token_budget_exhausted", anomaly, observed_chunks, steps_used, executor, ctx, device_id, unparseable_seen, fixture_root
            )

        action = _parse_action(reply.content)
        if action is None:
            # 本轮输出不可解析：提示模型修正，继续循环（消耗步数）
            unparseable_seen = True
            messages.append(HumanMessage('输出不可解析。请只输出 {"tool": ...} 或 {"final": ...} JSON。'))
            continue
        if "final" in action:
            if anomaly is None:
                anomaly = await _ensure_anomaly(executor, ctx, device_id)
            return await _finalize(
                result, action["final"], anomaly, observed_chunks, steps_used, ctx, device_id
            )
        tool_name = action.get("tool", "")
        args = dict(action.get("args") or {})
        if tool_name in DEVICE_SCOPED_TOOLS:
            args["device_id"] = device_id  # 代码强制：只能读本子任务设备
        outcome = await executor.call(tool_name, args)
        _absorb(outcome, observed_chunks)
        if outcome.status == "ok" and tool_name == "detect_anomaly":
            anomaly = outcome.result
        messages.append(
            HumanMessage(
                json.dumps(
                    {
                        "tool": tool_name,
                        "status": outcome.status,
                        "reason": outcome.reason,  # denied 时把拒绝原因给到模型（P1a 引导）
                        "result": outcome.result.model_dump() if outcome.result else outcome.error,
                    },
                    ensure_ascii=False,
                )
            )
        )
        result.steps_used = steps_used

    # 循环结束仍无有效结论：模型若已试图下结论（含垃圾输出），用确定性判读抢救
    return await _rescue_or_fail(
        result, "step_budget_exhausted", anomaly, observed_chunks, steps_used, executor, ctx, device_id, unparseable_seen, fixture_root
    )


def _parse_action(content: str) -> dict | None:
    text = content.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _absorb(outcome: ToolOutcome, observed_chunks: set[str]) -> None:
    """记录模型真实观测到的 chunk_id（后续引用校验的事实集合）。"""
    if outcome.status == "ok" and outcome.tool == "search_procedures":
        for chunk in outcome.result.chunks:
            observed_chunks.add(chunk.chunk_id)


async def _ensure_anomaly(
    executor: ToolExecutor, ctx: RunContext, device_id: str
) -> AnomalyResult | None:
    """模型没跑判读时由代码补跑（level 永远有确定性来源）。"""
    outcome = await executor.call("detect_anomaly", {"device_id": device_id})
    return outcome.result if outcome.status == "ok" else None


_BUDGET_EXHAUSTED_REASONS = frozenset({"step_budget_exhausted", "token_budget_exhausted"})


async def _rescue_or_fail(
    result: InspectorResult,
    reason: str,
    anomaly: AnomalyResult | None,
    observed_chunks: set[str],
    steps_used: int,
    executor: ToolExecutor,
    ctx: RunContext,
    device_id: str,
    unparseable_seen: bool,
    fixture_root,
) -> InspectorResult:
    """终止抢救（P1c 扩面）：预算耗尽或模型已试图下结论（垃圾输出）时，以确定性
    判读兜底出结论——判读可得（已观测或补跑成功）的设备照常出 finding，不再
    整 run 判 partial（E1 hard 档 H13/H17 失败形态）；判读仍不可得的设备才记
    失败（reason 进 notes，可解释）。llm_error 与预算无关，不扩面，保持原语义。"""
    if unparseable_seen or reason in _BUDGET_EXHAUSTED_REASONS:
        if anomaly is None:
            anomaly = await _ensure_anomaly(executor, ctx, device_id)
        if anomaly is not None:
            final = {"evidence_chunk_ids": sorted(observed_chunks)}  # 只含真实观测
            return await _finalize(
                result, final, anomaly, observed_chunks, steps_used, ctx, device_id
            )
    result.failure_reason = reason
    return _finalize_failure(result, steps_used)


def _fallback_summary(anomaly: AnomalyResult | None) -> str:
    if anomaly is None:
        return "判读工具不可用，未得出结论"
    if anomaly.level == "normal":
        return "遥测平稳，判读无异常"
    parts = [f"{a.metric}{_kind_cn(a.kind)}" for a in anomaly.anomalies]
    return f"判读异常：{'；'.join(parts)}"


def _kind_cn(kind: str) -> str:
    return {"threshold": "超阈值", "trend": "趋势漂移", "step": "阶跃突变", "drop": "功率骤降"}.get(kind, "异常")


async def _finalize(
    result: InspectorResult,
    final: dict,
    anomaly: AnomalyResult | None,
    observed_chunks: set[str],
    steps_used: int,
    ctx: RunContext,
    device_id: str,
) -> InspectorResult:
    """结论校验与兜底：level 以判读为准，chunk 引用剔除未观测项。"""
    result.steps_used = steps_used
    if anomaly is None:  # 判读不可用（预算/权限被拒）：以失败计，不编造结论
        result.failure_reason = "anomaly_unavailable"
        return result

    model_level = final.get("level")
    model_summary = final.get("summary")
    raw_chunks = final.get("evidence_chunk_ids") or []
    model_metrics = final.get("metrics") or {}

    errors: list[str] = []
    evidence = [c for c in raw_chunks if c in observed_chunks]
    dropped = len(raw_chunks) - len(evidence)
    if dropped:
        errors.append(f"剔除 {dropped} 个未观测到的 chunk 引用")
    if model_level not in ("normal", "warning", "critical"):
        errors.append("模型未给出合法 level")
    if model_level not in (None, anomaly.level):
        errors.append(f"模型 level={model_level} 与判读 {anomaly.level} 不符，以判读为准")

    metrics: dict = {"anomalies": [a.model_dump() for a in anomaly.anomalies]}
    if isinstance(model_metrics, dict):
        metrics.update({k: v for k, v in model_metrics.items() if isinstance(k, str)})

    summary = (
        model_summary if isinstance(model_summary, str) and model_summary.strip() else _fallback_summary(anomaly)
    )

    result.finding = Finding(
        device_id=device_id,
        level=anomaly.level,  # 代码强制：确定性判读优先
        summary=summary[:500],
        evidence_chunk_ids=evidence,
        metrics=metrics,
    )
    result.errors = errors
    if evidence:
        await ctx.emit(
            NewEvent(
                type="evidence.attached",
                node="inspector",
                payload={"device_id": device_id, "chunk_ids": evidence},
            )
        )
    return result


def _finalize_failure(result: InspectorResult, steps_used: int) -> InspectorResult:
    result.steps_used = steps_used
    result.finding = None
    return result
