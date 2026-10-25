"""X2R/X3R 真实模型消融共享机件：计量代理 / 轮次异常检查 / 报告装配（全部离线单测，不打 API）。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import json  # noqa: E402

import pytest  # noqa: E402
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage  # noqa: E402


# ---------- 计量代理 ----------

async def test_metered_model_records_real_usage_by_role() -> None:
    from evals.real_runs import MeteredLLM, RealLLMMeter

    meter = RealLLMMeter()
    replies = {
        "你是巡检规划器。": AIMessage(content='{"steps":[]}', usage_metadata={"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}),
        "你是设备巡检分析员，": AIMessage(content='{"final":{}}'),  # 无 usage → 走估算兜底
    }

    class Inner:
        async def ainvoke(self, messages, **kwargs):
            return replies[messages[0].content[:10]]

    model = MeteredLLM(Inner(), meter)
    await model.ainvoke([SystemMessage("你是巡检规划器。"), HumanMessage("x")])
    await model.ainvoke([SystemMessage("你是设备巡检分析员，负责一台设备。"), HumanMessage("y")])
    assert meter.calls == 2
    assert meter.calls_by_role == {"planner": 1, "inspector": 1}
    assert meter.input_tokens == 100  # 真实 usage 优先
    assert meter.output_tokens == 20
    assert meter.input_tokens_est > 0  # 无 usage 的调用按内容估算并单独标记
    assert meter.estimated_calls == 1
    snap = meter.snapshot()
    assert snap["calls"] == 2 and snap["estimated_calls"] == 1


def test_role_of_routes_by_system_marker() -> None:
    from evals.real_runs import role_of

    assert role_of([SystemMessage("你是巡检规划器")]) == "planner"
    assert role_of([SystemMessage("你是设备巡检分析员")]) == "inspector"
    assert role_of([SystemMessage("你是工单起草员")]) == "reporter"
    assert role_of([SystemMessage("你是巡检智能体")]) == "single"
    assert role_of([SystemMessage("其他")]) == "unknown"


# ---------- 轮次异常检查（停止规则） ----------

def test_round_anomaly_detection() -> None:
    from evals.real_runs import round_anomaly

    ok = {"aggregates": {"crashed_total": 1, "success_rate": 0.98, "cases": 50}}
    assert round_anomaly(ok) is None
    crashed = {"aggregates": {"crashed_total": 11, "success_rate": 0.78, "cases": 50}}
    assert "crash" in round_anomaly(crashed)
    broken = {"aggregates": {"crashed_total": 0, "success_rate": 0.4, "cases": 50}}
    assert "成功率" in round_anomaly(broken)


def test_usd_from_usage() -> None:
    from evals.real_runs import usd_from_usage

    assert usd_from_usage(1_000_000, 500_000, {"input_usd_per_m": 0.15, "output_usd_per_m": 0.60}) == pytest.approx(0.45)


# ---------- E1 真实版：轮次文件装配（断点续跑） ----------

def _round_file(tmp_path, config: str, rnd: int, success: float, p50: float) -> Path:
    n = 50
    path = tmp_path / f"round{rnd}-{config}.json"
    path.write_text(
        json.dumps(
            {
                "config": config,
                "round": rnd,
                "aggregates": {
                    "cases": n,
                    "success_rate": success,
                    "crashed_total": 0,
                    "tool_precision": 0.9,
                    "tool_recall": 0.8,
                    "tool_precision_goal": 0.95,
                    "tool_recall_goal": 0.99,
                    "drone_policy_ok_rate": 1.0,
                    "anomaly_recall": 1.0,
                    "anomaly_precision": 1.0,
                    "forbidden_calls_total": 0,
                    "steps_mean": 8.0,
                    "param_accuracy": 0.9,
                    "work_order_accuracy": 1.0,
                    "order_satisfaction": 0.9,
                    "evidence_ok_rate": 1.0,
                },
                "latencies": [p50] * n,
                "llm_meter": {
                    "calls": 300 + rnd,
                    "calls_by_role": {"planner": 50, "inspector": 200, "reporter": 50 + rnd},
                    "input_tokens": 100_000 + rnd,
                    "output_tokens": 10_000,
                    "input_tokens_est": 0,
                    "output_tokens_est": 0,
                    "estimated_calls": 0,
                },
                "per_case": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def test_collect_rounds_reads_all_files(tmp_path) -> None:
    from evals.real_runs import collect_rounds

    _round_file(tmp_path, "multi", 1, 1.0, 10.0)
    _round_file(tmp_path, "multi", 2, 0.98, 12.0)
    _round_file(tmp_path, "single", 1, 1.0, 20.0)
    rounds = collect_rounds(tmp_path)
    assert [r["round"] for r in rounds["multi"]] == [1, 2]
    assert len(rounds["single"]) == 1


def test_e1_real_assemble_summary_uses_offline_helpers(tmp_path) -> None:
    from evals.real_runs import collect_rounds, summarize_config

    _round_file(tmp_path, "multi", 1, 1.0, 10.0)
    _round_file(tmp_path, "multi", 2, 0.98, 12.0)
    rounds = collect_rounds(tmp_path)["multi"]
    s = summarize_config(rounds)
    assert s["success_rate"]["mean"] == pytest.approx(0.99)
    assert s["success_rate"]["min"] == pytest.approx(0.98)
    assert s["latency_ms_p50"] == pytest.approx(11.0)
    assert s["runs"] == 2
    assert s["llm"]["calls"] == 603  # 301+302


def test_e1_real_assemble_report_renders(tmp_path) -> None:
    """装配层结构测试（不打 API）：两配置各 1 轮 → e1-real-*.md 落盘且必要段齐全。"""
    from evals.e1_real import E1_REAL_DISCLAIMER, assemble_report

    rounds_dir = tmp_path / "rounds"
    rounds_dir.mkdir()
    _round_file(rounds_dir, "multi", 1, 1.0, 10.0)
    _round_file(rounds_dir, "single", 1, 1.0, 20.0)
    cases = [
        {"cid": "E01", "difficulty": "easy", "input": {"device_ids": ["INV-001"]}, "fault": None},
        {"cid": "H01", "difficulty": "hard", "input": {"device_ids": ["INV-001"] * 3}, "fault": None},
    ]
    payload = assemble_report(cases, {"easy": 1, "hard": 1}, rounds_dir, tmp_path, runs_requested=3, stopped=None)
    (md_path,) = list(tmp_path.glob("e1-real-*.md"))
    text = md_path.read_text(encoding="utf-8")
    assert payload["disclaimer"] == E1_REAL_DISCLAIMER
    assert "成功率已到上限" in text  # 两配置 100% → 上限检查文案
    assert "deepseek" in text and "0.15" in text  # 单价来源与日期段
    assert "逐轮概况" in text and "估算 vs 实际" in text
    assert "OPENAI_API_KEY" not in text  # 不回显 key


# ---------- E3 真实版：累计成本与装配 ----------

def test_cumulative_usd_sums_rounds(tmp_path) -> None:
    from evals.e3_real import collect_rounds_flat, cumulative_usd

    rounds_dir = tmp_path / "rounds"
    rounds_dir.mkdir()
    _round_file(rounds_dir, "parallel", 1, 1.0, 10.0)  # in 100_001, out 10_000
    _round_file(rounds_dir, "serial", 1, 1.0, 10.0)
    prices = {"input_usd_per_m": 0.15, "output_usd_per_m": 0.60}
    spent = cumulative_usd(collect_rounds_flat(rounds_dir), prices)
    assert spent == pytest.approx(2 * (100_001 / 1e6 * 0.15 + 10_000 / 1e6 * 0.60))


def test_e3_real_assemble_report_renders(tmp_path) -> None:
    from evals.e3_real import E3_REAL_DISCLAIMER, assemble_report

    rounds_dir = tmp_path / "rounds"
    rounds_dir.mkdir()
    _round_file(rounds_dir, "parallel", 1, 1.0, 10.0)
    _round_file(rounds_dir, "serial", 1, 1.0, 30.0)
    # 补 per_case 供分桶表（_round_file 默认 per_case 为空 → 表仍应渲染表头）
    cases = [{"cid": "E01", "difficulty": "easy", "input": {"device_ids": ["INV-001"]}, "fault": None}]
    assemble_report(cases, rounds_dir, tmp_path, runs_requested=3, budget_cap_usd=0.5, stopped=None)
    (md_path,) = list(tmp_path.glob("e3-real-*.md"))
    text = md_path.read_text(encoding="utf-8")
    assert "分设备数桶" in text and "预算熔断" in text and E3_REAL_DISCLAIMER.split("（")[0] in text
    assert "OPENAI_API_KEY" not in text
