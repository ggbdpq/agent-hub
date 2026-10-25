"""B4 巡检分析 Worker（ReAct）：每设备一个子任务，只许读工具，结论由代码校验。

计划 §7.2-B4：最多 6 步；只允许调用读工具（allowed_nodes 机制）；
结论必须引用 detect_anomaly 输出与真实 chunk_id——引用不存在的 chunk 剔除、
level 以确定性判读为准（LLM 只做解释）。
"""

import json

from langchain_core.messages import AIMessage

from app.agents.briefs import build_subtask_brief
from app.agents.inspector import run_inspection
from app.contracts.fakes import FakeLLM, StaticRetriever
from app.contracts.knowledge import Chunk
from app.tools.budget import BudgetTracker
from app.tools.inspection_tools import build_registry
from b_testkit import FIXTURES, make_ctx

CHUNK = Chunk(
    chunk_id="doc1#异常判据#1",
    doc_id="doc1",
    title="异常判据",
    text="柜温超过 85 摄氏度立即上报并生成工单",
    source_path="corpus/doc1.md",
)


def _make(**kwargs) -> tuple:
    """构造 inspector 运行环境：registry + tracker + 可注入脚本。"""
    retriever = kwargs.pop("retriever", StaticRetriever([CHUNK]))
    ctx, collector = make_ctx(retriever=retriever, **kwargs)
    registry = build_registry(ctx, fixture_root=FIXTURES)
    return ctx, collector, registry


def _act(tool: str, **args) -> AIMessage:
    return AIMessage(content=json.dumps({"tool": tool, "args": args}, ensure_ascii=False))


def _final(**kw) -> AIMessage:
    return AIMessage(content=json.dumps({"final": kw}, ensure_ascii=False))


async def test_happy_path_react_loop() -> None:
    """detect_anomaly → search_procedures → final：结论带证据、事件带节点。"""
    script = [
        _act("detect_anomaly", device_id="INV-005"),
        _act("search_procedures", query="异常判据 柜温"),
        _final(
            level="critical",
            summary="柜温漂移超阈值，需现场检查",
            evidence_chunk_ids=["doc1#异常判据#1"],
            metrics={"peak_temp": 91.0},
        ),
    ]
    ctx, collector, registry = _make(llm=FakeLLM.factory(script))
    tracker = BudgetTracker(ctx.budget)
    result = await run_inspection("INV-005", ["柜温趋势"], ctx, registry, tracker)
    assert result.failure_reason is None
    assert result.finding is not None
    assert result.finding.level == "critical"  # 与确定性判读一致
    assert result.finding.evidence_chunk_ids == ["doc1#异常判据#1"]
    assert result.finding.metrics["peak_temp"] == 91.0
    assert result.finding.metrics["anomalies"]  # 确定性判读明细并入
    assert result.steps_used == 3
    # 事件全部挂 inspector 节点，且未出现非 read 工具
    assert all(e.node == "inspector" for e in collector.events)
    tools_called = [e.payload["tool"] for e in collector.of_type("tool.called")]
    assert set(tools_called) <= {"detect_anomaly", "search_procedures", "get_telemetry", "list_devices"}


async def test_level_forced_by_code_when_model_skips_anomaly() -> None:
    """模型不调 detect_anomaly 直接下结论 → 执行器补跑判读，level 以代码为准。"""
    script = [
        _final(level="normal", summary="看起来没事", evidence_chunk_ids=[]),
    ]
    ctx, _, registry = _make(llm=FakeLLM.factory(script))
    result = await run_inspection("INV-005", ["柜温趋势"], ctx, registry, BudgetTracker(ctx.budget))
    assert result.finding is not None
    assert result.finding.level == "critical"  # 代码强制：INV-005 确定性判读 critical
    assert "anomalies" in result.finding.metrics


async def test_bogus_chunk_id_stripped() -> None:
    """引用不存在的 chunk_id → 剔除（§9：结论必须引用真实存在的证据）。"""
    script = [
        _act("detect_anomaly", device_id="INV-001"),
        _act("search_procedures", query="异常判据"),
        _final(
            level="warning",
            summary="编造证据",
            evidence_chunk_ids=["doc1#异常判据#1", "fake#chunk#999"],
        ),
    ]
    ctx, _, registry = _make(llm=FakeLLM.factory(script))
    result = await run_inspection("INV-001", ["遥测比对"], ctx, registry, BudgetTracker(ctx.budget))
    assert result.finding.evidence_chunk_ids == ["doc1#异常判据#1"]
    assert any("剔除" in e for e in result.errors)


