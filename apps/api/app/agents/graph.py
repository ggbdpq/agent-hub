"""B5/B7/B8：巡检编排图（手写 Supervisor，ADR-0007）。

supervisor → planner → (Send 扇出 inspector×N) → reporter（汇总/工单/审批/报告）。
- Send 必须经 add_conditional_edges 发出（langgraph 1.2 实测，U1 探针）；
- 每个子任务 asyncio.wait_for 超时；失败子任务记录原因并继续 → partial + 覆盖率；
- 事件经 RunContext 发出（node.started/completed/failed、plan.created、evidence.attached）；
- 检查点（B8）由 build 参数注入，thread_id = run_id 由调用方 config 指定。
"""

import asyncio
import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import ValidationError

from app.agents.inspector import InspectorResult, run_inspection
from app.agents.planner import make_plan
from app.contracts.agent import Finding, InspectionReport, RunContext
from app.contracts.events import NewEvent
from app.tools.budget import BudgetTracker
from app.tools.inspection_tools import build_registry
from app.tools.registry import ToolRegistry

DEFAULT_TASK_TIMEOUT_S = 30.0
DEFAULT_INSPECTOR_STEPS = 6


class InspectionState(TypedDict, total=False):
    """图状态：findings/failures 用 operator.add reducer 合并并行 inspector 输出。

    dict 全程（无 pydantic 对象）：LangGraph 检查点序列化友好（B8）。
    """

    request: dict[str, Any]
    plan: dict[str, Any] | None
    plan_meta: dict[str, Any]
    tasks: list[dict[str, Any]]
    findings: Annotated[list[dict[str, Any]], operator.add]
    failures: Annotated[list[dict[str, Any]], operator.add]
    report: dict[str, Any] | None


