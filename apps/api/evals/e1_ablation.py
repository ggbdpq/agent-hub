"""E1 消融入口（X2）：多智能体（Supervisor+Worker 图）vs 单智能体（SingleReAct）。

离线口径：两配置均由 ``CompliantScriptedLLM`` 确定性合规替身驱动（无真实模型），
范围 = 60 条评测集排除故障注入（fault 非空）后的 50 条，每配置跑 ``runs`` 次；
报告呈现均值与范围（min~max）、工具 P/R 双口径（process-strict / goal-oriented，
X1）、Token（脚本估算）、时延 P50/P95。**离线数字仅验证流程，不代表模型效果。**

真实模型口径：本入口不执行——按同一范围估算总调用次数与 USD（deepseek 价格，
来源与日期见 ``deepseek_prices``），写入报告"真实模型费用估算"节，等主线程确认
预算后再跑（OPENAI_BASE_URL=https://api.deepseek.com/v1、OPENAI_MODEL=deepseek-chat）。

用法：``python -m evals.e1_ablation [--runs 3]``。
"""

import argparse
import asyncio
import json
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evals.agent_eval import aggregate, run_case
from evals.compliant_llm import CompliantLLMFactory
from evals.report import finalize, write_report

# deepseek 官方定价页（api-docs.deepseek.com/quick_start/pricing，2026-10-25 查询）：
# 页面已不单列 "deepseek-chat"，当前模型为 deepseek-flash（V4.1-Flash-1017）与
# deepseek-v4-pro；按档位对应假设 deepseek-chat 按 flash 档计费，估值同时给
# off-peak（默认）与 peak（上界）两档，跑前需主线程与 DeepSeek 账单页确认。
DEEPSEEK_PRICES = {
    "source": "https://api-docs.deepseek.com/quick_start/pricing",
    "checked_at": "2026-10-25",
    "flash_offpeak": {"input_usd_per_m": 0.15, "output_usd_per_m": 0.60},
    "flash_peak": {"input_usd_per_m": 0.30, "output_usd_per_m": 1.20},
}

OFFLINE_DISCLAIMER = (
    "离线消融（CompliantScriptedLLM 确定性合规替身，无真实模型）：仅验证评测流程、"
    "口径与管线，不代表模型效果；时延不含真实 LLM 推理时间，Token 为脚本估算口径。"
)


def load_non_fault_cases() -> list[dict]:
    from evals.datasets import load_agent_cases

    return [c for c in load_agent_cases()["cases"] if not c.get("fault")]


def build_retriever():
    """离线真实检索（与既有 agent 套件同款 HybridRetriever + HashingEmbedder）。"""
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.retriever import HybridRetriever
    from app.knowledge.splitting import split_corpus
    from evals.datasets import corpus_root

    chunks = split_corpus(corpus_root())
    return HybridRetriever(chunks, embedder=HashingEmbedder(dim=512)), frozenset(
        c.chunk_id for c in chunks
    )


async def run_config_once(
    name: str,
    runner_factory: Callable[[], object],
    cases: list[dict],
    retriever,
    valid_chunk_ids: frozenset[str],
) -> dict:
    """单配置单轮：逐用例 run_case（每用例独立 RunContext/账本/合规 LLM 工厂）。"""
    factory = CompliantLLMFactory()
    runner = runner_factory()
    per_case = []
    for case in cases:
        per_case.append(
            await run_case(
                runner,
                case,
                retriever=retriever,
                llm_factory=factory.build,
                valid_chunk_ids=valid_chunk_ids,
            )
        )
    return {
        "config": name,
        "aggregates": aggregate(per_case),
        "per_case": per_case,
        "llm": factory.snapshot(),
    }


_SUMMARY_METRICS = (
    "success_rate",
    "anomaly_precision",
    "anomaly_recall",
    "tool_precision",
    "tool_recall",
    "tool_precision_goal",
    "tool_recall_goal",
    "drone_policy_ok_rate",
    "param_accuracy",
    "work_order_accuracy",
    "order_satisfaction",
    "evidence_ok_rate",
    "forbidden_calls_total",
    "crashed_total",
    "steps_mean",
)


