"""E1 真实模型消融（X2R）：多智能体 vs 单智能体——50 条非故障 × 2 配置 × 3 轮。

与离线版（e1_ablation）同结构：均值+范围、双口径工具 P/R（X1）、成功率上限检查
+ hard 档单列、时延 P50/P95（1 位小数）、Token/成本（单价来源与日期）。

真实模型差异：
- LLM = env 注入（OPENAI_BASE_URL/OPENAI_MODEL/OPENAI_API_KEY，key 不回显）；
  Token 取上游 usage（计费口径），缺失调用按内容估算并单独标记；
- 每轮完成即落盘 ``round<N>-<config>.json``（断点续跑：重跑同命令自动跳过已完成轮次）；
- 单轮异常检查（crash 率 >20% 或成功率 <50%）→ 如实记录并**停止后续轮次**
  （不烧预算掩埋问题），报告注明停止位置；
- 运行前在进程内清掉 http(s)_proxy/all_proxy（避免 urllib 走本地代理）。

用法：``python -m evals.e1_real --runs 3``（env 先行注入）。
"""

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evals.e1_ablation import _fmt_pct_range, _fmt_range
from evals.real_runs import (
    MAX_CRASH_RATE,
    MIN_SUCCESS,
    collect_rounds,
    dump_round,
    metered_env_factory,
    round_anomaly,
    summarize_config,
    usd_from_usage,
    utcnow_iso,
)

E1_REAL_DISCLAIMER = (
    "真实模型消融（deepseek-chat，env 注入）：结论仅适用于本合成评测集与本提示词/工具面；"
    "时延含真实 LLM 推理与网络往返；Token 为上游返回 usage（计费口径，缺失调用按内容估算并标记）。"
)

_PER_CASE_KEYS = (
    "success", "crashed", "tool_precision", "tool_recall", "tool_precision_goal", "tool_recall_goal",
    "anomaly_precision", "anomaly_recall", "drone_policy_ok", "forbidden_calls", "steps", "latency_ms",
)


def _compact(outcome: dict) -> dict:
    report = outcome.get("report")
    return {
        "cid": outcome.get("cid"),
        "difficulty": outcome.get("difficulty"),
        "devices": len((outcome.get("input") or {}).get("device_ids", [])),
        "metrics": {k: outcome["metrics"].get(k) for k in _PER_CASE_KEYS},
        "status": getattr(report, "status", None),
        "notes": (getattr(report, "notes", None) or "")[:200] or None,
    }


async def run_round(config: str, round_idx: int, cases: list[dict], retriever, valid_chunk_ids, fixtures) -> dict:
    from app.agents.runner import InspectionAgentRunner
    from app.agents.single_react import SingleReActRunner
    from evals.agent_eval import aggregate, run_case

    llm_factory, meter = metered_env_factory()
    runner = (
        InspectionAgentRunner(fixture_root=fixtures) if config == "multi"
        else SingleReActRunner(fixture_root=fixtures)
    )
    full_per_case: list[dict] = []
    latencies: list[float] = []
    started = time.perf_counter()
    for i, case in enumerate(cases, 1):
        t0 = time.perf_counter()
        outcome = await run_case(
            runner, case, retriever=retriever, llm_factory=llm_factory, valid_chunk_ids=valid_chunk_ids
        )
        latencies.append(outcome["metrics"]["latency_ms"])
        full_per_case.append(outcome)
        print(
            f"[e1r:{config}:R{round_idx}] {i}/{len(cases)} {case['cid']} "
            f"ok={outcome['metrics']['success']:.0f} crashed={int(outcome['crashed'])} "
            f"{time.perf_counter() - t0:.1f}s",
            flush=True,
        )
    return {
        "config": config,
        "round": round_idx,
        "generated_at": utcnow_iso(),
        "wall_s": round(time.perf_counter() - started, 1),
        "aggregates": aggregate(full_per_case),
        "latencies": latencies,
        "llm_meter": meter.snapshot(),
        "per_case": [_compact(o) for o in full_per_case],
    }


