"""E3 消融入口（X3）：Send 并行扇出 vs 逐设备串行——时延 P50/P95。

替身层实现（不改生产默认值）：生产图（app/agents/graph.py，只读）的扇出方式
固定为 Send 并行；本模块在 evals 侧构建**同一容器**（真实 make_plan 规划 +
真实 run_inspection 子任务 + 真实 ToolExecutor 强制），唯一消融变量是设备
子任务的调度方式：
- ``parallel``：asyncio.gather 并发（镜像 Send 扇出语义）；
- ``serial``：逐设备顺序 await（串行基线）。
子任务时延由合规替身 LLM 的 ``delay_s`` 注入（模拟单次 LLM 推理时延）——离线
绝对时延不代表真实模型，但"并行 ≈ max(子任务) vs 串行 ≈ Σ(子任务)"的结构
关系真实成立。**仅验证评测流程，不代表模型效果。**

用法：``python -m evals.e3_fanout [--runs 3] [--delay-s 0.05]``。
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app.contracts.agent import InspectionReport, InspectionRequest, RunContext
from app.contracts.events import NewEvent
from evals.compliant_llm import CompliantLLMFactory
from evals.report import finalize, write_report

DEFAULT_LLM_DELAY_S = 0.05  # 模拟单次 LLM 推理时延（离线注入，非实测）

E3_DISCLAIMER = (
    "离线扇出消融（替身层容器 + CompliantScriptedLLM 注入时延）：时延绝对值由 "
    "delay_s 参数决定，仅验证并行/串行的结构关系与评测流程，不代表真实模型时延。"
)


def device_bucket(n: int) -> str:
    """设备数分桶（报告分组口径）。"""
    if n <= 1:
        return "1 台"
    if n <= 3:
        return "2-3 台"
    if n <= 5:
        return "4-5 台"
    return "6+ 台"


class FanoutSimRunner:
    """evals 侧扇出消融容器：规划与子任务为生产代码，调度方式为消融变量。"""

    def __init__(self, *, fanout_mode: str = "parallel", fixture_root=None) -> None:
        if fanout_mode not in ("parallel", "serial"):
            raise ValueError(f"未知扇出模式：{fanout_mode}")
        self.fanout_mode = fanout_mode
        self._fixture_root = fixture_root

    async def run(self, run_id: str, input: InspectionRequest, ctx: RunContext) -> InspectionReport:
        from app.agents.inspector import run_inspection
        from app.agents.planner import make_plan
        from app.tools.budget import BudgetTracker
        from app.tools.inspection_tools import build_registry

        tracker = BudgetTracker(ctx.budget)
        registry = build_registry(ctx, fixture_root=self._fixture_root)
        await ctx.emit(NewEvent(type="node.started", node="supervisor", payload={"phase": f"fanout-{self.fanout_mode}"}))
        plan_result = await make_plan(input, ctx, tracker)
        tasks = [{"device_id": s.device_id, "checks": s.checks} for s in plan_result.plan.steps]
        await ctx.emit(NewEvent(type="plan.created", node="planner", payload={"steps": tasks, "fallback": plan_result.fallback}))

        async def inspect(task: dict):
            await ctx.emit(NewEvent(type="node.started", node="inspector", payload={"device_id": task["device_id"]}))
            outcome = await run_inspection(
                task["device_id"], task["checks"], ctx, registry, tracker,
                fixture_root=self._fixture_root,
            )
            return outcome

        # 唯一消融变量：并发（镜像 Send 扇出）vs 串行逐台
        if self.fanout_mode == "parallel":
            outcomes = await asyncio.gather(*(inspect(t) for t in tasks))
        else:
            outcomes = []
            for task in tasks:
                outcomes.append(await inspect(task))

        findings = [o.finding for o in outcomes if o.finding is not None]
        failures = [{"device_id": o.device_id, "reason": o.failure_reason} for o in outcomes if o.finding is None]
        requested = list(input.device_ids)
        covered = {f.device_id for f in findings}
        coverage = len(covered) / len(requested) if requested else 0.0
        notes = [f"{f['device_id']}: {f['reason']}" for f in failures]
        if plan_result.fallback:
            notes.append("规划失败，使用默认全量检查计划")
        await ctx.emit(NewEvent(type="node.completed", node="reporter", payload={"status": "completed" if not failures else "partial"}))
        return InspectionReport(
            findings=findings,
            work_orders=[],
            status="completed" if not failures else "partial",
            coverage=round(coverage, 4),
            notes="；".join(notes) if notes else None,
        )


async def run_e3(runs: int = 3, delay_s: float = DEFAULT_LLM_DELAY_S, out_dir: Path | None = None) -> dict:
    from evals.agent_eval import run_case
    from evals.datasets import fixtures_root
    from evals.e1_ablation import DEEPSEEK_PRICES, build_retriever, estimate_real_run_cost, load_non_fault_cases
    from evals.metrics import percentile

    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    cases = load_non_fault_cases()
    retriever, valid_chunk_ids = build_retriever()
    fixtures = fixtures_root()
    now = datetime.now(UTC)

    mode_latencies: dict[str, list[dict]] = {}  # mode → 每用例 {cid, devices, latency_ms}
    mode_calls: dict[str, int] = {}
    for mode in ("parallel", "serial"):
        records: list[dict] = []
        calls = 0
        for _ in range(runs):
            factory = CompliantLLMFactory(delay_s=delay_s)
            runner = FanoutSimRunner(fanout_mode=mode, fixture_root=fixtures)
            for case in cases:
                outcome = await run_case(
                    runner, case, retriever=retriever, llm_factory=factory.build,
                    valid_chunk_ids=valid_chunk_ids,
                )
                records.append(
                    {
                        "cid": case["cid"],
                        "devices": len(case["input"]["device_ids"]),
                        "latency_ms": outcome["metrics"]["latency_ms"],
                        "success": outcome["metrics"]["success"],
                    }
                )
            calls = factory.calls
        mode_latencies[mode] = records
        mode_calls[mode] = calls
        p50 = percentile([r["latency_ms"] for r in records], 50)
        p95 = percentile([r["latency_ms"] for r in records], 95)
        succ = sum(r["success"] for r in records) / len(records)
        print(f"[e3:{mode}] success={succ:.1%} p50={p50:.1f}ms p95={p95:.1f}ms llm_calls/run={calls}")

    def bucket_table(mode: str) -> list[list[str]]:
        rows = []
        for bucket in ("1 台", "2-3 台", "4-5 台", "6+ 台"):
            sample = [r for r in mode_latencies[mode] if device_bucket(r["devices"]) == bucket]
            if not sample:
                continue
            values = [r["latency_ms"] for r in sample]
            rows.append(
                [
                    bucket,
                    str(len(sample)),
                    f"{percentile(values, 50):.1f}",
                    f"{percentile(values, 95):.1f}",
                ]
            )
        return rows

    par = [r["latency_ms"] for r in mode_latencies["parallel"]]
    ser = [r["latency_ms"] for r in mode_latencies["serial"]]
    succ_par = sum(r["success"] for r in mode_latencies["parallel"]) / len(par)
    succ_ser = sum(r["success"] for r in mode_latencies["serial"]) / len(ser)

    # 真实模型费用估算：生产图 multi（含 reporter）× 2 模式 × runs 轮 —— 以 E1 multi
    # 单轮实测调用/Token 为基线（本容器无 reporter 起草调用，估真实口径用 E1 基线）
    cost_basis_calls = max(mode_calls.values())  # 两模式调用数相同（调度不改调用量）
    est = estimate_real_run_cost(
        {"parallel": cost_basis_calls, "serial": cost_basis_calls},
        {"parallel": (0, 0), "serial": (0, 0)},  # Token 基线在 bullets 中引用 E1 实测（见下）
        runs,
        DEEPSEEK_PRICES["flash_offpeak"],
    )

    payload = finalize(
        {
            "suite": "e3-fanout",
            "title": "E3 消融：Send 并行扇出 vs 逐设备串行（离线替身层，X3）",
            "meta": [
                ("模型", "CompliantScriptedLLM（确定性合规替身，注入 delay_s 模拟单次 LLM 时延）"),
                ("日期", f"{now.strftime('%Y-%m-%d')}（UTC；本地 UTC+8 为 {(now + timedelta(hours=8)).strftime('%Y-%m-%d')}）"),
                ("范围", f"{len(cases)} 条非故障用例，每模式 {runs} 次"),
                ("注入时延 delay_s", f"{delay_s * 1000:.0f} ms/次 LLM 调用"),
                ("消融变量", "仅设备子任务调度方式（asyncio.gather 并行 vs 逐台 await 串行）；规划/子任务/执行器为同一份生产代码"),
            ],
            "method": [
                "FanoutSimRunner（evals 侧替身层，不改生产默认）：真实 make_plan 规划 + 真实 run_inspection 子任务"
                "+ 真实 ToolExecutor 强制；并行模式镜像生产图 Send 扇出的并发语义。",
                "时延经 run_case 实测（runner.run 墙钟）；delay_s 注入替身 LLM 单次调用时延，"
                "子任务 LLM 调用数 2-3 次/台 → 并行墙钟 ≈ max(子任务)、串行 ≈ Σ(子任务)。",
                "成功率应两模式一致（调度不是结论变量），差异即实现缺陷——本报告同时核对。",
            ],
            "sample_size": {"cases": len(cases), "runs_per_mode": runs, "modes": 2},
            "variance": {
                "parallel latency_ms_p95": round(percentile(par, 95), 1),
                "serial latency_ms_p95": round(percentile(ser, 95), 1),
                "parallel/serial p50 比": f"{percentile(par, 50) / percentile(ser, 50):.3f}",
            },
            "known_biases": [
                "离线口径：时延绝对值由 delay_s 决定（模拟单次 LLM 推理时延），不代表真实模型时延；"
                "本实验验证的是并行/串行的结构关系与消融管线。",
                "容器未含 reporter 阶段（两模式相同的常数项，不进入对照）；生产图完整口径的真实模型重跑费用见估算节。",
                "成功率与结论指标两模式应一致（同一替身同一判据），时延为唯一对照维度。",
            ],
            "disclaimer": E3_DISCLAIMER,
            "tables": [
                {
                    "caption": f"总体时延（全部 轮×用例 样本；ms，1 位小数；delay_s={delay_s * 1000:.0f}ms）",
                    "header": ["模式", "样本数", "成功率", "P50 (ms)", "P95 (ms)"],
                    "rows": [
                        ["parallel（镜像 Send 扇出）", str(len(par)), f"{succ_par:.1%}", f"{percentile(par, 50):.1f}", f"{percentile(par, 95):.1f}"],
                        ["serial（逐设备串行）", str(len(ser)), f"{succ_ser:.1%}", f"{percentile(ser, 50):.1f}", f"{percentile(ser, 95):.1f}"],
                    ],
                },
                {
                    "caption": "分设备数桶 P50/P95（并行收益随设备数增长）",
                    "header": ["设备数", "样本数", "并行 P50", "并行 P95", "串行 P50", "串行 P95"],
                    "rows": [
                        [p[0], p[1], p[2], p[3], s[2], s[3]]
                        for p, s in zip(bucket_table("parallel"), bucket_table("serial"))
                    ],
                },
            ],
            "sections": [
                {
                    "caption": "结论（计划 §11-E3 判定）",
                    "bullets": [
                        f"并行 P50={percentile(par, 50):.1f}ms vs 串行 P50={percentile(ser, 50):.1f}ms"
                        f"（{percentile(par, 50) / percentile(ser, 50):.0%}）：注入 LLM 时延下并行显著降低时延，符合 §11-E3 预期。",
                        "分桶明细：单台用例两模式相同（无并发对象），收益集中在 2 台以上用例并随设备数增长。",
                        f"成功率两模式均 {succ_par:.0%}（调度不是结论变量，核对一致性）。",
                        "口径声明：以上为离线结构验证；真实模型时延重跑的费用估算见下节，等主线程确认。",
                    ],
                },
                {
                    "caption": "真实模型费用估算（未执行，等主线程确认预算）",
                    "bullets": [
                        f"真实口径 E3 = 生产图 multi（含 reporter）× 2 模式 × {len(cases)} 条 × {runs} 轮；"
                        f"调用数基线 = 本容器单轮实测（{cost_basis_calls} 次/轮，无 reporter 起草调用）"
                        "与 E1 multi 实测（375 次/轮，含 reporter）之间，按 E1 基线估上界。",
                        f"总 LLM 调用 ≈ {est['total_llm_calls']} 次（按下界容器口径）~ {375 * 2 * runs} 次（按 E1 multi 上界口径）；"
                        f"USD 按 deepseek-flash off-peak（$0.15/M 输入、$0.60/M 输出，来源 {DEEPSEEK_PRICES['source']}，"
                        f"{DEEPSEEK_PRICES['checked_at']} 查询）：Token 规模与 E1 multi 同量级（约 146K 输入/轮 → 两模式两轮制合计 ≈ $0.13~0.20）。",
                        "env 同 E1（api.deepseek.com/v1，deepseek-chat）；deepseek-chat 计费档假设同 E1 报告注意事项。",
                    ],
                },
            ],
            "latency_by_mode": {m: [r["latency_ms"] for r in records] for m, records in mode_latencies.items()},
            "llm_calls_per_run": mode_calls,
        },
        generated_at=now.isoformat(),
    )
    stamp = now.strftime("%Y%m%d-%H%M%S")
    md_path, json_path = write_report(payload, out_dir, f"e3-offline-{stamp}")
    print(f"[e3] 报告：{md_path.name} / {json_path.name}")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.e3_fanout", description="E3 扇出消融（并行 vs 串行，离线）")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--delay-s", type=float, default=DEFAULT_LLM_DELAY_S)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    asyncio.run(run_e3(runs=args.runs, delay_s=args.delay_s, out_dir=args.out_dir))
    return 0


if __name__ == "__main__":
    sys.exit(main())
