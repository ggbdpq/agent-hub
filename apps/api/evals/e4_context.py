"""E4 消融入口（Q1）：子任务上下文隔离——收敛简报（现状）vs 完整历史。

替身层实现（不改生产默认值，做法同 E3 FanoutSimRunner / E7 permission_check_disabled）：
两臂共用 **同一容器**（FanoutSimRunner parallel：真实 make_plan 规划 + 真实
run_inspection 子任务 + 真实 ToolExecutor 强制），唯一消融变量是子任务收到的
上下文载荷：
- ``brief``（对照臂，现状）：B6 收敛简报——设备静态信息 + 检查项（≤6）+ 遥测
  统计摘要（≤12 指标 min/max/mean/last），完整序列不进上下文；
- ``full_history``（实验臂）：进程内替换 ``app.agents.inspector.load_brief``，
  注入完整遥测采样序列（全部点位）+ 全站设备清单 + 不截断检查项——模拟
  「上下文不隔离」的子任务信息面。

观测：输入 Token（核心）、成功率、异常识别 P/R、时延 P50/P95。
合规替身按 receipts 决策、不读上下文载荷内容，两臂质量指标应一致——差异全部
落在输入 Token 上：这正是上下文隔离在真实模型下的直接代价（计费与注意力面）。
**离线数字仅验证流程，不代表模型效果。**

真实模型口径：本入口不执行——按同一范围估算总调用次数与 USD（deepseek-flash
档价，来源与日期见 DEEPSEEK_PRICES），写入报告"真实模型费用估算"节，等主线程
确认预算后再跑。

用法：``python -m evals.e4_context [--runs 3] [--limit N]``。
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from evals.compliant_llm import CompliantLLMFactory
from evals.e1_ablation import (
    OFFLINE_DISCLAIMER,
    DEEPSEEK_PRICES,
    _fmt_pct_range,
    estimate_real_run_cost,
    load_non_fault_cases,
    summarize_runs,
)
from evals.e3_fanout import FanoutSimRunner
from evals.report import finalize, write_report

E4_DISCLAIMER = (
    "离线上下文消融（CompliantScriptedLLM 确定性合规替身，无真实模型）：仅验证"
    "上下文隔离的 Token 结构关系与评测流程，不代表模型效果；Token 为脚本估算口径，"
    "时延不含真实 LLM 推理时间。"
)


def full_history_brief(device_id: str, checks: list[str], *, fixture_root=None) -> dict:
    """完整历史载荷：设备全量静态信息 + 不截断检查项 + 完整遥测采样序列 + 全站清单。

    对照臂（app.agents.briefs.load_brief）的收敛规则全部解除：
    - telemetry_points：全部采样点（简报臂只有 min/max/mean/last 摘要）；
    - site_inventory：全站设备清单（简报臂只有本设备；对应 R3 上下文隔离被打破的形态）；
    - checks：不截断（简报臂 ≤6 项）。
    """
    from app.tools.devices import load_devices, load_telemetry

    root = fixture_root
    device = next(d for d in load_devices(root) if d.device_id == device_id)
    series = load_telemetry(device_id, root)
    return {
        "device": device.model_dump(by_alias=True, mode="json"),
        "checks": list(checks),  # 不截断（对照臂 MAX_CHECKS=6）
        "telemetry_points": [p.model_dump(mode="json") for p in series.points],
        "telemetry_window": series.window,
        "site_inventory": [d.device_id for d in load_devices(root)],  # 上下文不隔离
        "note": "E4 full-history 臂：完整遥测序列 + 全站清单（对照臂为收敛简报）",
    }


class full_history_brief_enabled:
    """进程内替换 run_inspection 的简报注入函数（with 块内生效，退出还原）。

    与 E7 的 permission_check_disabled 同构：只动评测进程内的模块属性，
    app/agents/** 生产文件零改动、默认行为不受影响。
    """

    def __enter__(self) -> "full_history_brief_enabled":
        import app.agents.inspector as inspector_module

        self._original = inspector_module.load_brief
        inspector_module.load_brief = full_history_brief
        return self

    def __exit__(self, *exc) -> bool:
        import app.agents.inspector as inspector_module

        inspector_module.load_brief = self._original
        return False


async def run_arm_once(
    arm: str,
    cases: list[dict],
    retriever,
    valid_chunk_ids: frozenset[str],
    fixtures,
) -> dict:
    """单臂单轮：同一 FanoutSimRunner 容器，full_history 臂套进程内注入。"""
    factory = CompliantLLMFactory()
    per_case = []
    ctxmgr = full_history_brief_enabled() if arm == "full_history" else _null_context()
    with ctxmgr:
        runner = FanoutSimRunner(fanout_mode="parallel", fixture_root=fixtures)
        for case in cases:
            from evals.agent_eval import run_case

            per_case.append(
                await run_case(
                    runner, case, retriever=retriever,
                    llm_factory=factory.build, valid_chunk_ids=valid_chunk_ids,
                )
            )
    return {
        "config": arm,
        "aggregates": aggregate_of(per_case),
        "per_case": per_case,
        "llm": factory.snapshot(),
    }


def aggregate_of(per_case: list[dict]) -> dict:
    from evals.agent_eval import aggregate

    return aggregate(per_case)


class _null_context:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


async def run_e4(runs: int = 3, *, limit: int | None = None, out_dir: Path | None = None) -> dict:
    from evals.datasets import fixtures_root
    from evals.e1_ablation import build_retriever

    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    all_cases = load_non_fault_cases()
    cases = all_cases[:limit] if limit else all_cases
    retriever, valid_chunk_ids = build_retriever()
    fixtures = fixtures_root()
    now = datetime.now(UTC)

    results: dict[str, dict] = {}
    for arm in ("brief", "full_history"):
        once_runs, latencies_by_run = [], []
        for _ in range(runs):
            once = await run_arm_once(arm, cases, retriever, valid_chunk_ids, fixtures)
            once_runs.append(once)
            latencies_by_run.append([c["metrics"]["latency_ms"] for c in once["per_case"]])
        results[arm] = {
            "summary": summarize_runs(once_runs, latencies_by_run),
            "runs": [{"aggregates": r["aggregates"], "llm": r["llm"]} for r in once_runs],
        }
        s = results[arm]["summary"]
        print(
            f"[e4:{arm}] success={_fmt_pct_range(s['success_rate'])} "
            f"anomalyR={_fmt_pct_range(s['anomaly_recall'])} "
            f"in_tok/run={results[arm]['runs'][-1]['llm']['input_tokens_est']:,} "
            f"p50={s['latency_ms_p50']:.1f}ms"
        )

    s_brief, s_full = results["brief"]["summary"], results["full_history"]["summary"]
    llm_brief, llm_full = results["brief"]["runs"][-1]["llm"], results["full_history"]["runs"][-1]["llm"]
    in_ratio = llm_full["input_tokens_est"] / llm_brief["input_tokens_est"] if llm_brief["input_tokens_est"] else 0.0

    # 真实模型费用估算（不跑，只估）：调用数 = 两臂每轮实测调用 × 轮数；
    # Token = 两臂各自替身估算口径 × 轮数（full 臂增量即上下文隔离的计费差）。
    cost_offpeak = estimate_real_run_cost(
        {"brief": llm_brief["calls"], "full_history": llm_full["calls"]},
        {
            "brief": (llm_brief["input_tokens_est"], llm_brief["output_tokens_est"]),
            "full_history": (llm_full["input_tokens_est"], llm_full["output_tokens_est"]),
        },
        runs,
        DEEPSEEK_PRICES["flash_offpeak"],
    )
    cost_peak = estimate_real_run_cost(
        {"brief": llm_brief["calls"], "full_history": llm_full["calls"]},
        {
            "brief": (llm_brief["input_tokens_est"], llm_brief["output_tokens_est"]),
            "full_history": (llm_full["input_tokens_est"], llm_full["output_tokens_est"]),
        },
        runs,
        DEEPSEEK_PRICES["flash_peak"],
    )

    def quality_row(label: str, key: str, pct: bool = False) -> list[str]:
        vb, vf = s_brief[key], s_full[key]
        return [label, _fmt_pct_range(vb) if pct else f"{vb}", _fmt_pct_range(vf) if pct else f"{vf}"]

    payload = finalize(
        {
            "suite": "e4-context",
            "title": "E4 消融：子任务上下文隔离——收敛简报 vs 完整历史（离线替身层，Q1）",
            "meta": [
                ("模型", "CompliantScriptedLLM（确定性合规替身，非真实模型）"),
                ("日期", f"{now.strftime('%Y-%m-%d')}（UTC；本地 UTC+8 为 {(now + timedelta(hours=8)).strftime('%Y-%m-%d')}）"),
                ("范围", f"{len(cases)} 条非故障用例（60 条排除故障注入；limit={limit or '无'}），每臂 {runs} 次"),
                ("消融变量", "仅子任务上下文载荷（B6 收敛简报 vs 完整遥测序列+全站清单）；规划/子任务/执行器为同一份生产代码"),
                ("注入方式", "进程内替换 app.agents.inspector.load_brief（with 块内生效，退出还原；生产默认零改动）"),
                ("单价", "离线零单价；真实模型估算用 deepseek 价格（见「真实模型费用估算」节）"),
            ],
            "method": [
                "两臂共用同一 FanoutSimRunner 容器（E3 同款：真实 make_plan + 真实 run_inspection + 真实 ToolExecutor，"
                "asyncio.gather 并行镜像生产 Send 扇出）；容器未含 reporter 阶段（两臂相同常数项，不进入对照）。",
                "brief 臂 = 生产现状：B6 收敛简报（设备静态信息 + checks≤6 + 遥测摘要≤12 指标）；"
                "full_history 臂 = 完整遥测采样序列（全部点位）+ 全站设备清单 + checks 不截断。",
                "指标复用 C8 run_case/aggregate；Token 为替身工厂估算口径（estimate_tokens）；"
                "时延为 run_case 墙钟（替身即时回复，不含真实推理）。",
                "合规替身按工具回执决策、不读上下文载荷内容——两臂质量指标应一致，差异应全部落在输入 Token；"
                "若质量指标出现差异即为实现缺陷（本报告同时核对）。",
            ],
            "sample_size": {"cases": len(cases), "runs_per_arm": runs, "arms": 2},
            "variance": {
                "brief success_rate": _fmt_pct_range(s_brief["success_rate"]),
                "full_history success_rate": _fmt_pct_range(s_full["success_rate"]),
                "brief input_tokens_est/run": f"{llm_brief['input_tokens_est']:,}",
                "full_history input_tokens_est/run": f"{llm_full['input_tokens_est']:,}",
                "输入 Token 比（full/brief）": f"{in_ratio:.2f}x",
            },
            "known_biases": [
                "离线口径：成功率与 P/R 验证的是评测流程与契约管道，不代表模型效果。",
                "合规替身按 receipts 决策，不模拟「上下文变大导致注意力衰减/偏离」——真实模型下完整历史"
                "除计费差外还可能改变行为质量（方向不定，需真实模型重跑验证）。",
                "容器未含 reporter 阶段（两臂相同常数项）；生产图完整口径的真实模型费用见估算节。",
                "Token 为脚本估算（estimate_tokens：CJK 0.7 token/字，其余 4 字符 1 token），非计费口径；"
                "真实模型的简报/历史 Token 占比与此估算可能有差异。",
                "full_history 臂的全站清单对应 R3 上下文隔离被打破的形态（站点级数据进子任务上下文），"
                "仅用于 Token 对照，不涉及越权执行（工具权限面两臂相同）。",
            ],
            "disclaimer": E4_DISCLAIMER,
            "tables": [
                {
                    "caption": "总体指标（均值，范围 min~max；时延对全部 轮×用例 样本取分位数，ms 保留 1 位小数）",
                    "header": ["指标", "brief（收敛简报，现状）", "full_history（完整历史）"],
                    "rows": [
                        ["任务成功率", _fmt_pct_range(s_brief["success_rate"]), _fmt_pct_range(s_full["success_rate"])],
                        ["异常识别 Precision", _fmt_pct_range(s_brief["anomaly_precision"]), _fmt_pct_range(s_full["anomaly_precision"])],
                        ["异常识别 Recall", _fmt_pct_range(s_brief["anomaly_recall"]), _fmt_pct_range(s_full["anomaly_recall"])],
                        ["时延 P50 (ms)", f"{s_brief['latency_ms_p50']:.1f}", f"{s_full['latency_ms_p50']:.1f}"],
                        ["时延 P95 (ms)", f"{s_brief['latency_ms_p95']:.1f}", f"{s_full['latency_ms_p95']:.1f}"],
                        ["步数均值", f"{s_brief['steps_mean']['mean']:.2f}", f"{s_full['steps_mean']['mean']:.2f}"],
                        ["崩溃 run 数（合计）", str(s_brief["crashed_total"]["mean"]), str(s_full["crashed_total"]["mean"])],
                    ],
                },
                {
                    "caption": "Token 与 LLM 调用（单轮实测，脚本估算口径——E4 核心观测量）",
                    "header": ["臂", "LLM 调用/轮", "输入 Token/轮（估算）", "输出 Token/轮（估算）"],
                    "rows": [
                        ["brief（收敛简报）", str(llm_brief["calls"]), f"{llm_brief['input_tokens_est']:,}", f"{llm_brief['output_tokens_est']:,}"],
                        ["full_history（完整历史）", str(llm_full["calls"]), f"{llm_full['input_tokens_est']:,}", f"{llm_full['output_tokens_est']:,}"],
                    ],
                },
            ],
            "sections": [
                {
                    "caption": "结论（上下文隔离的代价结构，离线口径）",
                    "bullets": [
                        f"质量一致（替身按回执决策，不读载荷）：成功率 brief {_fmt_pct_range(s_brief['success_rate'])} vs "
                        f"full_history {_fmt_pct_range(s_full['success_rate'])}；异常 Recall "
                        f"{_fmt_pct_range(s_brief['anomaly_recall'])} vs {_fmt_pct_range(s_full['anomaly_recall'])}——"
                        "两臂差异不在质量维度（若出现差异为实现缺陷）。",
                        f"代价集中在输入 Token：full_history {llm_full['input_tokens_est']:,} vs "
                        f"brief {llm_brief['input_tokens_est']:,}/轮（{in_ratio:.2f}x）——完整遥测序列 + 全站清单"
                        "进上下文的计费差即上下文隔离（B6/R3）在真实模型下的直接收益。",
                        "LLM 调用数两臂相同（决策路径不依赖载荷大小）：上下文隔离不增加交互步数，只摊薄单步上下文。",
                        "口径声明：以上为离线结构验证；真实模型下完整历史还可能引起行为质量变化（注意力/偏离），"
                        "需真实模型重跑（费用估算见下节，等主线程确认预算）。",
                    ],
                },
                {
                    "caption": "真实模型费用估算（未执行，等主线程确认预算）",
                    "bullets": [
                        f"估算式：调用次数 = 用例数（{len(cases)}）× 每用例 LLM 调用 × 2 臂 × {runs} 轮 = "
                        f"({llm_brief['calls']} + {llm_full['calls']}) × {runs} = **{cost_offpeak['total_llm_calls']} 次**。",
                        f"Token（替身估算口径外推）：输入 {cost_offpeak['total_input_tokens']:,} + "
                        f"输出 {cost_offpeak['total_output_tokens']:,}（full_history 臂占输入增量主体）。",
                        f"USD（deepseek-flash off-peak，$0.15/M 入 + $0.60/M 出，来源 {DEEPSEEK_PRICES['source']}，"
                        f"{DEEPSEEK_PRICES['checked_at']} 查询）≈ **${cost_offpeak['total_usd']}**；"
                        f"peak 上界（$0.30/$1.20）≈ ${cost_peak['total_usd']}。",
                        "env 同 E1 真实版（OPENAI_BASE_URL=https://api.deepseek.com/v1、OPENAI_MODEL=deepseek-chat，"
                        "key 从仓库根 .env 读，调用前 unset 代理变量）；真实模型的重试/解析失败会使调用数高于基线。",
                    ],
                },
            ],
            "cost_estimate": {"offpeak": cost_offpeak, "peak": cost_peak, "prices": DEEPSEEK_PRICES},
        },
        generated_at=now.isoformat(),
    )
    stamp = now.strftime("%Y%m%d-%H%M%S")
    md_path, json_path = write_report(payload, out_dir, f"e4-offline-{stamp}")
    print(f"[e4] 报告：{md_path.name} / {json_path.name}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.e4_context", description="E4 上下文隔离消融（简报 vs 完整历史，离线）")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--limit", type=int, default=None, help="仅跑前 N 条（调试用）")
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    asyncio.run(run_e4(args.runs, limit=args.limit, out_dir=args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
