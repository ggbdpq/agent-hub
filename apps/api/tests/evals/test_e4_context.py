"""E4 上下文隔离消融：子任务简报（现状）vs 完整历史（替身注入）。

守卫点：
1. full_history_brief 含完整遥测序列与全站清单（不收敛——对照臂为收敛简报）；
2. 进程内注入 contextmanager 可替换可还原（生产默认零改动）；
3. 两臂质量指标一致（替身不因上下文变化改变行为）、full 臂输入 Token 显著更高
   （上下文隔离的 Token 代价——E4 的核心观测量）。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402

from evals.datasets import fixtures_root  # noqa: E402
from evals.e4_context import (  # noqa: E402
    full_history_brief,
    full_history_brief_enabled,
)

DEVICE = "INV-001"
CHECKS = ["遥测比对", "异常判据核查"]


def test_full_history_brief_contains_full_series_and_site_inventory() -> None:
    from app.agents.briefs import load_brief

    brief = full_history_brief(DEVICE, CHECKS, fixture_root=fixtures_root())
    baseline = load_brief(DEVICE, CHECKS, fixture_root=fixtures_root())

    # 完整遥测序列：点数与原始 fixtures 一致（对照臂是 min/max/mean/last 摘要）
    import json

    from evals.datasets import fixtures_root as fr

    raw = json.loads((fr() / "telemetry" / f"{DEVICE}.json").read_text())
    assert len(brief["telemetry_points"]) == len(raw["points"]), "完整历史臂必须含全部采样点"
    assert "telemetry_summary" not in brief or len(brief.get("telemetry_summary", {})) <= 12

    # 全站清单：上下文不隔离形态（对照臂不含）
    assert "site_inventory" in brief and len(brief["site_inventory"]) > 1

    # checks 不截断
    assert brief["checks"] == CHECKS
    # 结构上与收敛简报的关键差异可量化（Token 代价的来源）
    assert len(json.dumps(brief, ensure_ascii=False)) > len(json.dumps(baseline, ensure_ascii=False))


def test_full_history_brief_enabled_swaps_and_restores() -> None:
    import app.agents.inspector as inspector_module

    original = inspector_module.load_brief
    with full_history_brief_enabled():
        assert inspector_module.load_brief is not original
        brief = inspector_module.load_brief(DEVICE, CHECKS, fixture_root=fixtures_root())
        assert "telemetry_points" in brief
    assert inspector_module.load_brief is original, "退出 with 后必须还原生产注入函数"


@pytest.mark.asyncio
async def test_e4_arms_same_quality_more_input_tokens() -> None:
    from evals.agent_eval import run_case
    from evals.compliant_llm import CompliantLLMFactory
    from evals.datasets import load_agent_cases
    from evals.e1_ablation import build_retriever
    from evals.e3_fanout import FanoutSimRunner

    case = next(c for c in load_agent_cases()["cases"] if c["cid"] == "M01")
    retriever, valid_chunk_ids = build_retriever()
    fixtures = fixtures_root()

    arms: dict[str, dict] = {}
    for name, injected in (("brief", False), ("full_history", True)):
        factory = CompliantLLMFactory()
        ctxmgr = full_history_brief_enabled() if injected else _null_context()
        with ctxmgr:
            outcome = await run_case(
                FanoutSimRunner(fanout_mode="parallel", fixture_root=fixtures),
                case,
                retriever=retriever,
                llm_factory=factory.build,
                valid_chunk_ids=valid_chunk_ids,
            )
        arms[name] = {"outcome": outcome, "llm": factory.snapshot()}

    # 质量一致（替身决策不依赖上下文载荷）
    b, f = arms["brief"]["outcome"]["metrics"], arms["full_history"]["outcome"]["metrics"]
    assert b["success"] == f["success"] == 1.0
    assert b["anomaly_recall"] == f["anomaly_recall"]
    # Token 代价：完整历史臂输入 Token 显著更高
    assert (
        arms["full_history"]["llm"]["input_tokens_est"]
        > arms["brief"]["llm"]["input_tokens_est"]
    ), "完整历史臂输入 Token 应高于简报臂（上下文隔离的 Token 代价）"


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
