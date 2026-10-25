"""B12：InspectionAgentRunner——巡检多智能体对外唯一入口（AgentRunner 协议）。

- 编排（LangGraph 图，B5-B8）+ 工具执行器强制（B2）+ Skill 能力（B10）在此组装；
- 只经 RunContext 使用运行时能力（禁止 import app.runtime，AGENTS.md 硬规则）；
- interrupt 审批：注入 approver 的自动续跑循环（单 interrupt 点，ADR-0007）；
  默认 approver = 拒绝（安全缺省，评测/无人值守场景可解释）；
- 结束时发 metric.usage（步骤/工具次数/Token/耗时），评测框架（C8）消费。
"""

import inspect
import time
from collections.abc import Callable
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.graph import build_inspection_graph
from app.agents.planner import Plan, PlanStep  # noqa: F401  (re-export 供调用方类型引用)
from app.contracts.agent import (
    Budget,
    ChatModelFactory,
    InspectionReport,
    InspectionRequest,
    RunContext,
)
from app.contracts.events import NewEvent
from app.contracts.fakes import InMemoryRunStore
from app.contracts.knowledge import Retriever
from app.contracts.runtime import Claim, ToolLedger
from app.skills.base import Skill, get_skill
from app.tools.budget import BudgetTracker

DEFAULT_TASK_TIMEOUT_S = 30.0
DEFAULT_INSPECTOR_STEPS = 6


def _default_approver(payload: dict) -> dict:
    return {
        "approved": False,
        "approval_id": "",
        "note": "默认策略：无人工审批通道，自动拒绝（可注入 approver 覆盖）",
    }


class InspectionAgentRunner:
    """AgentRunner 实现：C 评测框架与主线程 HTTP 层的唯一编排入口。"""

    def __init__(
        self,
        *,
        checkpointer: Any | None = None,
        approver: Callable[[dict], dict] | None = None,
        fixture_root=None,
        task_timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
        inspector_max_steps: int = DEFAULT_INSPECTOR_STEPS,
        inspector_skills: list[str] | None = None,
    ) -> None:
        self._checkpointer = checkpointer
        self._approver = approver or _default_approver
        self._fixture_root = fixture_root
        self._task_timeout_s = task_timeout_s
        self._inspector_max_steps = inspector_max_steps
        # Skill 按名解析（缺失显式报错，不静默）
        self._inspector_skills: list[Skill] | None = (
            [get_skill(name) for name in inspector_skills] if inspector_skills else None
        )
        # P3：run 级规划缓存 {run_id: (request指纹, PlanResult)}——同进程崩溃续跑
        # 重认领再次驱动时规划复用，planner 不重复计费。跨进程不共享（新进程
        # 重新规划，语义与首次一致，只是重新计费）。
        self._plan_cache: dict[str, tuple[dict, Any]] = {}

    async def run(self, run_id: str, input: InspectionRequest, ctx: RunContext) -> InspectionReport:
        tracker = BudgetTracker(ctx.budget)
        started = time.monotonic()
        graph = build_inspection_graph(
            ctx,
            tracker,
            fixture_root=self._fixture_root,
            task_timeout_s=self._task_timeout_s,
            inspector_max_steps=self._inspector_max_steps,
            checkpointer=self._checkpointer if self._checkpointer is not None else InMemorySaver(),
            inspector_skills=self._inspector_skills,
            plan_cache=self._plan_cache,
        )
        config = {"configurable": {"thread_id": run_id}}  # thread_id = run_id（B8）

        # R1：生产重认领路径。若该 thread 已停在审批中断（服务端重启后 worker 重认领），
        # 必须以 Command(resume) 恢复检查点，绝不能带输入重启——否则整图从 START 重跑，
        # findings（operator.add）翻倍、planner/inspector 重复执行。
        pending = await _pending_interrupt_payload(graph, config)
        if pending is not None:
            # 审批决定仍经 approver 路由：重启后由新进程的 approver 接管一次性消费
            decision = self._approver(pending)
            # 与下方 in-process 循环的 I5 使能同一语义：异步 approver（生产
            # ApprovalHub 形态）必须 await，否则协程对象进 Command(resume)
            # 在检查点序列化时 TypeError（R-Y1，E2E 重启场景揪出）
            if inspect.isawaitable(decision):
                decision = await decision
            result = await graph.ainvoke(Command(resume=decision), config)
        else:
            result = await graph.ainvoke({"request": input.model_dump()}, config)

        # in-process interrupt 循环：本轮启动后新出现的中断逐次续跑
        guard = 0
        while "__interrupt__" in result:
            guard += 1
            if guard > 8:  # 防御：审批循环异常（恢复后仍中断）
                raise RuntimeError("审批 interrupt 循环超过 8 次，中止")
            decision = self._approver(result["__interrupt__"][0].value)
            # I5 使能（主线程向后兼容补丁）：approver 可返回 awaitable（HTTP 审批中心
            # 需异步等待人工决定；同步 approver 行为不变）
            if inspect.isawaitable(decision):
                decision = await decision
            result = await graph.ainvoke(Command(resume=decision), config)
        await ctx.emit(
            NewEvent(
                type="metric.usage",
                node="supervisor",
                payload={
                    **tracker.usage(),
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "approvals": guard,
                },
            )
        )
        report = result.get("report")
        if report is None:
            raise RuntimeError(f"编排未产出报告（run_id={run_id}）")
        return InspectionReport.model_validate(report)


async def _pending_interrupt_payload(graph, config: dict) -> dict | None:
    """检查点存在 pending interrupt 时返回其 payload（供 approver 决策），否则 None。"""
    state = await graph.aget_state(config)
    if not getattr(state, "next", None):
        return None
    for task in getattr(state, "tasks", ()) or ():
        interrupts = getattr(task, "interrupts", ()) or ()
        if interrupts:
            value = interrupts[0].value
            return value if isinstance(value, dict) else {"value": value}
    return None


def build_context_from_claim(
    store: InMemoryRunStore,
    claim: Claim,
    *,
    ledger: ToolLedger,
    retriever: Retriever,
    llm_factory: ChatModelFactory,
    budget: Budget | None = None,
) -> RunContext:
    """从 RunStore 认领凭据组装 RunContext（demo 与主线程 worker 共用的装配层）。

    emit 经 claim 的 fencing token 写入 store（ADR-0006/0008：seq 由 RunStore 分配）；
    cancelled 直读 store 的取消标记。
    """
    run_id = claim.run_id
    token = claim.fencing_token

    async def emit(event: NewEvent) -> None:
        store.append_event(run_id, token, event)

    return RunContext(
        emit=emit,
        cancelled=lambda: store.is_cancel_requested(run_id),
        budget=budget or Budget(),
        ledger=ledger,
        retriever=retriever,
        llm=llm_factory,
    )
