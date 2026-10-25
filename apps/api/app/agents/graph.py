"""B5/B7/B8：巡检编排图（手写 Supervisor，ADR-0007）。

supervisor → planner → (Send 扇出 inspector×N) → reporter（汇总/工单/审批/报告）。
- Send 必须经 add_conditional_edges 发出（langgraph 1.2 实测，U1 探针）；
- 每个子任务 asyncio.wait_for 超时；失败子任务记录原因并继续 → partial + 覆盖率；
- 事件经 RunContext 发出（node.started/completed/failed、plan.created、evidence.attached）；
- 检查点（B8）由 build 参数注入，thread_id = run_id 由调用方 config 指定。
"""

import asyncio
import json
import re
from typing import Annotated, Any, TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt
from pydantic import ValidationError

from app.agents.inspector import InspectorResult, run_inspection
from app.agents.planner import make_plan
from app.contracts.agent import Finding, InspectionReport, RunContext, WorkOrder
from app.contracts.events import NewEvent
from app.tools.budget import BudgetTracker, usage_from_message
from app.tools.executor import ToolExecutor
from app.tools.inspection_tools import build_registry
from app.tools.registry import ToolRegistry

DEFAULT_TASK_TIMEOUT_S = 30.0
DEFAULT_INSPECTOR_STEPS = 6
_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _merge_by_device(
    existing: list[dict[str, Any]], update: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """findings/failures 的 reducer：按 device_id 去重合并，后写覆盖先写。

    R1 加固：任何"检查点存在又带输入重启"的路径（误用/未来回归）下，重跑节点
    的输出覆盖旧值而不是翻倍——coverage 永不 >1。
    """
    merged: dict[str, dict[str, Any]] = {}
    for item in (*existing, *update):
        key = item.get("device_id") if isinstance(item, dict) else str(item)
        merged[str(key)] = item
    return list(merged.values())

_ORDER_SYSTEM = """你是工单起草员。为异常设备起草检修工单。
只输出严格 JSON（不要多余文字）：
{"orders": [{"device_id": "设备编号", "title": "工单标题", "action": "处置措施"}]}
title 简明（≤40字），action 引用判读发现的异常参数与处置方向。"""

# -- P2：goal 只诊断判定（与评测口径一致：只诊断 → create_work_order 属 forbidden）--

# 显式「只诊断」信号：出现任一即认为 goal 声明了只诊断意图
_DIAGNOSIS_ONLY_MARKERS = (
    "只诊断",
    "仅诊断",
    "只出结论",
    "只给结论",
    "不开工单",
    "不生成工单",
    "不创建工单",
    "禁止创建工单",
    "禁止生成工单",
    "无需工单",
    "不需要工单",
)
# 工单/派工/处置动作词：肯定式出现即不算只诊断（禁止式提及不算）
_ACTION_MARKERS = (
    "生成工单",
    "创建工单",
    "开工单",
    "派工",
    "派单",
    "处置",
    "安排无人机",
    "无人机现场复核",
)
_NEGATION_WORDS = ("不", "禁止", "无需", "无须", "勿", "别", "非", "不能", "不得")


def _affirmative_action_goal(goal: str) -> bool:
    """是否存在肯定式动作词提及（前 2 字内含否定词视为禁止式提及）。"""
    for marker in _ACTION_MARKERS:
        pos = goal.find(marker)
        while pos != -1:
            if not any(neg in goal[max(0, pos - 2) : pos] for neg in _NEGATION_WORDS):
                return True
            pos = goal.find(marker, pos + 1)
    return False


def diagnosis_only_goal(goal: str | None) -> bool:
    """goal 是否为「只诊断」：有显式只诊断信号，且无肯定式工单/派工/处置动作词。

    口径（与 docs/integration-requests/Q.md 下行一致）：
    - 只诊断 → create_work_order 属 forbidden，reporter 跳过起草（notes 记原因）；
    - 判定要求 goal 带显式信号（只出结论/不开工单/禁止创建工单类）——无信号
      （如"例行巡检：遥测比对"或未填 goal）保持既有行为，起草不受影响；
    - 肯定式动作词优先：即便同时出现禁止式提及也不跳过（用户既要求处置）。
    """
    if not goal:
        return False
    if not any(marker in goal for marker in _DIAGNOSIS_ONLY_MARKERS):
        return False
    return not _affirmative_action_goal(goal)


class InspectionState(TypedDict, total=False):
    """图状态：findings/failures 用 operator.add reducer 合并并行 inspector 输出。

    dict 全程（无 pydantic 对象）：LangGraph 检查点序列化友好（B8）。
    """

    request: dict[str, Any]
    plan: dict[str, Any] | None
    plan_meta: dict[str, Any]
    tasks: list[dict[str, Any]]
    findings: Annotated[list[dict[str, Any]], _merge_by_device]
    failures: Annotated[list[dict[str, Any]], _merge_by_device]
    work_orders: list[dict[str, Any]]
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
    inspector_skills: list | None = None,
):
    """编译巡检图。ctx/tracker 为 run 级共享（预算跨节点强制）。

    inspector_skills（B10）：传入 Skill 实例列表 → inspector 白名单并集 + 提示片段。
    """
    reg = registry if registry is not None else build_registry(ctx, fixture_root=fixture_root)
    reporter_tools = ToolExecutor(reg, ctx, node="reporter", tracker=tracker)

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
                    skills=inspector_skills,
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

    async def reporter(state: InspectionState, config: RunnableConfig | None = None) -> dict:
        """汇总 → 工单起草与幂等执行 → critical 审批 interrupt（单点设计，ADR-0007）→ 报告。

        interrupt 恢复时本节点从头重跑：工单因可复现账本键（run_id:reporter:wo:<device>）
        去重不重复执行（§9）；thread_id 即 run_id，由 config 传入。
        """
        run_id = (config or {}).get("configurable", {}).get("thread_id", "unknown-run")
        await ctx.emit(NewEvent(type="node.started", node="reporter", payload={"phase": "reporter"}))
        findings = [Finding.model_validate(f) for f in state.get("findings") or []]
        critical = [f for f in findings if f.level == "critical"]

        # 1) 工单起草（warning+critical），结构化校验失败退确定性兜底
        # P2：goal 只诊断 → 跳过起草（评测口径：只诊断 → create_work_order 属 forbidden），
        # notes 记录原因；非只诊断行为完全不变。
        if diagnosis_only_goal((state.get("request") or {}).get("goal")):
            orders: list[WorkOrder] = []
        else:
            orders = await _draft_work_orders(ctx, tracker, findings)
        saved_orders: list[WorkOrder] = []
        for order in orders:
            outcome = await reporter_tools.call(
                "create_work_order",
                {"device_id": order.device_id, "title": order.title, "action": order.action},
                ledger_key=f"{run_id}:reporter:wo:{order.device_id}",
            )
            if outcome.status in ("ok", "deduplicated") and outcome.result is not None:
                saved_orders.append(outcome.result.work_order)

        # 2) critical → 人工审批（唯一 interrupt 点）
        decision: dict | None = None
        if critical:
            payload = {
                "question": f"是否派无人机现场复核 {len(critical)} 台 critical 设备？",
                "devices": [f.device_id for f in critical],
                # payload 出站键统一 camelCase（ADR-0008 I7/I8 补丁；前端按 camel 读取）
                "workOrders": [o.model_dump(by_alias=True) for o in saved_orders],
                # I5 使能（主线程向后兼容补丁）：HTTP 审批端点按此 id 寻址
                "approvalId": f"{run_id}:approval:1",
            }
            await ctx.emit(NewEvent(type="approval.required", node="reporter", payload=payload))
            raw = interrupt(payload)
            decision = raw if isinstance(raw, dict) else {}
            await ctx.emit(NewEvent(type="approval.resolved", node="reporter", payload=decision))
            if decision.get("approved"):
                for f in critical:
                    await reporter_tools.call(
                        "dispatch_drone",
                        {"device_id": f.device_id, "reason": f.summary[:100], "approval_id": decision.get("approvalId", "")},
                        ledger_key=f"{run_id}:reporter:drone:{f.device_id}",
                    )

        report = _assemble_report(state, ctx, saved_orders, decision)
        await ctx.emit(
            NewEvent(
                type="node.completed",
                node="reporter",
                payload={"status": report.status, "coverage": report.coverage},
            )
        )
        return {"report": report.model_dump(), "work_orders": [o.model_dump() for o in saved_orders]}

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