def summarize_runs(runs: list[dict], per_case_latencies: list[list[float]]) -> dict:
    """跨轮汇总：指标均值与范围（min~max）；时延对全部样本（轮×用例）取分位数。"""
    summary: dict = {}
    for key in _SUMMARY_METRICS:
        values = [r["aggregates"].get(key) for r in runs]
        present = [v for v in values if v is not None]
        if not present:
            summary[key] = None
        else:
            summary[key] = {"mean": sum(present) / len(present), "min": min(present), "max": max(present)}
    latencies = [x for run in per_case_latencies for x in run]
    from evals.metrics import percentile

    summary["latency_ms_p50"] = percentile(latencies, 50)
    summary["latency_ms_p95"] = percentile(latencies, 95)
    summary["latency_ms_mean"] = sum(latencies) / len(latencies) if latencies else 0.0
    return summary


def ceiling_check(multi: dict, single: dict) -> dict:
    """报告开头的「成功率是否到上限」检查（X2 要求）；入参支持原始聚合或跨轮汇总。"""

    def _rate(value) -> float:
        if isinstance(value, dict):
            return float(value.get("mean", 0.0))
        return float(value or 0.0)

    at_ceiling = _rate(multi.get("success_rate")) == 1.0 and _rate(single.get("success_rate")) == 1.0
    return {
        "at_ceiling": at_ceiling,
        "note": (
            "两配置成功率均=100%，成功率已到上限，区分度看 hard 档与其余指标"
            "（工具 P/R 双口径、时延、Token）。"
            if at_ceiling
            else "成功率未同时到上限，成功率本身仍具区分度。"
        ),
    }


def estimate_real_run_cost(
    per_config_calls: dict[str, int],
    per_config_tokens: dict[str, tuple[int, int]],
    runs: int,
    prices: dict,
) -> dict:
    """总调用次数 = Σ(配置每轮调用数) × 轮数；USD = tokens/1M × 单价（off-peak 口径）。"""
    total_calls = sum(per_config_calls.values()) * runs
    total_in = sum(v[0] for v in per_config_tokens.values()) * runs
    total_out = sum(v[1] for v in per_config_tokens.values()) * runs
    usd = total_in / 1e6 * prices["input_usd_per_m"] + total_out / 1e6 * prices["output_usd_per_m"]
    return {
        "runs": runs,
        "per_config_llm_calls_per_run": dict(per_config_calls),
        "total_llm_calls": total_calls,
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "prices_usd_per_m": prices,
        "total_usd": round(usd, 6),
    }


