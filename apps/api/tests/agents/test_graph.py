"""B5 Supervisor 与并行扇出：LangGraph StateGraph，失败子任务降级 partial + 覆盖率。

计划 §7.2-B5：supervisor → planner → (Send 扇出多个 inspector) → 汇总 → reporter；
每个子任务 asyncio.wait_for 超时；失败子任务记录原因并继续，最终状态 partial、
覆盖率可解释。Send 走 add_conditional_edges（U1 实测，ADR-0007）。
"""

import asyncio
import json

from langchain_core.messages import AIMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.graph import build_inspection_graph
from app.contracts.agent import InspectionRequest
from app.contracts.fakes import StaticRetriever
from app.contracts.knowledge import Chunk
from app.tools.budget import BudgetTracker
from b_testkit import FIXTURES, make_ctx, sequence_llm_factory

REQUEST = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-005", "INV-002"])

CHUNK = Chunk(
    chunk_id="doc1#异常判据#1",
    doc_id="doc1",
    title="异常判据",
    text="柜温超过 85 摄氏度立即上报并生成工单",
    source_path="corpus/doc1.md",
)


def _plan_json() -> str:
    return json.dumps(
        {
            "steps": [
                {"device_id": "INV-001", "checks": ["遥测比对"]},
                {"device_id": "INV-005", "checks": ["柜温趋势"]},
                {"device_id": "INV-002", "checks": ["遥测比对"]},
            ]
        },
        ensure_ascii=False,
    )


def _insp_script(device_id: str, level: str, summary: str, with_evidence: bool = False) -> list[AIMessage]:
    """单设备 inspector 的多轮脚本：判读 → （可选检索）→ 结论。"""
    turns = [AIMessage(content=json.dumps({"tool": "detect_anomaly", "args": {"device_id": device_id}}))]
    if with_evidence:
        turns.append(AIMessage(content=json.dumps({"tool": "search_procedures", "args": {"query": "异常判据 柜温"}})))
    final = {
        "summary": summary,
        "level": level,
        "evidence_chunk_ids": ["doc1#异常判据#1"] if with_evidence else [],
        "metrics": {},
    }
    turns.append(AIMessage(content=json.dumps({"final": final}, ensure_ascii=False)))
    return turns


def _orders_json() -> str:
    return json.dumps(
        {"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}]},
        ensure_ascii=False,
    )


def _build(**kwargs):
    """构造图与收集器；脚本默认：planner + 3 台 inspector + reporter 起草 ×2。"""
    scripts = kwargs.pop(
        "scripts",
        [
            _plan_json(),
            _insp_script("INV-001", "normal", "平稳"),
            _insp_script("INV-005", "critical", "柜温漂移", with_evidence=True),
            _insp_script("INV-002", "normal", "平稳"),
            _orders_json(),
            _orders_json(),
        ],
    )
    kwargs.setdefault("retriever", StaticRetriever([CHUNK]))
    kwargs.setdefault("checkpointer", InMemorySaver())
    checkpointer = kwargs.pop("checkpointer")
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), **kwargs)
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, checkpointer=checkpointer, fixture_root=FIXTURES)
    return graph, ctx, collector, tracker


async def test_full_flow_completed_with_coverage_one() -> None:
    """INV-005 critical → interrupt 审批（拒绝派工）→ 报告 completed、覆盖率 1。"""
    graph, ctx, collector, tracker = _build()
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t1"}})
    assert "__interrupt__" in result  # critical 触发审批（B7）

    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "approvalId": "", "note": "无需无人机"}),
        {"configurable": {"thread_id": "t1"}},
    )
    report = resumed["report"]
    assert report["status"] == "completed"
    assert report["coverage"] == 1.0
    assert len(report["findings"]) == 3
    levels = {f["device_id"]: f["level"] for f in report["findings"]}
    assert levels == {"INV-001": "normal", "INV-005": "critical", "INV-002": "normal"}
    assert any("INV-005" in wo["device_id"] for wo in report["work_orders"])
    assert "拒绝" in report["notes"]
    # 事件序列：plan.created + 各节点 started/completed + inspector 证据事件 + 审批事件
    assert "plan.created" in collector.types()
    started = [e for e in collector.of_type("node.started")]
    assert any(e.node == "supervisor" for e in started)
    assert sum(1 for e in started if e.node == "inspector") == 3
    assert collector.of_type("evidence.attached")  # INV-005 检索证据入档
    assert "approval.required" in collector.types() and "approval.resolved" in collector.types()