async def run_e1_real(
    runs: int = 3,
    *,
    rounds_dir: Path | None = None,
    out_dir: Path | None = None,
    max_crash_rate: float = MAX_CRASH_RATE,
    min_success: float = MIN_SUCCESS,
) -> dict:
    from evals.datasets import fixtures_root
    from evals.e1_ablation import DEEPSEEK_PRICES, build_retriever, ceiling_check, load_non_fault_cases
    from evals.metrics import percentile
    from evals.report import finalize, write_report

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY 未注入：先 export env（key 从仓库根 .env 读，不在日志回显）")
    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    rounds_dir = rounds_dir or out_dir / "e1-real-rounds"
    cases = load_non_fault_cases()
    retriever, valid_chunk_ids = build_retriever()
    fixtures = fixtures_root()
    by_difficulty: dict[str, int] = {}
    for case in cases:
        by_difficulty[case["difficulty"]] = by_difficulty.get(case["difficulty"], 0) + 1

    stopped: dict | None = None
    for round_idx in range(1, runs + 1):
        for config in ("multi", "single"):
            path = rounds_dir / f"round{round_idx}-{config}.json"
            if path.exists():
                print(f"[e1r] 跳过已完成轮次 {path.name}", flush=True)
                continue
            if stopped:
                print(f"[e1r] 已停止，跳过 {config} R{round_idx}（{stopped['reason']}）", flush=True)
                continue
            result = await run_round(config, round_idx, cases, retriever, valid_chunk_ids, fixtures)
            anomaly = round_anomaly(result, max_crash_rate=max_crash_rate, min_success=min_success)
            result["anomaly"] = anomaly
            dump_round(rounds_dir, result)
            meter = result["llm_meter"]
            print(
                f"[e1r] R{round_idx} {config}: success={result['aggregates']['success_rate']:.0%} "
                f"crashed={result['aggregates']['crashed_total']} wall={result['wall_s']:.0f}s "
                f"calls={meter['calls']} in/out={meter['input_tokens']:,}/{meter['output_tokens']:,}",
                flush=True,
            )
            if anomaly:
                stopped = {"config": config, "round": round_idx, "reason": anomaly}
                print(f"[e1r][STOP] 单轮异常，停止后续轮次：{anomaly}", flush=True)

    return assemble_report(cases, by_difficulty, rounds_dir, out_dir, runs, stopped)