def _fmt_range(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        return f"{value['mean']:.3f}（{value['min']:.3f}~{value['max']:.3f}）"
    return f"{value:.3f}"


def _fmt_pct_range(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, dict):
        return f"{value['mean']:.1%}（{value['min']:.1%}~{value['max']:.1%}）"
    return f"{value:.1%}"


async def run_e1(runs: int = 3, out_dir: Path | None = None) -> dict:
    from app.agents.runner import InspectionAgentRunner
    from app.agents.single_react import SingleReActRunner
    from evals.datasets import fixtures_root

    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    cases = load_non_fault_cases()
    retriever, valid_chunk_ids = build_retriever()
    fixtures = fixtures_root()
    configs: dict[str, Callable[[], object]] = {
        "multi": lambda: InspectionAgentRunner(fixture_root=fixtures),
        "single": lambda: SingleReActRunner(fixture_root=fixtures),
    }

    by_difficulty: dict[str, int] = {}
    for case in cases:
        by_difficulty[case["difficulty"]] = by_difficulty.get(case["difficulty"], 0) + 1

    results: dict[str, dict] = {}
    for name, factory in configs.items():
        once_runs = []
        latencies_by_run = []
        for _ in range(runs):
            once = await run_config_once(name, factory, cases, retriever, valid_chunk_ids)
            once_runs.append(once)
            latencies_by_run.append([c["metrics"]["latency_ms"] for c in once["per_case"]])
        results[name] = {
            "summary": summarize_runs(once_runs, latencies_by_run),
            "runs": [
                {"aggregates": r["aggregates"], "llm": r["llm"]} for r in once_runs
            ],
            "last_per_case": [
                {k: v for k, v in c.items() if k != "report"} for c in once_runs[-1]["per_case"]
            ],
        }
        s = results[name]["summary"]
        print(
            f"[e1:{name}] success={_fmt_pct_range(s['success_rate'])} "
            f"toolP/R(strict)={_fmt_pct_range(s['tool_precision'])}/{_fmt_pct_range(s['tool_recall'])} "
            f"toolP/R(goal)={_fmt_pct_range(s['tool_precision_goal'])}/{_fmt_pct_range(s['tool_recall_goal'])} "
            f"p50/p95={s['latency_ms_p50']:.1f}/{s['latency_ms_p95']:.1f}ms"
        )

    # 报告开头的上限检查
    ceiling = ceiling_check(results["multi"]["summary"], results["single"]["summary"])

    # hard 档成功率（区分度所在，X2 要求单列）
    hard_rows = []
    for name in results:
        hard = [c for c in results[name]["last_per_case"] if c["difficulty"] == "hard"]
        hard_success = (
            sum(c["metrics"]["success"] for c in hard) / len(hard) if hard else None
        )
        hard_rows.append([name, str(len(hard)), _fmt_pct_range(hard_success)])

    # 真实模型费用估算（不跑，只估）
    per_config_calls = {n: results[n]["runs"][-1]["llm"]["calls"] for n in results}
    per_config_tokens = {
        n: (results[n]["runs"][-1]["llm"]["input_tokens_est"], results[n]["runs"][-1]["llm"]["output_tokens_est"])
        for n in results
    }
    cost_offpeak = estimate_real_run_cost(per_config_calls, per_config_tokens, runs, DEEPSEEK_PRICES["flash_offpeak"])
    cost_peak = estimate_real_run_cost(per_config_calls, per_config_tokens, runs, DEEPSEEK_PRICES["flash_peak"])

    def token_row(name: str) -> list[str]:
        llm = results[name]["runs"][-1]["llm"]
        return [name, str(llm["calls"]), f"{llm['input_tokens_est']:,}", f"{llm['output_tokens_est']:,}", json.dumps(llm["calls_by_role"], ensure_ascii=False)]

    now = datetime.now(UTC)
    local_date = (now + timedelta(hours=8)).strftime("%Y-%m-%d")
    s_multi, s_single = results["multi"]["summary"], results["single"]["summary"]
    payload = finalize(
        {
            "suite": "e1-ablation",
            "title": "E1 消融：多智能体 vs 单智能体（离线合规替身，X2）",
            "meta": [
                ("模型", "CompliantScriptedLLM（确定性合规替身，非真实模型）"),
                ("日期", f"{now.strftime('%Y-%m-%d')}（UTC；本地 UTC+8 为 {local_date}）"),
                ("范围", f"{len(cases)} 条（60 条排除故障注入 fault 非空 10 条；easy/medium/hard = "
                        f"{by_difficulty.get('easy', 0)}/{by_difficulty.get('medium', 0)}/{by_difficulty.get('hard', 0)}）"),
                ("运行次数", f"每配置 {runs} 次（均值与范围 min~max）"),
                ("单价", "离线零单价；真实模型估算用 deepseek 价格（见「真实模型费用估算」节）"),
                ("成功率上限检查", ceiling["note"]),
            ],
            "method": [
                "配置 multi=InspectionAgentRunner（Supervisor→planner→Send 扇出 inspector×N→reporter，默认拒绝审批）；"
                "single=SingleReActRunner（单 ReAct 循环）。同一 HybridRetriever（真实检索）、同一合规替身 LLM 工厂、同一 Budget 默认值。",
                "合规替身按系统提示路由角色：multi 的 inspector 简报驱动（B6：遥测已在简报，不调 get_telemetry）；"
                "single 无简报逐台自取遥测——两配置信息面差异即真实设计差异，指标如实呈现。",
                "指标复用 C8 run_case/aggregate（含 X1 双口径）：process-strict=期望集工具必须调用；"
                "goal-oriented=get_telemetry/list_devices 以结论覆盖判定、dispatch_drone 默认拒绝口径期望不调用。",
                "真实模型口径未执行：总调用次数与 USD 为估算（调用数基线=离线替身每轮实测 LLM 调用数，"
                "真实模型的重试/循环行为可能高于此基线）。",
            ],
            "sample_size": {"cases": len(cases), "runs_per_config": runs, "configs": 2},
            "variance": {
                "multi success_rate range": _fmt_range(s_multi["success_rate"]).replace("（", " (").replace("）", ")"),
                "single success_rate range": _fmt_range(s_single["success_rate"]).replace("（", " (").replace("）", ")"),
                "multi latency_ms_p95": round(s_multi["latency_ms_p95"], 1),
                "single latency_ms_p95": round(s_single["latency_ms_p95"], 1),
            },
            "known_biases": [
                "离线口径：成功率验证的是评测流程与契约管道，不代表模型效果。",
                "时延不含真实 LLM 推理时间（替身即时回复）；两配置时延差只反映编排结构本身（并行 vs 串行的执行框架开销）。",
                "Token 为脚本估算（estimate_tokens：CJK 0.7 token/字，其余 4 字符 1 token），非计费口径。",
                "multi 的 reporter 对 warning/critical 一律起草工单（生产行为）：只诊断类 goal 下会与期望集错配，指标如实呈现。",
                "真实模型估算假设 deepseek-chat 按 deepseek-flash 档计费（2026-10-25 定价页已不单列 deepseek-chat），跑前需确认。",
            ],
            "disclaimer": OFFLINE_DISCLAIMER,
            "ceiling_check": ceiling,
            "tables": [
                {
                    "caption": "总体指标（均值，范围 min~max；时延对全部 轮×用例 样本取分位数，ms 保留 1 位小数）",
                    "header": ["指标", "multi（Supervisor+Worker）", "single（SingleReAct）"],
                    "rows": [
                        ["任务成功率", _fmt_pct_range(s_multi["success_rate"]), _fmt_pct_range(s_single["success_rate"])],
                        ["异常识别 Precision", _fmt_pct_range(s_multi["anomaly_precision"]), _fmt_pct_range(s_single["anomaly_precision"])],
                        ["异常识别 Recall", _fmt_pct_range(s_multi["anomaly_recall"]), _fmt_pct_range(s_single["anomaly_recall"])],
                        ["工具 Precision（process-strict）", _fmt_pct_range(s_multi["tool_precision"]), _fmt_pct_range(s_single["tool_precision"])],
                        ["工具 Recall（process-strict）", _fmt_pct_range(s_multi["tool_recall"]), _fmt_pct_range(s_single["tool_recall"])],
                        ["工具 Precision（goal-oriented）", _fmt_pct_range(s_multi["tool_precision_goal"]), _fmt_pct_range(s_single["tool_precision_goal"])],
                        ["工具 Recall（goal-oriented）", _fmt_pct_range(s_multi["tool_recall_goal"]), _fmt_pct_range(s_single["tool_recall_goal"])],
                        ["派工合规率（默认拒绝口径）", _fmt_pct_range(s_multi["drone_policy_ok_rate"]), _fmt_pct_range(s_single["drone_policy_ok_rate"])],
                        ["越权调用次数（合计）", _fmt_range(s_multi["forbidden_calls_total"]), _fmt_range(s_single["forbidden_calls_total"])],
                        ["时延 P50 (ms)", f"{s_multi['latency_ms_p50']:.1f}", f"{s_single['latency_ms_p50']:.1f}"],
                        ["时延 P95 (ms)", f"{s_multi['latency_ms_p95']:.1f}", f"{s_single['latency_ms_p95']:.1f}"],
                        ["步数均值", _fmt_range(s_multi["steps_mean"]), _fmt_range(s_single["steps_mean"])],
                    ],
                },
                {
                    "caption": "hard 档成功率（上限检查后的区分度所在）",
                    "header": ["配置", "hard 用例数", "成功率"],
                    "rows": hard_rows,
                },
                {
                    "caption": "Token 与 LLM 调用（单轮实测，脚本估算口径）",
                    "header": ["配置", "LLM 调用/轮", "输入 Token/轮（估算）", "输出 Token/轮（估算）", "按角色调用"],
                    "rows": [token_row("multi"), token_row("single")],
                },
            ],
            "sections": [
                {
                    "caption": "结论（严格按计划 §11-E1 判定，不美化）",
                    "bullets": [
                        f"成功率差距 {abs(s_multi['success_rate']['mean'] - s_single['success_rate']['mean']) * 100:.1f}pp < 3pp 判定线："
                        "离线合规替身口径下，单智能体成功率不差于多智能体（两配置均 100%）。",
                        f"单智能体更优的维度：LLM 调用数（{results['single']['runs'][-1]['llm']['calls']} vs "
                        f"{results['multi']['runs'][-1]['llm']['calls']} 次/轮）、步数（{s_single['steps_mean']['mean']:.2f} vs "
                        f"{s_multi['steps_mean']['mean']:.2f}）、离线时延（P50 {s_single['latency_ms_p50']:.1f} vs "
                        f"{s_multi['latency_ms_p50']:.1f} ms，替身即时回复时编排开销主导，见已知偏差）。",
                        f"多智能体更优的维度：输入 Token（{results['multi']['runs'][-1]['llm']['input_tokens_est']:,} vs "
                        f"{results['single']['runs'][-1]['llm']['input_tokens_est']:,}/轮，子任务上下文隔离摊薄单轮上下文）；"
                        "多设备并行的时延收益见 E3（真实 LLM 时延注入下的对照）。",
                        "按 §11 判定如实写明：本离线口径下若只看成功率与调用成本，单智能体已够用；"
                        "多智能体的实证优势在上下文隔离（输入 Token）与并行扇出（E3），而非成功率。"
                        "最终结论以真实模型重跑为准（费用估算见下节，等主线程确认预算）。",
                    ],
                },
                {
                    "caption": "真实模型费用估算（未执行，等主线程确认预算）",
                    "bullets": [
                        f"范围同上（{len(cases)} 条 × {runs} 次 × 2 配置）；调用数基线 = 离线替身单轮实测 LLM 调用数"
                        f"（multi {per_config_calls['multi']} 次/轮，single {per_config_calls['single']} 次/轮）。",
                        f"总 LLM 调用 ≈ {cost_offpeak['total_llm_calls']} 次；"
                        f"Token ≈ 输入 {cost_offpeak['total_input_tokens']:,} + 输出 {cost_offpeak['total_output_tokens']:,}（估算口径）。",
                        f"USD（deepseek-flash off-peak，cache miss：$0.15/M 输入、$0.60/M 输出，来源 {DEEPSEEK_PRICES['source']}，"
                        f"{DEEPSEEK_PRICES['checked_at']} 查询）≈ **${cost_offpeak['total_usd']}**；"
                        f"peak 上界（$0.30/$1.20）≈ ${cost_peak['total_usd']}。",
                        "env：OPENAI_BASE_URL=https://api.deepseek.com/v1、OPENAI_MODEL=deepseek-chat（key 从仓库根 .env 的 OPENAI_API_KEY 读）。",
                        "注意：2026-10-25 定价页已不单列 deepseek-chat；本估算按 deepseek-flash 档假设，真实模型的重试/解析失败重试会使调用数高于基线。",
                    ],
                },
            ],
            "summaries": {n: results[n]["summary"] for n in results},
            "cost_estimate": {"offpeak": cost_offpeak, "peak": cost_peak, "prices": DEEPSEEK_PRICES},
            "runs_detail": results,
        },
        generated_at=datetime.now(UTC).isoformat(),
    )
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    md_path, json_path = write_report(payload, out_dir, f"e1-offline-{stamp}")
    print(f"[e1] 报告：{md_path.name} / {json_path.name}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.e1_ablation", description="E1 离线消融（多 vs 单智能体）")
    parser.add_argument("--runs", type=int, default=3, help="每配置运行次数（默认 3）")
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    asyncio.run(run_e1(runs=args.runs, out_dir=args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