async def test_subtask_timeout_degrades_to_partial_with_coverage() -> None:
    """1 台超时 → 报告 partial、覆盖率 2/3、原因可读（§9 不变量）。"""
    scripts = [
        _plan_json(),
        _insp_script("INV-001", "normal", "平稳"),
        # INV-005 第一步就调 get_telemetry（被覆盖为慢 handler）→ 节点级 wait_for 超时
        [
            AIMessage(content=json.dumps({"tool": "get_telemetry", "args": {"device_id": "INV-005"}})),
            _insp_script("INV-005", "critical", "柜温漂移")[0],
        ],
        _insp_script("INV-002", "normal", "平稳"),
    ]
    from app.tools.inspection_tools import build_registry

    async def slow_handler(args):
        await asyncio.sleep(10)

    # 先建 ctx 用于注册表构造，再建图（覆盖 get_telemetry 为慢调用）
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]))
    base = build_registry(ctx, fixture_root=FIXTURES)
    slow_spec = base.get("get_telemetry").model_copy(update={"handler": slow_handler})
    registry = build_registry(ctx, fixture_root=FIXTURES, spec_overrides={"get_telemetry": slow_spec})
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(
        ctx, tracker, registry=registry, fixture_root=FIXTURES, task_timeout_s=0.2
    )
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t2"}})

    report = result["report"]
    assert report["status"] == "partial"
    assert abs(report["coverage"] - 2 / 3) < 5e-4  # 报告覆盖率保留 4 位小数
    assert "INV-005" in report["notes"] and "timeout" in report["notes"]
    assert len(report["findings"]) == 2
    # 失败子任务发 node.failed 事件
    failed = collector.of_type("node.failed")
    assert any(e.payload.get("device_id") == "INV-005" for e in failed)


async def test_starved_step_budget_still_yields_findings() -> None:
    """P1c（E1 hard 档失败形态）：共享步数预算耗尽后，确定性判读兜底出 finding。

    复现 e1-real H13/H17 形态：多台设备共享 24 步，排尾设备一步未跑即
    step_budget_exhausted → 整 run partial。修复后：有判读的设备照常出结论，
    报告 completed、覆盖率 1.0；不再因预算耗尽整体降级。
    """
    scripts = [
        _plan_json(),
        "",  # 3 台 starved inspector 各取一个模型实例（预算耗尽，不发生 ainvoke）
        "",
        "",
        _orders_json(),
        _orders_json(),
    ]
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]))
    tracker = BudgetTracker(ctx.budget)
    while tracker.consume_step():
        pass  # 模拟同 run 排头设备先耗尽全部 24 步（E1 hard 的排队形态）
    graph = build_inspection_graph(ctx, tracker, checkpointer=InMemorySaver(), fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t6"}})
    assert "__interrupt__" in result  # INV-005 判读 critical → 审批照常触发

    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "approvalId": "", "note": "无需无人机"}),
        {"configurable": {"thread_id": "t6"}},
    )
    report = resumed["report"]
    assert report["status"] == "completed"  # 不再整 run 判 partial
    assert report["coverage"] == 1.0
    levels = {f["device_id"]: f["level"] for f in report["findings"]}
    assert levels == {"INV-001": "normal", "INV-005": "critical", "INV-002": "normal"}
    assert "step_budget" not in (report["notes"] or "")  # 无失败项
    # starved inspector 也真实执行了确定性判读
    detects = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "detect_anomaly"]
    assert len(detects) == 3


def test_diagnosis_only_goal_classification_matches_eval_vocabulary() -> None:
    """P2 口径锁定：评测集 5 种 goal 文本的只诊断判定，与 Q.md 下行口径一致
    （只诊断 → create_work_order 属 forbidden）。"""
    from app.agents.graph import diagnosis_only_goal

    # 只诊断类（3 种评测文本）
    assert diagnosis_only_goal("例行巡检：核对遥测是否越限，无异常只出结论，不开工单。")
    assert diagnosis_only_goal("只诊断不生成工单：输出异常结论与依据，禁止创建工单。")
    assert diagnosis_only_goal("深度诊断：给出运行评价与规程依据，禁止创建工单。")
    # 处置/派工类（2 种评测文本）：不算只诊断
    assert not diagnosis_only_goal(
        "巡检并处置：异常设备按规程生成工单，注明设备编号、异常参数、实测值与判定依据。"
    )
    assert not diagnosis_only_goal("巡检并对异常设备安排无人机现场复核（需审批），同时按规程生成工单。")
    # 无 goal / 无显式只诊断信号 → 维持既有行为（起草工单）
    assert not diagnosis_only_goal(None)
    assert not diagnosis_only_goal("")
    assert not diagnosis_only_goal("例行巡检：遥测比对与异常判读")