async def _draft_work_orders(
    ctx: RunContext, tracker: BudgetTracker, findings: list[Finding]
) -> list[WorkOrder]:
    """warning/critical 设备起草工单：LLM 结构化输出，校验失败退确定性兜底（不编造）。"""
    targets = [f for f in findings if f.level in ("warning", "critical")]
    if not targets or ctx.cancelled():
        return []
    drafted: dict[str, tuple[str, str]] = {}
    try:
        from app.skills.base import get_skill

        # B10：起草守则以 Skill 片段注入（协议部分仍由 _ORDER_SYSTEM 提供）
        system = _ORDER_SYSTEM + "\n\n" + get_skill("work-order-drafting").system_prompt
    except Exception:
        system = _ORDER_SYSTEM  # Skill 缺失退内置守则（不阻断流程）
    try:
        model = ctx.llm()
        payload = json.dumps(
            [{"device_id": f.device_id, "level": f.level, "summary": f.summary} for f in targets],
            ensure_ascii=False,
        )
        reply = await model.ainvoke([SystemMessage(system), HumanMessage(payload)])
        if tracker.consume_tokens(usage_from_message(reply)):
            text = reply.content.strip()
            fenced = _JSON_FENCE.search(text)
            if fenced:
                text = fenced.group(1)
            data = json.loads(text)
            for item in data.get("orders", []):
                device_id = item.get("device_id")
                if device_id in {f.device_id for f in targets} and item.get("title") and item.get("action"):
                    drafted[device_id] = (str(item["title"])[:80], str(item["action"])[:200])
    except Exception:
        drafted = {}  # 起草失败 → 全部走兜底（可解释，不阻断流程）
    orders = []
    for f in targets:
        title, action = drafted.get(
            f.device_id,
            (f"{f.level}：{f.summary[:30]}", "现场检查，按异常判据规程处置并复核"),
        )
        orders.append(WorkOrder(device_id=f.device_id, title=title, action=action))
    return orders