async def test_invalid_final_json_falls_back_to_deterministic() -> None:
    """final 非法 JSON → 确定性兜底结论（summary 来自判读明细，不编造）。"""
    script = [
        _act("detect_anomaly", device_id="INV-005"),
        AIMessage(content="我结束了我瞎说的{{{"),
    ]
    ctx, _, registry = _make(llm=FakeLLM.factory(script))
    result = await run_inspection("INV-005", ["柜温趋势"], ctx, registry, BudgetTracker(ctx.budget))
    assert result.failure_reason is None
    assert result.finding.level == "critical"
    assert result.finding.summary  # 兜底摘要非空（来自判读明细）
    assert result.finding.evidence_chunk_ids == []  # 无检索证据不引用


async def test_step_budget_exhausted_spam_rescues_judgement_finding() -> None:
    """步数耗尽仍无结论 → P1c 确定性判读兜底出 finding（原语义为子任务失败）。

    模型刷同一读工具（P1b 连续重复被拒，不真实执行）→ 每设备步数上限耗尽 →
    有判读的设备照常出结论（INV-001 判读 normal），不再记子任务失败。
    """
    script = [_act("get_telemetry", device_id="INV-001")] * 7
    ctx, collector, registry = _make(llm=FakeLLM.factory(script))
    result = await run_inspection(
        "INV-001", ["遥测比对"], ctx, registry, BudgetTracker(ctx.budget), max_steps=6
    )
    assert result.finding is not None
    assert result.finding.level == "normal"  # 判读为准（确定性）
    assert result.failure_reason is None
    assert result.steps_used == 6
    denied = [e for e in collector.of_type("tool.denied") if e.payload["reason"] == "duplicate_call"]
    assert denied  # 刷屏调用被 P1b 拒绝，只真实执行了一次


async def test_starved_run_budget_rescues_finding_without_any_step() -> None:
    """P1c（E1 hard 档失败形态）：共享步数预算被同 run 其他设备耗尽，本设备
    一步未跑——判读兜底照常出 finding，不再记 step_budget_exhausted 失败。"""
    ctx, _, registry = _make(llm=FakeLLM.factory([_act("get_telemetry", device_id="INV-005")]))
    tracker = BudgetTracker(ctx.budget)
    while tracker.consume_step():
        pass  # 模拟排尾设备入场前预算已被耗尽（multi 8 台设备的排队形态）
    result = await run_inspection("INV-005", ["柜温趋势"], ctx, registry, tracker, max_steps=6)
    assert result.finding is not None
    assert result.finding.level == "critical"  # INV-005 确定性判读
    assert result.failure_reason is None
    assert result.finding.summary  # 兜底摘要来自判读明细，不编造


async def test_token_budget_exhausted_rescues_finding_too() -> None:
    """P1c：token 预算耗尽同语义（E1 single 档 H18/M17 失败形态）→ 判读兜底出 finding。"""
    from app.contracts.agent import Budget

    script = [_act("detect_anomaly", device_id="INV-005"), _act("get_telemetry", device_id="INV-005")]
    ctx, _, registry = _make(
        llm=FakeLLM.factory(script),
        budget=Budget(max_steps=24, max_tool_calls=40, max_tokens=1),
    )
    result = await run_inspection("INV-005", ["柜温趋势"], ctx, registry, BudgetTracker(ctx.budget))
    assert result.finding is not None and result.finding.level == "critical"
    assert result.failure_reason is None


async def test_rescue_still_fails_when_judgement_unavailable() -> None:
    """P1c 反例兜底：判读也不可得（工具预算耗尽）→ 仍记失败（notes 可解释）。"""
    from app.contracts.agent import Budget

    ctx, _, registry = _make(
        llm=FakeLLM.factory([_act("get_telemetry", device_id="INV-001")]),
        budget=Budget(max_tool_calls=1),
    )
    tracker = BudgetTracker(ctx.budget)
    assert tracker.allow_tool_call()[0]  # 模拟同 run 已用掉全部工具调用预算
    while tracker.consume_step():
        pass
    result = await run_inspection("INV-001", ["遥测比对"], ctx, registry, tracker, max_steps=2)
    assert result.finding is None
    assert result.failure_reason == "step_budget_exhausted"