async def test_diagnosis_only_goal_skips_work_order_drafting() -> None:
    """P2：goal 只诊断 → reporter 跳过工单起草，notes 记录原因；其余行为不变。"""
    request = InspectionRequest(
        site="SITE-A",
        device_ids=["INV-001", "INV-005", "INV-002"],
        goal="只诊断不生成工单：输出异常结论与依据，禁止创建工单。",
    )
    graph, ctx, collector, _ = _build()
    result = await graph.ainvoke({"request": request.model_dump()}, {"configurable": {"thread_id": "t7"}})
    assert "__interrupt__" in result  # critical 审批不受 goal 影响
    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "approvalId": "", "note": "无需无人机"}),
        {"configurable": {"thread_id": "t7"}},
    )
    report = resumed["report"]
    assert report["status"] == "completed"
    levels = {f["device_id"]: f["level"] for f in report["findings"]}
    assert levels["INV-005"] == "critical"  # 诊断结论照常产出
    assert report["work_orders"] == []  # 未起草工单
    wo_calls = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "create_work_order"]
    assert wo_calls == []  # 只诊断 → create_work_order 属 forbidden（Q 口径）
    assert "只诊断" in (report["notes"] or "") and "未起草工单" in (report["notes"] or "")
    assert "approval.required" in collector.types()  # 审批链路不变


async def test_action_goal_still_drafts_work_orders() -> None:
    """P2 反例面：处置类 goal 行为完全不变（起草工单照旧）。"""
    request = InspectionRequest(
        site="SITE-A",
        device_ids=["INV-001", "INV-005", "INV-002"],
        goal="巡检并处置：异常设备按规程生成工单，注明设备编号、异常参数、实测值与判定依据。",
    )
    graph, ctx, collector, _ = _build()
    result = await graph.ainvoke({"request": request.model_dump()}, {"configurable": {"thread_id": "t8"}})
    resumed = await graph.ainvoke(
        Command(resume={"approved": False, "approvalId": "", "note": "无需无人机"}),
        {"configurable": {"thread_id": "t8"}},
    )
    assert resumed["report"]["work_orders"]  # 处置 goal：工单照常起草
    wo_calls = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "create_work_order"]
    assert wo_calls  # 行为不变


async def test_cancelled_run_yields_partial_report() -> None:
    scripts = [
        json.dumps({"steps": [{"device_id": "INV-001", "checks": ["遥测比对"]}]}, ensure_ascii=False),
        _insp_script("INV-001", "normal", "平稳"),
    ]
    ctx, _collector = make_ctx(
        llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]), cancelled=True
    )
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": REQUEST.model_dump()}, {"configurable": {"thread_id": "t3"}})
    assert result["report"]["status"] == "partial"
    assert result["report"]["coverage"] == 0.0


async def test_planner_fallback_plan_used_on_garbage() -> None:
    """planner 三次都输出垃圾 → 兜底计划照常扇出（流程不中断）。

    用全 normal 设备（INV-001/002/003），避免确定性判读 critical 触发审批中断。
    """
    request = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-002", "INV-003"])
    scripts = ["垃圾", "垃圾", "垃圾"] + [
        _insp_script("INV-001", "normal", "平稳"),
        _insp_script("INV-002", "normal", "平稳"),
        _insp_script("INV-003", "normal", "平稳"),
    ]
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]))
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": request.model_dump()}, {"configurable": {"thread_id": "t4"}})
    assert result["plan"]["note"].startswith("默认全量检查计划")
    assert result["report"]["status"] == "completed"
    assert len(result["report"]["findings"]) == 3


async def test_shrunken_plan_coverage_uses_requested_denominator() -> None:
    """R2：coverage 分母 = 请求的 device_ids；计划缺失设备记入 notes 且状态 partial。

    缩水计划（3 台只计划 2 台）不得虚抬覆盖率到 1.0。
    """
    request = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-002", "INV-003"])
    scripts = [
        json.dumps(
            {
                "steps": [
                    {"device_id": "INV-001", "checks": ["遥测比对"]},
                    {"device_id": "INV-002", "checks": ["遥测比对"]},
                    # INV-003 缺席：缩水计划
                ]
            },
            ensure_ascii=False,
        ),
        _insp_script("INV-001", "normal", "平稳"),
        _insp_script("INV-002", "normal", "平稳"),
    ]
    ctx, collector = make_ctx(llm=sequence_llm_factory(scripts), retriever=StaticRetriever([CHUNK]))
    tracker = BudgetTracker(ctx.budget)
    graph = build_inspection_graph(ctx, tracker, fixture_root=FIXTURES)
    result = await graph.ainvoke({"request": request.model_dump()}, {"configurable": {"thread_id": "t5"}})

    report = result["report"]
    assert abs(report["coverage"] - 2 / 3) < 5e-4, f"缩水计划虚抬覆盖率: {report['coverage']}"
    assert report["status"] == "partial"
    assert "INV-003" in (report["notes"] or "") and "未列入计划" in report["notes"]
