"""E3 真实模型消融（X3R）：Send 并行扇出 vs 逐设备串行——时延 P50/P95（multi 口径）。

与离线版（e3_fanout）同容器（FanoutSimRunner：真实 make_plan + 真实 run_inspection，
唯一消融变量=设备子任务调度方式），LLM 换 env 注入的真实模型——真实时延才是有效结论。

预算熔断：每轮完成即累计 USD（off-peak 口径），超过 ``--budget-cap``（默认 $0.5）
→ 停止后续轮次，出初步报告（等主线程确认后续）。断点续跑同 e1_real。

用法：``python -m evals.e3_real --runs 3 --budget-cap 0.5``（env 先行注入）。
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evals.e1_ablation import _fmt_pct_range
from evals.real_runs import (
    collect_rounds,
    dump_round,
    metered_env_factory,
    round_anomaly,
    summarize_config,
    usd_from_usage,
    utcnow_iso,
)

DEFAULT_BUDGET_CAP_USD = 0.5

E3_REAL_DISCLAIMER = (
    "真实模型扇出消融（deepseek-chat，env 注入）：时延含真实 LLM 推理与网络往返，"
    "结论仅适用于本合成评测集与本提示词/工具面；Token 为上游 usage（计费口径）。"
)


async def run_round(mode: str, round_idx: int, cases: list[dict], retriever, valid_chunk_ids, fixtures) -> dict:
    from evals.agent_eval import aggregate, run_case
    from evals.e3_fanout import FanoutSimRunner

    llm_factory, meter = metered_env_factory()
    runner = FanoutSimRunner(fanout_mode=mode, fixture_root=fixtures)
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
            f"[e3r:{mode}:R{round_idx}] {i}/{len(cases)} {case['cid']} "
            f"ok={outcome['metrics']['success']:.0f} {time.perf_counter() - t0:.1f}s",
            flush=True,
        )
    from evals.e1_real import _compact

    return {
        "config": mode,
        "round": round_idx,
        "generated_at": utcnow_iso(),
        "wall_s": round(time.perf_counter() - started, 1),
        "aggregates": aggregate(full_per_case),
        "latencies": latencies,
        "llm_meter": meter.snapshot(),
        "per_case": [_compact(o) for o in full_per_case],
    }


def cumulative_usd(rounds: list[dict], prices: dict) -> float:
    return sum(
        usd_from_usage(r["llm_meter"]["input_tokens"], r["llm_meter"]["output_tokens"], prices)
        for r in rounds
    )


async def run_e3_real(
    runs: int = 3,
    *,
    rounds_dir: Path | None = None,
    out_dir: Path | None = None,
    budget_cap_usd: float = DEFAULT_BUDGET_CAP_USD,
) -> dict:
    from evals.datasets import fixtures_root
    from evals.e1_ablation import DEEPSEEK_PRICES, build_retriever, load_non_fault_cases

    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY 未注入：先 export env（key 不在日志回显）")
    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    rounds_dir = rounds_dir or out_dir / "e3-real-rounds"
    cases = load_non_fault_cases()
    retriever, valid_chunk_ids = build_retriever()
    fixtures = fixtures_root()
    prices = DEEPSEEK_PRICES["flash_offpeak"]

    stopped: dict | None = None
    for round_idx in range(1, runs + 1):
        for mode in ("parallel", "serial"):
            path = rounds_dir / f"round{round_idx}-{mode}.json"
            if path.exists():
                print(f"[e3r] 跳过已完成轮次 {path.name}", flush=True)
                continue
            if stopped:
                print(f"[e3r] 已停止，跳过 {mode} R{round_idx}（{stopped['reason']}）", flush=True)
                continue
            result = await run_round(mode, round_idx, cases, retriever, valid_chunk_ids, fixtures)
            anomaly = round_anomaly(result)
            result["anomaly"] = anomaly
            dump_round(rounds_dir, result)
            spent = cumulative_usd(collect_rounds_flat(rounds_dir), prices)
            print(
                f"[e3r] R{round_idx} {mode}: success={result['aggregates']['success_rate']:.0%} "
                f"wall={result['wall_s']:.0f}s calls={result['llm_meter']['calls']} 累计=${spent:.4f}/{budget_cap_usd}",
                flush=True,
            )
            if anomaly:
                stopped = {"mode": mode, "round": round_idx, "reason": anomaly}
                print(f"[e3r][STOP] 单轮异常，停止后续轮次：{anomaly}", flush=True)
            elif spent > budget_cap_usd:
                stopped = {"mode": mode, "round": round_idx, "reason": f"预算熔断：累计 ${spent:.4f} > ${budget_cap_usd}"}
                print(f"[e3r][STOP] {stopped['reason']}——出初步报告，等主线程确认", flush=True)

    return assemble_report(cases, rounds_dir, out_dir, runs, budget_cap_usd, stopped)


def collect_rounds_flat(rounds_dir: Path) -> list[dict]:
    return [r for rounds in collect_rounds(rounds_dir).values() for r in rounds]


def assemble_report(
    cases: list[dict],
    rounds_dir: Path,
    out_dir: Path,
    runs_requested: int,
    budget_cap_usd: float,
    stopped: dict | None,
) -> dict:
    from evals.e1_ablation import DEEPSEEK_PRICES
    from evals.e3_fanout import device_bucket
    from evals.metrics import percentile
    from evals.report import finalize, write_report

    rounds = collect_rounds(rounds_dir)
    missing = [c for c in ("parallel", "serial") if not rounds.get(c)]
    if missing:
        raise SystemExit(f"缺少模式的已完成轮次：{missing}（rounds_dir={rounds_dir}）")
    summaries = {m: summarize_config(rounds[m]) for m in ("parallel", "serial")}
    par, ser = summaries["parallel"], summaries["serial"]
    price_offpeak = DEEPSEEK_PRICES["flash_offpeak"]
    price_peak = DEEPSEEK_PRICES["flash_peak"]
    now = datetime.now(UTC)

    def records(mode: str) -> list[dict]:
        return [c for r in rounds[mode] for c in r["per_case"]]

    def bucket_rows() -> list[list[str]]:
        rows = []
        for bucket in ("1 台", "2-3 台", "4-5 台", "6+ 台"):
            p = [c["metrics"]["latency_ms"] for c in records("parallel") if device_bucket(c.get("devices", 1)) == bucket]
            s = [c["metrics"]["latency_ms"] for c in records("serial") if device_bucket(c.get("devices", 1)) == bucket]
            if not p and not s:
                continue
            rows.append(
                [
                    bucket,
                    str(len(p)),
                    f"{percentile(p, 50):.1f}" if p else "—",
                    f"{percentile(p, 95):.1f}" if p else "—",
                    f"{percentile(s, 50):.1f}" if s else "—",
                    f"{percentile(s, 95):.1f}" if s else "—",
                ]
            )
        return rows

    total_usd = usd_from_usage(
        par["llm"]["input_tokens"] + ser["llm"]["input_tokens"],
        par["llm"]["output_tokens"] + ser["llm"]["output_tokens"],
        price_offpeak,
    )
    succ_par = next((p["success_rate"] for p in par["per_round"] if p["success_rate"] is not None), None)
    succ_ser = next((p["success_rate"] for p in ser["per_round"] if p["success_rate"] is not None), None)
    mean_succ_par = _mean([p["success_rate"] for p in par["per_round"] if p["success_rate"] is not None])
    mean_succ_ser = _mean([p["success_rate"] for p in ser["per_round"] if p["success_rate"] is not None])

    payload = finalize(
        {
            "suite": "e3-real",
            "title": "E3 消融：Send 并行扇出 vs 逐设备串行（真实模型 deepseek-chat，X3R）",
            "meta": [
                ("模型", f"{os.environ.get('OPENAI_MODEL', 'deepseek-chat')} @ {os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com/v1')}（temperature=0.2，非流式）"),
                ("日期", f"{now.strftime('%Y-%m-%d')}（UTC；本地 UTC+8 为 {(now + timedelta(hours=8)).strftime('%Y-%m-%d')}）"),
                ("范围", f"{len(cases)} 条非故障用例；请求 {runs_requested} 轮，实际完成 parallel {par['runs']} 轮 / serial {ser['runs']} 轮"
                            + (f"；**中途停止**：{stopped['reason']}（{stopped.get('mode', stopped.get('config'))} R{stopped['round']} 后）" if stopped else "")),
                ("消融变量", "仅设备子任务调度方式（asyncio.gather 并行 vs 逐台 await 串行）；规划/子任务/执行器同一份生产代码（FanoutSimRunner，multi 口径无 reporter 阶段）"),
                ("单价", f"deepseek-flash 档 off-peak $0.15/M 入 + $0.60/M 出；来源 {DEEPSEEK_PRICES['source']}，{DEEPSEEK_PRICES['checked_at']} 查询（deepseek-chat 按 flash 档假设）"),
            ],
            "method": [
                "与离线版同容器（FanoutSimRunner）：真实 make_plan + 真实 run_inspection + 真实 ToolExecutor；"
                "LLM 为 env 注入真实模型（60s 超时/调用，非流式）。",
                "时延为 run_case 墙钟（真实 LLM 推理 + 网络往返）；P50/P95 对全部 轮×用例 样本取分位数，ms 1 位小数。",
                "预算熔断：每轮落盘后累计 USD（off-peak 口径）> $%.2f 即停止出初步报告。" % budget_cap_usd,
                "断点续跑：轮次文件落盘后中断可重跑同命令跳过已完成轮次。",
            ],
            "sample_size": {
                "cases": len(cases),
                "runs_requested": runs_requested,
                "runs_completed": {"parallel": par["runs"], "serial": ser["runs"]},
            },
            "variance": {
                "parallel latency_ms_p50": round(par["latency_ms_p50"], 1),
                "serial latency_ms_p50": round(ser["latency_ms_p50"], 1),
                "parallel latency_ms_p95": round(par["latency_ms_p95"], 1),
                "serial latency_ms_p95": round(ser["latency_ms_p95"], 1),
            },
            "known_biases": [
                "真实时延含 deepseek 网络往返（本机直连，代理已 unset）；单次调用波动会放大 P95。",
                "容器无 reporter 起草阶段（两模式相同常数项不进对照）；总调用数低于 E1 multi 口径。",
                "FanoutSimRunner 为 evals 侧容器（生产图为 Send 扇出，语义同 parallel 模式）；serial 模式不存在于生产——它是消融基线。",
                "temperature=0.2 → 轮间波动真实存在；成功率两模式应一致（调度不是结论变量），差异即实现缺陷。",
            ],
            "disclaimer": E3_REAL_DISCLAIMER,
            "tables": [
                {
                    "caption": "总体时延（全部 轮×用例 样本；ms，1 位小数）",
                    "header": ["模式", "样本数", "成功率（轮均值）", "P50 (ms)", "P95 (ms)", "LLM 调用（合计）"],
                    "rows": [
                        ["parallel（镜像 Send 扇出）", str(len(records("parallel"))), f"{mean_succ_par:.1%}" if mean_succ_par is not None else "—",
                         f"{par['latency_ms_p50']:.1f}", f"{par['latency_ms_p95']:.1f}", str(par["llm"]["calls"])],
                        ["serial（逐设备串行）", str(len(records("serial"))), f"{mean_succ_ser:.1%}" if mean_succ_ser is not None else "—",
                         f"{ser['latency_ms_p50']:.1f}", f"{ser['latency_ms_p95']:.1f}", str(ser["llm"]["calls"])],
                    ],
                },
                {"caption": "分设备数桶 P50/P95（并行收益随设备数增长）", "header": ["设备数", "样本数", "并行 P50", "并行 P95", "串行 P50", "串行 P95"], "rows": bucket_rows()},
                {
                    "caption": "逐轮概况",
                    "header": ["模式", "轮次", "成功率", "崩溃", "墙钟", "LLM 调用", "异常"],
                    "rows": [
                        [m, *[str(p["round"]), f"{p['success_rate']:.0%}" if p["success_rate"] is not None else "—", str(p["crashed_total"]),
                              f"{p['wall_s']:.0f}s" if p.get("wall_s") else "—", str(p["llm_calls"]), p.get("anomaly") or ""]]
                        for m in ("parallel", "serial")
                        for p in summaries[m]["per_round"]
                    ],
                },
            ],
            "sections": [
                {
                    "caption": "结论（计划 §11-E3 判定，真实时延口径）",
                    "bullets": [
                        f"并行 P50={par['latency_ms_p50']:.1f}ms vs 串行 P50={ser['latency_ms_p50']:.1f}ms"
                        + (f"（{par['latency_ms_p50'] / ser['latency_ms_p50']:.0%}）" if ser["latency_ms_p50"] else ""),
                        f"并行 P95={par['latency_ms_p95']:.1f}ms vs 串行 P95={ser['latency_ms_p95']:.1f}ms——"
                        "真实 LLM 时延下并行是否显著降低时延以本表为准（离线版为注入时延的结构验证）。",
                        "分桶明细：单台用例无并发对象（两模式应接近），收益集中在 2 台以上用例。",
                        f"成本（off-peak 口径）：合计 ${total_usd:.4f}（估算区间 $0.13~0.20；"
                        f"parallel {par['llm']['calls']} + serial {ser['llm']['calls']} 次调用）。",
                    ],
                },
            ],
            "summaries": {m: summaries[m] for m in summaries},
            "rounds_dir": str(rounds_dir),
            "stopped": stopped,
        },
        generated_at=now.isoformat(),
    )
    stamp = now.strftime("%Y%m%d-%H%M%S")
    md_path, json_path = write_report(payload, out_dir, f"e3-real-{stamp}")
    print(f"[e3r] 报告：{md_path.name} / {json_path.name}")
    return payload


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.e3_real", description="E3 真实模型扇出消融（并行 vs 串行）")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--rounds-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument("--budget-cap", type=float, default=DEFAULT_BUDGET_CAP_USD)
    args = parser.parse_args(argv)
    for proxy in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(proxy, None)
    print(
        f"[e3r] model={os.environ.get('OPENAI_MODEL', 'deepseek-chat')} "
        f"base_url={os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com/v1')} "
        f"key={'***已注入***' if os.environ.get('OPENAI_API_KEY') else '缺失'}",
        flush=True,
    )
    asyncio.run(run_e3_real(args.runs, rounds_dir=args.rounds_dir, out_dir=args.out_dir, budget_cap_usd=args.budget_cap))
    return 0


if __name__ == "__main__":
    sys.exit(main())
