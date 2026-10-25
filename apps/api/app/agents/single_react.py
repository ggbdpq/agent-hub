"""B11：单智能体基线（供消融）——同样的工具，一个 ReAct 循环做完全部。

与 InspectionAgentRunner（B12）的对照变量：无规划、无 Send 扇出、无子任务上下文
隔离（device_id 由模型自定）、无审批 interrupt。保留的运行时强制（同一执行器）：
节点权限、预算、幂等账本、dangerous 审批凭证、结论 level 以确定性判读为准、
证据 chunk_id 必须真实观测（§9 不变量不由编排结构承担）。
"""

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from app.contracts.agent import AgentRunner, Finding, InspectionReport, InspectionRequest, RunContext, WorkOrder
from app.contracts.events import NewEvent
from app.tools.anomaly import AnomalyResult, detect
from app.tools.budget import BudgetTracker, usage_from_message
from app.tools.devices import DEFAULT_FIXTURE_ROOT
from app.tools.executor import ToolExecutor
from app.tools.inspection_tools import build_registry
from app.tools.registry import ToolRegistry

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

_SYSTEM = """你是巡检智能体：独自完成全部设备的巡检（读遥测、跑判读、查规程、给结论、起工单）。
可用工具：{tools}

每轮只输出一个 JSON 对象（不要多余文字）：
- 调工具：{{"tool": "工具名", "args": {{...}}}}
- 收尾：{{"final": {{"findings": [{{"device_id": "...", "level": "normal|warning|critical", "summary": "...", "evidence_chunk_ids": [...], "metrics": {{}}}}], "orders": [{{"device_id": "...", "title": "...", "action": "..."}}], "notes": "..."}}}}

要求：覆盖请求清单中的每台设备；level 以 detect_anomaly 判读为准；
evidence_chunk_ids 只能引用 search_procedures 返回过的 chunk_id。"""


def _parse_json(content: str) -> dict | None:
    text = content.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


