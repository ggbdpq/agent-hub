"""B10 Skill 封装：一个 Skill = 系统提示片段 + 工具白名单 + 输出 Schema。

计划 §7.2-B10：三个 Skill（telemetry-triage / procedure-lookup / work-order-drafting）；
Worker 通过 Skill 加载能力——声明之外的工具不可用（执行器 allowed 强制）。
"""

import json

from langchain_core.messages import AIMessage

from app.agents.inspector import run_inspection
from app.contracts.fakes import FakeLLM, StaticRetriever
from app.contracts.knowledge import Chunk
from app.skills import base as skills_base
from app.skills.procedure_lookup.skill import load as load_procedure_lookup
from app.skills.telemetry_triage.skill import load as load_telemetry_triage
from app.skills.work_order_drafting.skill import load as load_work_order_drafting
from app.tools.budget import BudgetTracker
from app.tools.inspection_tools import build_registry
from b_testkit import FIXTURES, make_ctx

CHUNK = Chunk(
    chunk_id="doc1#异常判据#1",
    doc_id="doc1",
    title="异常判据",
    text="柜温超限上报",
    source_path="corpus/doc1.md",
)


def test_three_skills_parse_from_skill_md() -> None:
    triage = load_telemetry_triage()
    lookup = load_procedure_lookup()
    drafting = load_work_order_drafting()
    assert triage.name == "telemetry-triage"
    assert triage.allowed_tools == ("get_telemetry", "detect_anomaly")
    assert triage.output_schema.endswith("AnomalyResult")
    assert "判读" in triage.system_prompt

    assert lookup.allowed_tools == ("search_procedures",)
    assert "chunk_id" in lookup.system_prompt

    assert drafting.allowed_tools == ("create_work_order",)
    assert drafting.output_schema.endswith("WorkOrder")
    assert "draft" in drafting.system_prompt


def test_available_skills_registry() -> None:
    names = set(skills_base.available_skills())
    assert names == {"telemetry-triage", "procedure-lookup", "work-order-drafting"}
    # 未知 Skill 显式报错（不静默）
    import pytest

    with pytest.raises(KeyError):
        skills_base.get_skill("no-such-skill")


async def test_skill_allowlist_blocks_tools_outside_declaration() -> None:
    """Skill 声明之外的工具不可用：even if 节点权限允许。"""
    script = [
        AIMessage(content=json.dumps({"tool": "search_procedures", "args": {"query": "异常判据"}})),
        AIMessage(
            content=json.dumps(
                {"final": {"summary": "x", "level": "normal", "evidence_chunk_ids": [], "metrics": {}}}
            )
        ),
    ]
    ctx, collector = make_ctx(llm=FakeLLM.factory(script), retriever=StaticRetriever([CHUNK]))
    registry = build_registry(ctx, fixture_root=FIXTURES)
    skill = load_telemetry_triage()
    tracker = BudgetTracker(ctx.budget)
    result = await run_inspection(
        "INV-001",
        ["遥测比对"],
        ctx,
        registry,
        tracker,
        skills=[skill],
    )
    # telemetry-triage 不含 search_procedures：脚本请求它 → denied not_in_allowlist
    denied = [e for e in collector.of_type("tool.denied") if e.payload.get("reason") == "not_in_allowlist"]
    assert any(e.payload["tool"] == "search_procedures" for e in denied)
    assert result.failure_reason is None  # 流程不因拒绝而崩溃
    assert result.finding is not None


async def test_inspector_loads_skill_fragments_into_prompt() -> None:
    """Worker 通过 Skill 加载能力：系统提示包含 Skill 片段，白名单为并集。"""
    from b_testkit import recording_llm_factory

    triage = load_telemetry_triage()
    lookup = load_procedure_lookup()
    script = [
        AIMessage(content=json.dumps({"tool": "detect_anomaly", "args": {"device_id": "INV-005"}})),
        AIMessage(content=json.dumps({"tool": "search_procedures", "args": {"query": "异常判据"}})),
        AIMessage(
            content=json.dumps(
                {
                    "final": {
                        "summary": "柜温漂移",
                        "level": "critical",
                        "evidence_chunk_ids": ["doc1#异常判据#1"],
                        "metrics": {},
                    }
                },
                ensure_ascii=False,
            )
        ),
    ]
    factory, received = recording_llm_factory([script])
    ctx, collector = make_ctx(llm=factory, retriever=StaticRetriever([CHUNK]))
    registry = build_registry(ctx, fixture_root=FIXTURES)
    tracker = BudgetTracker(ctx.budget)
    result = await run_inspection(
        "INV-005", ["柜温趋势"], ctx, registry, tracker, skills=[triage, lookup]
    )
    assert result.finding is not None
    assert result.finding.evidence_chunk_ids == ["doc1#异常判据#1"]
    # 两个 Skill 的片段都进了系统提示
    system_text = str(received[0][0].content)
    assert "分诊守则" in system_text
    assert "检索守则" in system_text
    # 白名单 = 两 Skill 并集：list_devices 不在其中
    denied_tools = {e.payload["tool"] for e in collector.of_type("tool.denied")}
    assert "list_devices" not in denied_tools  # 脚本没调它，但白名单已排除（见下一条）
    allowed = set(triage.allowed_tools) | set(lookup.allowed_tools)
    assert "list_devices" not in allowed


async def test_reporter_drafting_skill_prompt_available() -> None:
    """work-order-drafting 的系统提示片段可被 reporter 起草加载（存在且含守则）。"""
    drafting = load_work_order_drafting()
    assert "工单" in drafting.system_prompt
    assert "draft" in drafting.system_prompt