def _assemble_report(
    state: InspectionState,
    ctx: RunContext,
    work_orders: list[WorkOrder] | None = None,
    approval: dict | None = None,
) -> InspectionReport:
    """汇总 findings/failures/工单/审批 → InspectionReport（partial 时覆盖率与原因可解释）。

    R2：coverage 分母统一为**请求的 device_ids**（single_react 同口径）——
    缩水计划（请求 N 台只计划 M<N 台）不得虚抬覆盖率；缺失设备记入 notes。
    """
    request = state.get("request") or {}
    requested: list[str] = list(request.get("device_ids") or [])
    tasks = state.get("tasks") or []
    findings = [Finding.model_validate(f) for f in state.get("findings") or []]
    failures = list(state.get("failures") or [])
    total = len(requested) if requested else len(tasks)
    covered = {f.device_id for f in findings}
    coverage = (len(covered) / total) if total else 0.0
    status = "completed" if not failures else "partial"
    notes_parts = []
    for failure in failures:
        notes_parts.append(f"{failure.get('device_id')}: {failure.get('reason')}")
    # 计划缺失的请求设备：可解释的失败项（缩水计划不静默）
    planned = {t.get("device_id") for t in tasks}
    for device_id in requested:
        if device_id not in planned:
            notes_parts.append(f"{device_id}: 未列入计划（计划缩水），未巡检")
            status = "partial"
    if ctx.cancelled():
        status = "partial"
        notes_parts.append("run 被请求取消，提前结束")
    if state.get("plan_meta", {}).get("fallback"):
        notes_parts.append("规划失败，使用默认全量检查计划")
    # P2：goal 只诊断时起草被跳过——仅当确有可起草对象（warning/critical）时记 notes
    if (
        diagnosis_only_goal(request.get("goal"))
        and any(f.level in ("warning", "critical") for f in findings)
        and not work_orders
    ):
        notes_parts.append("按 goal 只诊断，未起草工单")
    if work_orders:
        notes_parts.append(f"工单 {len(work_orders)} 份（draft，待人工确认执行）")
    if approval is not None:
        if approval.get("approved"):
            aids = approval.get("approvalId") or "-"
            notes_parts.append(f"审批通过：无人机派工已模拟执行（approval_id={aids}）")
        else:
            note = approval.get("note") or ""
            notes_parts.append(f"审批拒绝：无人机派工未执行{'（' + str(note) + '）' if note else ''}")
    return InspectionReport(
        findings=findings,
        work_orders=work_orders or [],
        status=status,
        coverage=round(coverage, 4),
        notes="；".join(notes_parts) if notes_parts else None,
    )