class SingleReActRunner(AgentRunner):
    """消融基线：实现 AgentRunner 协议，与多智能体共用评测样例（C8/C9 消费）。"""

    def __init__(self, *, fixture_root=None, registry: ToolRegistry | None = None) -> None:
        self._fixture_root = fixture_root
        self._registry = registry

    async def run(
        self,
        run_id: str,
        input: InspectionRequest,
        ctx: RunContext,
        tracker: BudgetTracker | None = None,
    ) -> InspectionReport:
        """单 ReAct 循环跑完全部设备；永不抛异常（预算/异常降级 partial）。"""
        tracker = tracker or BudgetTracker(ctx.budget)
        fixture_root = self._fixture_root if self._fixture_root is not None else DEFAULT_FIXTURE_ROOT
        registry = self._registry or build_registry(ctx, fixture_root=fixture_root)
        executor = ToolExecutor(registry, ctx, node="supervisor", tracker=tracker)
        await ctx.emit(NewEvent(type="node.started", node="supervisor", payload={"phase": "single-react"}))

        observed_chunks: set[str] = set()
        anomaly_cache: dict[str, AnomalyResult] = {}
        errors: list[str] = []
        model = ctx.llm()
        tools_desc = json.dumps(
            [{"name": s.name, "description": s.description} for s in registry.for_node("supervisor")],
            ensure_ascii=False,
        )
        messages: list = [
            SystemMessage(_SYSTEM.format(tools=tools_desc)),
            HumanMessage(
                json.dumps(
                    {"site": input.site, "devices": input.device_ids, "goal": input.goal or "日常巡检"},
                    ensure_ascii=False,
                )
            ),
        ]

        final: dict[str, Any] | None = None
        while final is None:
            if ctx.cancelled() or not tracker.consume_step():
                errors.append("步数预算耗尽或已取消，未能产出结论")
                break
            try:
                reply = await model.ainvoke(messages)
            except Exception as exc:
                errors.append(f"llm_error: {exc}")
                break
            if not tracker.consume_tokens(usage_from_message(reply)):
                errors.append("token 预算耗尽")
                break
            action = _parse_json(reply.content)
            if action is None:
                messages.append(HumanMessage('输出不可解析。请只输出 {"tool": ...} 或 {"final": ...} JSON。'))
                continue
            if "final" in action:
                final = action["final"] if isinstance(action["final"], dict) else {}
                break
            tool_name = str(action.get("tool", ""))
            outcome = await executor.call(tool_name, dict(action.get("args") or {}))
            if outcome.status == "ok" and tool_name == "search_procedures":
                observed_chunks.update(c.chunk_id for c in outcome.result.chunks)
            if outcome.status == "ok" and tool_name == "detect_anomaly":
                anomaly_cache[str(getattr(outcome.result, "device_id", ""))] = outcome.result
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

        report = await self._assemble(
            run_id, input, final, observed_chunks, anomaly_cache, errors, executor, fixture_root
        )
        await ctx.emit(
            NewEvent(
                type="node.completed",
                node="supervisor",
                payload={"status": report.status, "coverage": report.coverage},
            )
        )
        return report

    async def _assemble(
        self,
        run_id: str,
        input: InspectionRequest,
        final: dict[str, Any] | None,
        observed_chunks: set[str],
        anomaly_cache: dict[str, AnomalyResult],
        errors: list[str],
        executor: ToolExecutor,
        fixture_root,
    ) -> InspectionReport:
        """结论校验（level 判读校正 + 证据过滤）→ 工单幂等创建 → 报告。"""
        findings: list[Finding] = []
        if final:
            for raw in final.get("findings") or []:
                try:
                    model_finding = Finding.model_validate(raw)
                except ValidationError:
                    errors.append(f"无效 finding 已剔除: {json.dumps(raw, ensure_ascii=False)[:80]}")
                    continue
                anomaly = anomaly_cache.get(model_finding.device_id) or self._ensure_anomaly(
                    model_finding.device_id, fixture_root
                )
                evidence = [c for c in model_finding.evidence_chunk_ids if c in observed_chunks]
                level = anomaly.level if anomaly else model_finding.level
                metrics = dict(model_finding.metrics)
                if anomaly:
                    metrics.setdefault("anomalies", [a.model_dump() for a in anomaly.anomalies])
                findings.append(
                    Finding(
                        device_id=model_finding.device_id,
                        level=level,
                        summary=model_finding.summary[:500],
                        evidence_chunk_ids=evidence,
                        metrics=metrics,
                    )
                )
        notes_parts: list[str] = list(errors)
        if final and isinstance(final.get("notes"), str) and final["notes"].strip():
            notes_parts.append(final["notes"])
        covered = {f.device_id for f in findings}
        coverage = len(covered) / len(input.device_ids) if input.device_ids else 0.0
        status = "completed" if final is not None and coverage >= 1.0 else "partial"
        return InspectionReport(
            findings=findings,
            work_orders=await self._create_orders(run_id, final or {}, findings, executor),
            status=status,
            coverage=round(coverage, 4),
            notes="；".join(notes_parts) if notes_parts else None,
        )

    async def _create_orders(
        self, run_id: str, final: dict[str, Any], findings: list[Finding], executor: ToolExecutor
    ) -> list[WorkOrder]:
        """final.orders → create_work_order（幂等账本键；只给已巡检设备开工单）。"""
        orders: list[WorkOrder] = []
        known = {f.device_id for f in findings}
        for raw in final.get("orders") or []:
            try:
                order = WorkOrder.model_validate(raw)
            except ValidationError:
                continue
            if order.device_id not in known:
                continue
            outcome = await executor.call(
                "create_work_order",
                {"device_id": order.device_id, "title": order.title, "action": order.action},
                ledger_key=f"{run_id}:supervisor:wo:{order.device_id}",
            )
            if outcome.status in ("ok", "deduplicated") and outcome.result is not None:
                orders.append(outcome.result.work_order)
        return orders

    def _ensure_anomaly(self, device_id: str, fixture_root) -> AnomalyResult | None:
        try:
            return detect(device_id, fixture_root)
        except Exception:
            return None