async def test_cancelled_subtask_fails_fast() -> None:
    ctx, _, registry = _make(
        llm=FakeLLM.factory([_act("list_devices", site="SITE-A")]), cancelled=True
    )
    result = await run_inspection("INV-001", ["遥测比对"], ctx, registry, BudgetTracker(ctx.budget))
    assert result.finding is None
    assert result.failure_reason == "cancelled"


async def test_device_id_overridden_by_code() -> None:
    """模型试图读别的设备 → device_id 被执行器覆盖为本子任务设备（B6 隔离的强制点）。"""
    script = [
        _act("get_telemetry", device_id="INV-001"),  # 越界尝试：正在巡检 INV-005
        _final(level="normal", summary="x", evidence_chunk_ids=[]),
    ]
    ctx, collector, registry = _make(llm=FakeLLM.factory(script))
    result = await run_inspection("INV-005", ["柜温趋势"], ctx, registry, BudgetTracker(ctx.budget))
    assert result.failure_reason is None
    called = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "get_telemetry"]
    assert called[0].payload["args"]["device_id"] == "INV-005"


async def test_last_step_reserve_steers_model_to_conclude() -> None:
    """P1a：倒数第二步的读工具被拒（引导基于已观测数据出结论），最后一步出结论。

    复现 E1 hard 档失败形态的临界点：共享步数预算逼近耗尽时，执行器拒绝读工具
    并通过拒绝事件/观察消息把引导给到模型；模型在最后一步给出结论，不走耗尽路径。
    """
    from app.contracts.agent import Budget

    script = [
        _act("search_procedures", query="异常判据 一"),
        _act("search_procedures", query="异常判据 二"),
        _final(level="critical", summary="基于已观测判读收口", evidence_chunk_ids=[]),
    ]
    ctx, collector, registry = _make(
        llm=FakeLLM.factory(script),
        budget=Budget(max_steps=3, max_tool_calls=10, max_tokens=100_000),
    )
    result = await run_inspection(
        "INV-005", ["柜温趋势"], ctx, registry, BudgetTracker(ctx.budget), max_steps=6
    )
    # 倒数第二步的第二次检索被拒绝：检索只真实执行了一次
    searches = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "search_procedures"]
    assert len(searches) == 1
    denied = [e for e in collector.of_type("tool.denied") if e.payload["reason"] == "last_step_reserve"]
    assert denied and "结论" in denied[0].payload["detail"]
    # 引导生效：模型在最后一步给出结论（不是预算耗尽路径）
    assert result.failure_reason is None
    assert result.finding is not None
    assert result.finding.summary == "基于已观测判读收口"


async def test_consecutive_duplicate_tool_call_denied() -> None:
    """P1b：模型连续两次同工具同参数的读调用，第二次被拒并引导换工具或出结论。

    E1 hard 档浪费形态：真实模型在等待/失败恢复时会把同一读调用连发两遍，
    每一遍都白耗一步。执行器层拒绝后，观察消息把引导带回模型。
    """
    script = [
        _act("detect_anomaly", device_id="INV-005"),
        _act("detect_anomaly", device_id="INV-005"),  # 连续重复：第二次应被拒
        _final(level="critical", summary="柜温漂移维持", evidence_chunk_ids=[]),
    ]
    ctx, collector, registry = _make(llm=FakeLLM.factory(script))
    result = await run_inspection("INV-005", ["柜温趋势"], ctx, registry, BudgetTracker(ctx.budget))
    detects = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "detect_anomaly"]
    assert len(detects) == 1  # 重复调用未真实执行
    denied = [e for e in collector.of_type("tool.denied") if e.payload["reason"] == "duplicate_call"]
    assert denied and "结论" in denied[0].payload["detail"]
    assert result.failure_reason is None
    assert result.finding is not None and result.finding.level == "critical"


def test_brief_contains_only_own_device_data() -> None:
    """简报只含本设备数据摘要（B6 的基础行为）。"""
    from app.agents.briefs import build_subtask_brief
    from app.tools.devices import load_devices, load_telemetry, telemetry_summary

    devices = {d.device_id: d for d in load_devices(FIXTURES)}
    brief = build_subtask_brief(
        device_id="INV-005",
        checks=["柜温趋势"],
        device=devices["INV-005"],
        telemetry=telemetry_summary(load_telemetry("INV-005", FIXTURES)),
    )
    text = json.dumps(brief, ensure_ascii=False)
    assert "INV-005" in text
    assert "INV-001" not in text and "WT-002" not in text
