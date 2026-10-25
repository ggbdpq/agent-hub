"""E4 真实模型消融（第 6 轮）：子任务简报 vs 完整历史——deepseek-chat。

复用 e4_context 的臂构造（full_history_brief_enabled 进程内注入，生产零改动）与
real_runs 的计量/轮次落盘/停止规则；LLM 为 env 注入真实模型（key 不回显）。

用法：``python -m evals.e4_real --runs 3``（env 先行注入；断点续跑同 e1_real）。
"""

import argparse
import asyncio
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.e1_ablation import DEEPSEEK_PRICES, build_retriever, load_non_fault_cases
from evals.e4_context import full_history_brief_enabled
from evals.e3_fanout import FanoutSimRunner
from evals.real_runs import (
    dump_round,
    metered_env_factory,
    round_anomaly,
    usd_from_usage,
    utcnow_iso,
)

E4_REAL_DISCLAIMER = (
    "真实模型上下文消融（deepseek-chat，env 注入）：结论仅适用于本合成评测集与本提示词/工具面；"
    "「完整历史」臂为构造载荷（完整遥测序列+全站清单），Token 为上游 usage 计费口径。"
)


def _compact(outcome: dict) -> dict:
    report = outcome.get("report")
    return {
        "cid": outcome.get("cid"),
        "difficulty": outcome.get("difficulty"),
        "metrics": {k: outcome["metrics"].get(k) for k in
                    ("success", "crashed", "anomaly_precision", "anomaly_recall",
                     "tool_recall_goal", "forbidden_calls", "steps", "latency_ms",
                     "input_tokens", "output_tokens")},
        "status": getattr(report, "status", None),
    }


async def _run_round(arm: str, round_idx: int, cases, retriever, valid_ids, fixtures, build_llm) -> dict:
    from evals.agent_eval import run_case

    per_case = []
    ctxmgr = full_history_brief_enabled() if arm == "full_history" else _null_ctx()
    with ctxmgr:
        runner = FanoutSimRunner(fanout_mode="parallel", fixture_root=fixtures)
        for case in cases:
            per_case.append(
                await run_case(
                    runner, case, retriever=retriever,
                    llm_factory=build_llm, valid_chunk_ids=valid_ids,
                )
            )
    from evals.agent_eval import aggregate

    return {
        "round": round_idx,
        "config": arm,
        "per_case": [_compact(c) for c in per_case],
        "aggregates": aggregate(per_case),
    }