def build_inspection_graph(
    ctx: RunContext,
    tracker: BudgetTracker,
    *,
    registry: ToolRegistry | None = None,
    fixture_root=None,
    task_timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
    inspector_max_steps: int = DEFAULT_INSPECTOR_STEPS,
    checkpointer=None,
):
    """编译巡检图。ctx/tracker 为 run 级共享（预算跨节点强制）。"""
    reg = registry if registry is not None else build_registry(ctx, fixture_root=fixture_root)

    graph = StateGraph(InspectionState)

    async def supervisor(state: InspectionState) -> dict:
        """入口：只发事件与校验入参（扇出经条件边，见 planner 后的 conditional_edges）。"""
        await ctx.emit(NewEvent(type="node.started", node="supervisor", payload={"phase": "supervisor"}))
        request = state.get("request") or {}
        try:
            from app.contracts.agent import InspectionRequest as _Req

            _Req.model_validate(request)
        except ValidationError as exc:
            await ctx.emit(NewEvent(type="node.failed", node="supervisor", payload={"error": str(exc)}))
            raise ValueError(f"非法巡检请求: {exc}") from exc
        return {}

    async def planner(state: InspectionState) -> dict:
        await ctx.emit(NewEvent(type="node.started", node="planner", payload={"phase": "planner"}))
        request = state["request"]
        req = _request_from(request)
        plan_result = await make_plan(req, ctx, tracker)
        tasks = [{"device_id": s.device_id, "checks": s.checks} for s in plan_result.plan.steps]
        await ctx.emit(
            NewEvent(
                type="plan.created",
                node="planner",
                payload={
                    "steps": tasks,
                    "fallback": plan_result.fallback,
                    "attempts": plan_result.attempts,
                    "errors": plan_result.errors,
                },
            )
        )
        await ctx.emit(NewEvent(type="node.completed", node="planner", payload={"tasks": len(tasks)}))
        return {
            "plan": plan_result.plan.model_dump(),
            "plan_meta": {
                "fallback": plan_result.fallback,
                "attempts": plan_result.attempts,
                "errors": plan_result.errors,
            },
            "tasks": tasks,
            "findings": [],
            "failures": [],
        }

    async def inspector(task: dict[str, Any]) -> dict[str, Any]:
        device_id = task["device_id"]
        checks = task.get("checks") or ["遥测比对"]
        await ctx.emit(
            NewEvent(type="node.started", node="inspector", payload={"device_id": device_id, "checks": checks})
        )
        try:
            outcome: InspectorResult = await asyncio.wait_for(
                run_inspection(
                    device_id,
                    checks,
                    ctx,
                    reg,
                    tracker,
                    max_steps=inspector_max_steps,
                    fixture_root=fixture_root,
                ),
                timeout=task_timeout_s,
            )
        except TimeoutError:
            await ctx.emit(
                NewEvent(
                    type="node.failed",
                    node="inspector",
                    payload={"device_id": device_id, "reason": f"timeout after {task_timeout_s:.1f}s"},
                )
            )
            return {"failures": [{"device_id": device_id, "reason": f"timeout after {task_timeout_s:.1f}s"}]}
        except Exception as exc:  # 子任务异常不拖垮整体（§9）
            await ctx.emit(
                NewEvent(
                    type="node.failed",
                    node="inspector",
                    payload={"device_id": device_id, "reason": f"error: {exc}"},
                )
            )
            return {"failures": [{"device_id": device_id, "reason": f"error: {exc}"}]}

        if outcome.finding is not None:
            await ctx.emit(
                NewEvent(
                    type="node.completed",
                    node="inspector",
                    payload={"device_id": device_id, "level": outcome.finding.level},
                )
            )
            return {"findings": [outcome.finding.model_dump()]}
        reason = outcome.failure_reason or "unknown"
        await ctx.emit(
            NewEvent(type="node.failed", node="inspector", payload={"device_id": device_id, "reason": reason})
        )
        return {"failures": [{"device_id": device_id, "reason": reason}]}

    async def reporter(state: InspectionState) -> dict:
        """汇总（B5）→ 工单与审批（B7 接管本节点的中断设计）。"""
        await ctx.emit(NewEvent(type="node.started", node="reporter", payload={"phase": "reporter"}))
        report = _assemble_report(state, ctx)
        await ctx.emit(
            NewEvent(
                type="node.completed",
                node="reporter",
                payload={"status": report.status, "coverage": report.coverage},
            )
        )
        return {"report": report.model_dump()}

    graph.add_node("supervisor", supervisor)
    graph.add_node("planner", planner)
    graph.add_node("inspector", inspector)
    graph.add_node("reporter", reporter)
    graph.add_edge(START, "supervisor")
    graph.add_edge("supervisor", "planner")
    # 并行扇出：每台设备一个 inspector 子任务（langgraph 1.x：Send 必须经条件边）
    graph.add_conditional_edges(
        "planner",
        lambda state: [Send("inspector", task) for task in state["tasks"]],
        ["inspector"],
    )
    graph.add_edge("inspector", "reporter")  # 全部汇合后进 reporter
    graph.add_edge("reporter", END)
    return graph.compile(checkpointer=checkpointer)


def _request_from(request: dict):
    from app.contracts.agent import InspectionRequest

    return InspectionRequest.model_validate(request)


def _assemble_report(state: InspectionState, ctx: RunContext) -> InspectionReport:
    """汇总 findings/failures → InspectionReport（partial 时覆盖率与原因可解释）。"""
    tasks = state.get("tasks") or []
    findings = [Finding.model_validate(f) for f in state.get("findings") or []]
    failures = state.get("failures") or []
    total = len(tasks)
    coverage = (len(findings) / total) if total else 0.0
    status = "completed" if not failures else "partial"
    notes_parts = []
    for failure in failures:
        notes_parts.append(f"{failure.get('device_id')}: {failure.get('reason')}")
    if ctx.cancelled():
        status = "partial"
        notes_parts.append("run 被请求取消，提前结束")
    if state.get("plan_meta", {}).get("fallback"):
        notes_parts.append("规划失败，使用默认全量检查计划")
    return InspectionReport(
        findings=findings,
        work_orders=[],
        status=status,
        coverage=round(coverage, 4),
        notes="；".join(notes_parts) if notes_parts else None,
    )