def assemble_report(
    cases: list[dict],
    by_difficulty: dict[str, int],
    rounds_dir: Path,
    out_dir: Path,
    runs_requested: int,
    stopped: dict | None,
) -> dict:
    from evals.e1_ablation import DEEPSEEK_PRICES, ceiling_check
    from evals.metrics import percentile
    from evals.report import finalize, write_report

    rounds = collect_rounds(rounds_dir)
    missing = [c for c in ("multi", "single") if not rounds.get(c)]
    if missing:
        raise SystemExit(f"缺少配置的已完成轮次：{missing}（rounds_dir={rounds_dir}）")
    summaries = {c: summarize_config(rounds[c]) for c in ("multi", "single")}
    s_multi, s_single = summaries["multi"], summaries["single"]
    ceiling = ceiling_check(s_multi, s_single)

    # hard 档成功率：对该配置全部已完成轮次的 hard 用例样本取均值
    def hard_success(config: str) -> float | None:
        sample = [c for r in rounds[config] for c in r["per_case"] if c["difficulty"] == "hard"]
        return sum(c["metrics"]["success"] for c in sample) / len(sample) if sample else None

    now = datetime.now(UTC)
    price_offpeak = DEEPSEEK_PRICES["flash_offpeak"]
    price_peak = DEEPSEEK_PRICES["flash_peak"]

    def actual_usd(config: str, prices: dict) -> float:
        m = summaries[config]["llm"]
        return usd_from_usage(m["input_tokens"], m["output_tokens"], prices)

    total_calls = s_multi["llm"]["calls"] + s_single["llm"]["calls"]
    total_usd_offpeak = actual_usd("multi", price_offpeak) + actual_usd("single", price_offpeak)
    total_usd_peak = actual_usd("multi", price_peak) + actual_usd("single", price_peak)

    def per_round_rows(config: str) -> list[list[str]]:
        return [
            [
                str(p["round"]),
                f"{p['success_rate']:.0%}" if p["success_rate"] is not None else "—",
                str(p["crashed_total"]),
                f"{p['wall_s']:.0f}s" if p.get("wall_s") else "—",
                str(p["llm_calls"]),
                p.get("anomaly") or "",
            ]
            for p in summaries[config]["per_round"]
        ]

    # 格式化函数复用离线版（模块顶部已导入 _fmt_pct_range/_fmt_range）

    payload = finalize(
        {
            "suite": "e1-real",
            "title": "E1 消融：多智能体 vs 单智能体（真实模型 deepseek-chat，X2R）",
            "meta": [
                ("模型", f"{os.environ.get('OPENAI_MODEL', 'deepseek-chat')} @ {os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com/v1')}（temperature=0.2，非流式）"),
                ("日期", f"{now.strftime('%Y-%m-%d')}（UTC；本地 UTC+8 为 {(now + timedelta(hours=8)).strftime('%Y-%m-%d')}）"),
                ("范围", f"{len(cases)} 条非故障用例（easy/medium/hard = {by_difficulty.get('easy', 0)}/"
                        f"{by_difficulty.get('medium', 0)}/{by_difficulty.get('hard', 0)}）"),
                ("运行次数", f"请求 {runs_requested} 轮；实际完成 multi {s_multi['runs']} 轮 / single {s_single['runs']} 轮"
                            + (f"；**中途停止**：{stopped['reason']}（{stopped['config']} R{stopped['round']} 后）" if stopped else "")),
                ("单价", f"deepseek-flash 档 off-peak $0.15/M 入 + $0.60/M 出（peak 上界 $0.30/$1.20）；"
                        f"来源 {DEEPSEEK_PRICES['source']}，{DEEPSEEK_PRICES['checked_at']} 查询"
                        "（定价页已不单列 deepseek-chat，按 flash 档假设）"),
                ("成功率上限检查", ceiling["note"]),
            ],
            "method": [
                "配置与指标同离线版（e1-offline 报告）：multi=InspectionAgentRunner（Send 扇出，默认拒绝审批），"
                "single=SingleReActRunner；同一 HybridRetriever、同一 Budget 默认值（24 步生产口径）、同一评分（含 X1 双口径）。",
                "真实模型经 env 注入（app/agents/models.py 的 OpenAI 兼容客户端，60s 超时）；"
                "Token 取上游 usage 计费口径（缺失调用按内容估算，estimated_calls 单列）。",
                "每轮完成即落盘（断点续跑）；单轮 crash 率 >20% 或成功率 <50% 触发停止规则（不烧预算掩埋问题）。",
                "时延为 run_case 墙钟（含真实 LLM 推理与网络往返）；P50/P95 对全部 轮×用例 样本取分位数。",
            ],
            "sample_size": {
                "cases": len(cases),
                "runs_requested": runs_requested,
                "runs_completed": {"multi": s_multi["runs"], "single": s_single["runs"]},
            },
            "variance": {
                "multi success_rate range": _fmt_range(s_multi["success_rate"]),
                "single success_rate range": _fmt_range(s_single["success_rate"]),
                "multi latency_ms_p95": round(s_multi["latency_ms_p95"], 1),
                "single latency_ms_p95": round(s_single["latency_ms_p95"], 1),
            },
            "known_biases": [
                "合成评测集（60 条中的 50 条非故障），结论仅适用本语料与提示词/工具面；temperature=0.2 → 轮间波动真实存在（范围列呈现）。",
                "Token 为上游 usage 计费口径；上游未回 usage 的调用按内容估算（estimated_calls 单列，不计入计费口径）。",
                "Budget=24 步（生产默认）对 multi 共享步数预算是紧约束（8 台设备 hard 用例），真实模型步数分布可能触发 partial——如实呈现，不是评分缺陷。",
                "图节点 task_timeout=30s（生产默认）真实生效：inspector 子任务超时按 partial 计。",
                f"成本按 deepseek-flash 档假设计价（定价页 2026-10-25 已不单列 deepseek-chat，{DEEPSEEK_PRICES['checked_at']} 查询）。",
            ],
            "disclaimer": E1_REAL_DISCLAIMER,
            "ceiling_check": ceiling,
            "tables": [
                {
                    "caption": "总体指标（均值，范围 min~max；时延对全部 轮×用例 样本取分位数，ms 保留 1 位小数）",
                    "header": ["指标", "multi（Supervisor+Worker）", "single（SingleReAct）"],
                    "rows": [
                        ["任务成功率", _fmt_pct_range(s_multi["success_rate"]), _fmt_pct_range(s_single["success_rate"])],
                        ["崩溃 run 数（合计）", _fmt_range(s_multi["crashed_total"]), _fmt_range(s_single["crashed_total"])],
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
                    "caption": "hard 档成功率（上限检查后的区分度所在；全部已完成轮次样本均值）",
                    "header": ["配置", "hard 样本数（轮×用例）", "成功率"],
                    "rows": [
                        ["multi", str(sum(1 for r in rounds["multi"] for c in r["per_case"] if c["difficulty"] == "hard")),
                         _fmt_pct_range(hard_success("multi"))],
                        ["single", str(sum(1 for r in rounds["single"] for c in r["per_case"] if c["difficulty"] == "hard")),
                         _fmt_pct_range(hard_success("single"))],
                    ],
                },
                {
                    "caption": "逐轮概况（成功率 / 崩溃 / 墙钟 / LLM 调用 / 异常）",
                    "header": ["配置", "轮次", "成功率", "崩溃", "墙钟", "LLM 调用", "异常"],
                    "rows": [[c, *row] for c in ("multi", "single") for row in per_round_rows(c)],
                },
                {
                    "caption": "Token 与成本（上游 usage 计费口径；估算对照见结论节）",
                    "header": ["配置", "LLM 调用（合计）", "输入 Token", "输出 Token", "估算调用（无 usage）", "USD（off-peak）"],
                    "rows": [
                        ["multi", str(s_multi["llm"]["calls"]), f"{s_multi['llm']['input_tokens']:,}", f"{s_multi['llm']['output_tokens']:,}",
                         str(s_multi["llm"]["estimated_calls"]), f"{actual_usd('multi', price_offpeak):.4f}"],
                        ["single", str(s_single["llm"]["calls"]), f"{s_single['llm']['input_tokens']:,}", f"{s_single['llm']['output_tokens']:,}",
                         str(s_single["llm"]["estimated_calls"]), f"{actual_usd('single', price_offpeak):.4f}"],
                    ],
                },
            ],
            "sections": [
                {
                    "caption": "估算 vs 实际",
                    "bullets": [
                        f"总 LLM 调用实际 {total_calls} 次（离线估算基线 2,076 次 = 375+317/轮 × 3 轮）；"
                        f"实际 USD（off-peak 口径）= ${total_usd_offpeak:.4f}（peak 上界 ${total_usd_peak:.4f}；离线估算 $0.235）。",
                        "差异来源：真实模型每步 Token 与调用数分布 ≠ 合规替身（重试/解析失败会加调用，步数受 24 步预算截断）。",
                    ],
                },
                {
                    "caption": "与离线版的差异解读（结论以真实口径为准）",
                    "bullets": _diff_vs_offline(s_multi, s_single),
                },
            ],
            "summaries": {c: summaries[c] for c in summaries},
            "rounds_dir": str(rounds_dir),
            "stopped": stopped,
        },
        generated_at=now.isoformat(),
    )
    stamp = now.strftime("%Y%m%d-%H%M%S")
    md_path, json_path = write_report(payload, out_dir, f"e1-real-{stamp}")
    print(f"[e1r] 报告：{md_path.name} / {json_path.name}")
    print(
        f"[e1r] multi success={_fmt_pct_range(s_multi['success_rate'])} p50/p95="
        f"{s_multi['latency_ms_p50']:.1f}/{s_multi['latency_ms_p95']:.1f}ms"
    )
    print(
        f"[e1r] single success={_fmt_pct_range(s_single['success_rate'])} p50/p95="
        f"{s_single['latency_ms_p50']:.1f}/{s_single['latency_ms_p95']:.1f}ms"
    )
    return payload


def _diff_vs_offline(s_multi: dict, s_single: dict) -> list[str]:
    """与离线版（e1-offline 报告）逐维度对照；数字为真实口径，解读附判定方向。"""

    def m(key: str) -> str:
        return _fmt_pct_range(s_multi[key])

    def sg(key: str) -> str:
        return _fmt_pct_range(s_single[key])

    return [
        f"成功率：multi {m('success_rate')} vs single {sg('success_rate')}（离线两配置均 100%——真实口径以本表为准）。",
        f"双口径工具 Recall：multi strict {m('tool_recall')} / goal {m('tool_recall_goal')}；"
        f"single strict {sg('tool_recall')} / goal {sg('tool_recall_goal')}"
        "（离线：multi 54.0%/99.0%，single 85.6%/99.3%——真实模型是否自调遥测直接决定两口径差，见正文解读）。",
        f"时延：multi P50/P95 {s_multi['latency_ms_p50']:.1f}/{s_multi['latency_ms_p95']:.1f}ms vs "
        f"single {s_single['latency_ms_p50']:.1f}/{s_single['latency_ms_p95']:.1f}ms"
        "（离线替身即时回复时 multi 反而更慢；真实 LLM 时延下并行扇出是否兑现收益以本表为准）。",
        "结论判定沿用计划 §11-E1：成功率差距 <3pp 时，调用数/Token/时延决定「单智能体是否已够用」，如实采信。",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.e1_real", description="E1 真实模型消融（多 vs 单智能体）")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rounds-dir", type=Path, default=None, help="轮次文件目录（默认 reports/e1-real-rounds，断点续跑）")
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--max-crash-rate", type=float, default=MAX_CRASH_RATE)
    parser.add_argument("--min-success", type=float, default=MIN_SUCCESS)
    args = parser.parse_args(argv)
    for proxy in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(proxy, None)  # urllib 会读取代理变量；真实模型直连
    print(
        f"[e1r] model={os.environ.get('OPENAI_MODEL', 'deepseek-chat')} "
        f"base_url={os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com/v1')} "
        f"key={'***已注入***' if os.environ.get('OPENAI_API_KEY') else '缺失'}",
        flush=True,
    )
    asyncio.run(
        run_e1_real(
            args.runs, rounds_dir=args.rounds_dir, out_dir=args.out_dir,
            max_crash_rate=args.max_crash_rate, min_success=args.min_success,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
