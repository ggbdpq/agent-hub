"""X2：E1 离线消融（multi vs single，合规替身 LLM）。离线数字仅验证流程，不代表模型效果。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402


# ---------- 纯函数：成功率上限检查 / 汇总 / 费用估算 ----------

def test_ceiling_check_flags_both_full_success() -> None:
    from evals.e1_ablation import ceiling_check

    flagged = ceiling_check({"success_rate": 1.0}, {"success_rate": 1.0})
    assert flagged["at_ceiling"] is True
    assert "成功率已到上限" in flagged["note"]
    clean = ceiling_check({"success_rate": 1.0}, {"success_rate": 0.96})
    assert clean["at_ceiling"] is False


def test_summarize_runs_mean_and_range() -> None:
    from evals.e1_ablation import summarize_runs

    runs = [
        {"aggregates": {"success_rate": 1.0, "tool_recall": 0.9, "anomaly_precision": None}},
        {"aggregates": {"success_rate": 0.98, "tool_recall": 0.8, "anomaly_precision": None}},
    ]
    per_case_latencies = [[10.0, 20.0], [30.0, 40.0]]  # 两轮各 2 条
    s = summarize_runs(runs, per_case_latencies)
    assert s["success_rate"]["mean"] == pytest.approx(0.99)
    assert s["success_rate"]["min"] == pytest.approx(0.98)
    assert s["success_rate"]["max"] == pytest.approx(1.0)
    assert s["latency_ms_p50"] == pytest.approx(25.0)
    assert s["latency_ms_p95"] == pytest.approx(38.5)  # numpy 线性插值
    assert s["anomaly_precision"] is None  # 全 None 指标不虚计


def test_estimate_real_run_cost_arithmetic() -> None:
    from evals.e1_ablation import estimate_real_run_cost

    est = estimate_real_run_cost(
        per_config_calls={"multi": 100, "single": 80},
        per_config_tokens={"multi": (20000, 5000), "single": (15000, 4000)},
        runs=3,
        prices={"input_usd_per_m": 0.15, "output_usd_per_m": 0.60},
    )
    assert est["total_llm_calls"] == (100 + 80) * 3
    assert est["total_input_tokens"] == 35000 * 3
    assert est["total_output_tokens"] == 9000 * 3
    assert est["total_usd"] == pytest.approx(35000 * 3 / 1e6 * 0.15 + 9000 * 3 / 1e6 * 0.60)


# ---------- 端到端：两配置真跑（小子集，离线替身） ----------

async def test_run_config_once_scores_both_configs() -> None:
    from app.agents.runner import InspectionAgentRunner
    from app.agents.single_react import SingleReActRunner
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.retriever import HybridRetriever
    from app.knowledge.splitting import split_corpus
    from evals.datasets import corpus_root, fixtures_root, load_agent_cases
    from evals.e1_ablation import run_config_once

    chunks = split_corpus(corpus_root())
    retriever = HybridRetriever(chunks, embedder=HashingEmbedder(dim=512))
    valid_chunk_ids = frozenset(c.chunk_id for c in chunks)
    cases = [
        c for c in load_agent_cases()["cases"]
        if not c.get("fault") and c["difficulty"] == "medium"
    ][:1]

    multi = await run_config_once(
        "multi", lambda: InspectionAgentRunner(fixture_root=fixtures_root()), cases, retriever, valid_chunk_ids
    )
    single = await run_config_once(
        "single", lambda: SingleReActRunner(fixture_root=fixtures_root()), cases, retriever, valid_chunk_ids
    )
    for result in (multi, single):
        agg = result["aggregates"]
        assert agg["cases"] == 1
        assert "tool_recall_goal" in agg and "drone_policy_ok_rate" in agg
        assert result["llm"]["calls"] > 0  # 费用估算的调用数基线
    # multi 简报驱动不调 get_telemetry（process-strict 漏调、goal-oriented 计达成），
    # single 无简报必须自取遥测 → 口径差在两列召回上方向相反
    assert multi["aggregates"]["tool_recall"] < single["aggregates"]["tool_recall"]
    assert multi["aggregates"]["tool_recall_goal"] >= multi["aggregates"]["tool_recall"]