class _null_ctx:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def run_e4_real(runs: int = 3, *, difficulty: str | None = None,
                      rounds_dir: Path | None = None, out_dir: Path | None = None) -> dict:
    from evals.report import finalize, write_report

    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    rounds_dir = rounds_dir or out_dir / "e4-real-rounds"
    cases = load_non_fault_cases()
    if difficulty:
        cases = [c for c in cases if c["difficulty"] == difficulty]
    retriever, valid_ids = build_retriever()
    from evals.datasets import fixtures_root

    fixtures = fixtures_root()
    build_llm, meter = metered_env_factory()

    stopped: dict[str, str] | None = None
    rounds: dict[str, list[dict]] = {"brief": [], "full_history": []}
    for round_idx in range(1, runs + 1):
        for arm in ("brief", "full_history"):
            result = await _run_round(arm, round_idx, cases, retriever, valid_ids, fixtures, build_llm)
            dump_round(rounds_dir, result)
            rounds[arm].append(result)
            agg = result["aggregates"]
            print(f"[e4r] R{round_idx} {arm}: success={agg.get('success_rate', 0):.0%} "
                  f"crashed={agg.get('crashed_total', 0)} calls={meter.snapshot()['calls']:,}", flush=True)
            reason = round_anomaly(result)
            if reason:
                stopped = {"arm": arm, "round": round_idx, "reason": reason}
                print(f"[e4r] 停止规则触发：{reason}", flush=True)
                break
        if stopped:
            break

    now = datetime.now(UTC)
    snap = meter.snapshot()
    usd = usd_from_usage(snap["input_tokens"], snap["output_tokens"], DEEPSEEK_PRICES["flash_offpeak"])

    def summary(arm: str) -> dict:
        rs = rounds[arm]
        n = len(rs) or 1
        succ = [r["aggregates"].get("success_rate", 0) for r in rs]
        rec = [r["aggregates"].get("anomaly_recall") for r in rs if r["aggregates"].get("anomaly_recall") is not None]
        lats = [c["metrics"]["latency_ms"] for r in rs for c in r["per_case"]]
        from evals.metrics import percentile

        return {
            "rounds": len(rs),
            "success_mean": sum(succ) / n,
            "success_range": (min(succ), max(succ)) if succ else (0, 0),
            "anomaly_recall_mean": (sum(rec) / len(rec)) if rec else None,
            "latency_p50": percentile(lats, 50) if lats else None,
            "latency_p95": percentile(lats, 95) if lats else None,
        }

    s_brief, s_full = summary("brief"), summary("full_history")
    in_ratio = (s_full["latency_p50"] and True) and None  # placeholder, ratio computed below from meter split
    payload = finalize(
        {
            "suite": "e4-real",
            "title": "E4 消融：上下文载荷 简报 vs 完整历史（真实模型 deepseek-chat）",
            "meta": [
                ("模型", f"deepseek-chat @ {os.environ.get('OPENAI_BASE_URL', 'https://api.deepseek.com/v1')}（2026-10-25）"),
                ("样本", f"{len(cases)} 条非故障用例{'（难度子集：' + difficulty + '）' if difficulty else ''} × 2 臂"),
                ("运行次数", f"{runs} 轮请求；实际 brief {len(rounds['brief'])} / full_history {len(rounds['full_history'])}"),
                ("成本", f"实际 usage：入 {snap['input_tokens']:,} + 出 {snap['output_tokens']:,} token ≈ ${usd:.4f}"
                        f"（deepseek-flash 档 off-peak 单价，来源 api-docs.deepseek.com/quick_start/pricing 2026-10-25，未与账单核对）"),
                ("停止规则", json.dumps(stopped, ensure_ascii=False) if stopped else "未触发"),
            ],
            "method": [
                "两臂同一 FanoutSimRunner 容器（真实规划/子任务/执行器），唯一变量 = 子任务上下文载荷；",
                "full_history 臂经进程内替换 load_brief 注入完整遥测序列 + 全站清单（生产默认零改动）；",
                "LLM 为 env 注入真实模型，Token 取上游 usage（计费口径）；每轮落盘可复核。",
            ],
            "sample_size": {"cases": len(cases), "rounds_per_arm": {a: len(rounds[a]) for a in rounds}},
            "known_biases": [
                "「完整历史」臂形态是构造的（完整遥测+全站清单），与真实场景的上下文膨胀形态未必一致；",
                "成功率/召回在小样本 + 单模型口径下不可外推到其他模型或业务语料；",
                "结论仅适用于本合成评测集。",
            ],
            "disclaimer": E4_REAL_DISCLAIMER,
            "tables": [
                {
                    "caption": "总体指标（轮均值，范围 min~max；时延对全部 轮×用例 样本取分位数）",
                    "header": ["指标", "brief（简报，现状）", "full_history（完整历史）"],
                    "rows": [
                        ["成功率", f"{s_brief['success_mean']:.1%}（{s_brief['success_range'][0]:.0%}~{s_brief['success_range'][1]:.0%}）",
                         f"{s_full['success_mean']:.1%}（{s_full['success_range'][0]:.0%}~{s_full['success_range'][1]:.0%}）"],
                        ["异常识别 Recall",
                         f"{s_brief['anomaly_recall_mean']:.1%}" if s_brief["anomaly_recall_mean"] is not None else "-",
                         f"{s_full['anomaly_recall_mean']:.1%}" if s_full["anomaly_recall_mean"] is not None else "-"],
                        ["时延 P50 (ms)", f"{s_brief['latency_p50']:.1f}", f"{s_full['latency_p50']:.1f}"],
                        ["时延 P95 (ms)", f"{s_brief['latency_p95']:.1f}", f"{s_full['latency_p95']:.1f}"],
                    ],
                },
                {
                    "caption": "计量（全部轮次合计，上游 usage 计费口径）",
                    "header": ["指标", "数值"],
                    "rows": [
                        ["LLM 调用次数", f"{snap['calls']:,}"],
                        ["输入 / 输出 Token", f"{snap['input_tokens']:,} / {snap['output_tokens']:,}"],
                        ["估算成本 (USD, off-peak)", f"{usd:.4f}"],
                    ],
                },
            ],
            "summaries": {a: summary(a) for a in rounds},
            "rounds_dir": str(rounds_dir),
            "stopped": stopped,
        },
        generated_at=now.isoformat(),
    )
    md, js = write_report(payload, out_dir, prefix="e4-real")
    print(f"[e4r] 报告：{Path(md).name} / {Path(js).name}", flush=True)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.e4_real", description="E4 真实模型消融（简报 vs 完整历史）")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--difficulty", choices=["hard"], default=None)
    parser.add_argument("--rounds-dir", type=Path, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    for proxy in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.pop(proxy, None)
    print(f"[e4r] model={os.environ.get('OPENAI_MODEL')} key={'***已注入***' if os.environ.get('OPENAI_API_KEY') else '缺失'}", flush=True)
    asyncio.run(run_e4_real(args.runs, difficulty=args.difficulty, rounds_dir=args.rounds_dir, out_dir=args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
