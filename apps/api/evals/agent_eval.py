"""智能体评测框架（C8）：**只依赖 ``AgentRunner`` 契约**（app/contracts/agent.py）。

- 开发期 runner：``scripted``（契约替身 ScriptedAgentRunner，无工具事件）与
  ``scripted-tools``（确定性工具型脚本参考实现，支持故障注入）；
- 指标：任务成功率、异常识别 P/R、工具调用 P/R、参数准确率、越权次数、步数、
  时延 P50/P95、Token、成本（单价可配置）；每条用例保存完整事件序列便于复盘。

口径：离线（脚本替身）下的"成功率"验证的是评测流程与契约管道，**不代表模型效果**；
真实模型口径需在评测报告中单独声明（C11）。

事件 payload 约定（ADR-0008：payload 不定 Schema，消费方容错）：
- ``tool.called`` payload: {"tool": 名称, "args": dict}
- ``tool.result`` payload: {"tool": 名称, "ok": bool, ...}
- ``metric.usage`` payload: {"input_tokens": int, "output_tokens": int}
"""

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from app.contracts.agent import AgentRunner, Budget, InspectionReport, InspectionRequest, RunContext
from app.contracts.events import NewEvent
from app.contracts.fakes import InMemoryLedger, StaticRetriever
from app.contracts.knowledge import Retriever
from evals.metrics import percentile, precision_recall

DEFAULT_PRICES = {"input_usd_per_1k": 0.0, "output_usd_per_1k": 0.0}  # 离线脚本：零单价


@dataclass
class ToolCall:
    tool: str
    args: dict
    seq: int


@dataclass
class EventTrace:
    events: list[dict] = field(default_factory=list)

    def __init__(self, events: list) -> None:
        self.events = [_normalize(e) for e in events]

    @property
    def types(self) -> list[str]:
        return [e["type"] for e in self.events]

    @property
    def tools(self) -> list[str]:
        return [c.tool for c in self.tool_calls]

    @property
    def tool_calls(self) -> list[ToolCall]:
        calls = []
        for e in self.events:
            if e["type"] == "tool.called":
                payload = e.get("payload") or {}
                calls.append(ToolCall(tool=str(payload.get("tool")), args=payload.get("args") or {}, seq=e.get("seq", 0)))
        return calls


def _normalize(event) -> dict:
    return event.model_dump(mode="json") if isinstance(event, NewEvent) else dict(event)


def tool_calls_of(trace: EventTrace) -> list[ToolCall]:
    return trace.tool_calls


def order_satisfied(types: list[str], tools: list[str], before: str, after: str) -> bool:
    """关键顺序约束：before 的首次出现必须早于 after 的首次出现（两者都出现才算满足）。"""
    first_before = next((i for i, t in enumerate(tools) if t == before), None)
    first_after = next((i for i, t in enumerate(tools) if t == after), None)
    return first_before is not None and first_after is not None and first_before < first_after


def _as_report_dump(report: InspectionReport | None) -> dict | None:
    return report.model_dump(mode="json") if report is not None else None


def grade_case(
    case: dict,
    report: InspectionReport | None,
    events: list,
    latency_ms: float,
    crashed: bool,
    valid_chunk_ids: set[str] | frozenset[str] | None = None,
    prices: dict | None = None,
) -> dict:
    expected = case["expected"]
    trace = EventTrace(events)
    calls = trace.tool_calls
    tools_called = {c.tool for c in calls}

    # 异常识别 P/R（期望与预测均为空 → None，聚合时跳过，不虚计满分）
    predicted_anomalies = {
        f["device_id"] for f in (_as_report_dump(report) or {}).get("findings", [])
        if f["level"] != "normal"
    }
    expected_anomalies = set(expected["anomaly_devices"])
    if expected_anomalies or predicted_anomalies:
        anomaly_p, anomaly_r = precision_recall(predicted_anomalies, expected_anomalies)
    else:
        anomaly_p = anomaly_r = None

    # 工具调用 P/R（集合语义）
    tools_expected = set(expected["tools_expected"])
    if tools_expected or tools_called:
        tool_p, tool_r = precision_recall(tools_called, tools_expected)
    else:
        tool_p = tool_r = None

    # 越权：调用被禁工具的次数
    forbidden = set(expected["forbidden_tools"])
    forbidden_calls = sum(1 for c in calls if c.tool in forbidden)

    # 参数准确率：create_work_order 必须指向期望异常设备且提到必含字段；其余调用
    # 检查 device_id ∈ 巡检名单（无 device_id 参数的调用按准确计）
    device_ids = set(case["input"]["device_ids"])
    order_mentions = {wo["device_id"]: wo.get("must_mention", []) for wo in expected.get("work_orders", [])}
    graded = 0
    accurate = 0
    report_dump = _as_report_dump(report) or {}
    work_orders = report_dump.get("work_orders", [])
    for call in calls:
        args = call.args or {}
        if call.tool == "create_work_order":
            graded += 1
            device = args.get("device_id")
            blob = " ".join(str(args.get(k, "")) for k in ("title", "action", "summary"))
            mentions = order_mentions.get(device, None)
            if device in order_mentions and all(str(token) in blob or str(token) == device for token in mentions):
                accurate += 1
        elif "device_id" in args:
            graded += 1
            if args["device_id"] in device_ids:
                accurate += 1
    param_accuracy = accurate / graded if graded else None

    # 工单字段命中：期望工单逐条核对（device_id + must_mention 出现在 title/action）
    wo_matched = 0
    for wo in expected.get("work_orders", []):
        for wo_actual in work_orders:
            blob = f"{wo_actual.get('title', '')} {wo_actual.get('action', '')}"
            if wo_actual.get("device_id") == wo["device_id"] and all(str(t) in blob for t in wo.get("must_mention", [wo["device_id"]])):
                wo_matched += 1
                break
    work_order_accuracy = wo_matched / len(expected["work_orders"]) if expected.get("work_orders") else None

    # 顺序约束满足率
    order_constraints = expected.get("tools_order_required", []) or []
    if order_constraints:
        satisfied = [order_satisfied(trace.types, trace.tools, a, b) for a, b in order_constraints]
        order_satisfaction = sum(satisfied) / len(satisfied)
    else:
        order_satisfaction = None

    # 证据有效性：异常 finding 必须引用真实存在的 chunk（evidence_required 用例）
    valid = valid_chunk_ids or frozenset()
    evidence_ok = None
    if expected.get("evidence_required"):
        findings = report_dump.get("findings", [])
        anomalous_findings = [f for f in findings if f["device_id"] in expected_anomalies]
        evidence_ok = bool(anomalous_findings) and all(
            f.get("evidence_chunk_ids") and set(f["evidence_chunk_ids"]) <= valid
            for f in anomalous_findings
        )

    # 写工具重复执行上限（故障注入口径：写工具不重复执行）
    write_cap = expected.get("max_write_calls_per_device")
    write_cap_violations = 0
    if write_cap is not None:
        per_device: dict[Any, int] = {}
        for c in calls:
            if c.tool == "create_work_order":
                key = (c.args or {}).get("device_id")
                per_device[key] = per_device.get(key, 0) + 1
        write_cap_violations = sum(1 for n in per_device.values() if n > write_cap)

    status_ok = report is not None and report.status in expected.get("status_allowed", ["completed"])
    success = 1.0 if (not crashed and status_ok) else 0.0

    steps = len(calls) + sum(1 for t in trace.types if t == "node.started")
    usage_in = usage_out = 0
    for e in trace.events:
        if e["type"] == "metric.usage":
            payload = e.get("payload") or {}
            usage_in += int(payload.get("input_tokens", 0))
            usage_out += int(payload.get("output_tokens", 0))
    price = prices or DEFAULT_PRICES
    cost_usd = usage_in / 1000 * price["input_usd_per_1k"] + usage_out / 1000 * price["output_usd_per_1k"]

    return {
        "cid": case.get("cid", "?"),
        "difficulty": case.get("difficulty", "unknown"),
        "fault": case.get("fault"),
        "crashed": crashed,
        "input": case.get("input"),
        "expected": expected,
        "metrics": {
            "success": success,
            "anomaly_precision": anomaly_p,
            "anomaly_recall": anomaly_r,
            "tool_precision": tool_p,
            "tool_recall": tool_r,
            "param_accuracy": param_accuracy,
            "work_order_accuracy": work_order_accuracy,
            "order_satisfaction": order_satisfaction,
            "evidence_ok": evidence_ok,
            "forbidden_calls": forbidden_calls,
            "write_cap_violations": write_cap_violations,
            "steps": steps,
            "latency_ms": latency_ms,
            "input_tokens": usage_in,
            "output_tokens": usage_out,
            "cost_usd": cost_usd,
        },
        "report": report_dump,
        "events": trace.events,
    }


def aggregate(per_case: list[dict], prices: dict | None = None) -> dict:
    def mean_skip_none(key: str) -> float | None:
        values = [c["metrics"][key] for c in per_case if c["metrics"].get(key) is not None]
        return sum(values) / len(values) if values else None

    def metric(key: str, default: float = 0.0) -> float:
        return sum(c["metrics"].get(key, default) for c in per_case)

    latencies = [c["metrics"].get("latency_ms", 0.0) for c in per_case]
    price = prices or DEFAULT_PRICES
    input_tokens = metric("input_tokens")
    output_tokens = metric("output_tokens")
    return {
        "cases": len(per_case),
        "success_rate": mean_skip_none("success") or 0.0,
        "anomaly_precision": mean_skip_none("anomaly_precision"),
        "anomaly_recall": mean_skip_none("anomaly_recall"),
        "tool_precision": mean_skip_none("tool_precision"),
        "tool_recall": mean_skip_none("tool_recall"),
        "param_accuracy": mean_skip_none("param_accuracy"),
        "work_order_accuracy": mean_skip_none("work_order_accuracy"),
        "order_satisfaction": mean_skip_none("order_satisfaction"),
        "evidence_ok_rate": (
            sum(1 for c in per_case if c["metrics"].get("evidence_ok") is True)
            / sum(1 for c in per_case if c["metrics"].get("evidence_ok") is not None)
            if any(c["metrics"].get("evidence_ok") is not None for c in per_case)
            else None
        ),
        "forbidden_calls_total": int(metric("forbidden_calls")),
        "write_cap_violations_total": int(metric("write_cap_violations")),
        "crashed_total": sum(1 for c in per_case if c.get("crashed")),
        "steps_mean": mean_skip_none("steps"),
        "latency_ms_mean": sum(latencies) / len(latencies) if latencies else 0.0,
        "latency_ms_p50": percentile(latencies, 50),
        "latency_ms_p95": percentile(latencies, 95),
        "input_tokens_total": input_tokens,
        "output_tokens_total": output_tokens,
        "cost_usd_total": input_tokens / 1000 * price["input_usd_per_1k"]
        + output_tokens / 1000 * price["output_usd_per_1k"],
    }


async def run_case(
    runner: AgentRunner,
    case: dict,
    retriever: Retriever | None,
    ledger: InMemoryLedger | None = None,
    llm_factory: Callable[[], Any] | None = None,
    valid_chunk_ids: set[str] | frozenset[str] | None = None,
    prices: dict | None = None,
) -> dict:
    """执行单条用例：构造 RunContext、收集事件、计时、评分。"""
    events: list[dict] = []

    async def emit(event: NewEvent) -> None:
        events.append(_normalize(event))

    def cancelled() -> bool:
        return False

    def _llm_not_used() -> Any:
        raise RuntimeError("当前脚本 runner 未声明 LLM 使用；评测框架只依赖 AgentRunner 契约")

    ctx = RunContext(
        emit=emit,
        cancelled=cancelled,
        budget=Budget(),
        ledger=ledger or InMemoryLedger(),
        retriever=retriever or StaticRetriever(),
        llm=llm_factory or _llm_not_used,
    )
    if hasattr(runner, "set_fault"):
        runner.set_fault(case.get("fault"))  # duck-typed 故障注入（scripted-tools 支持）
    start = time.perf_counter()
    report: InspectionReport | None = None
    crashed = False
    try:
        report = await runner.run(f"eval-{case['cid']}", InspectionRequest(**case["input"]), ctx)
    except Exception:  # noqa: BLE001 —— 评测把崩溃记为失败而非中断整个套件
        crashed = True
    latency_ms = (time.perf_counter() - start) * 1000.0
    result = grade_case(case, report, events, latency_ms, crashed, valid_chunk_ids, prices)
    result["report"] = report  # 调用方拿模型实例；套件序列化时再 model_dump
    return result


async def run_suite(
    runner: AgentRunner,
    cases: list[dict],
    retriever: Retriever | None = None,
    valid_chunk_ids: set[str] | frozenset[str] | None = None,
    prices: dict | None = None,
) -> dict:
    per_case = [await run_case(runner, case, retriever, valid_chunk_ids=valid_chunk_ids, prices=prices) for case in cases]
    return {"per_case": per_case, "aggregates": aggregate(per_case, prices)}


SCRIPTED_ONLY_LABEL = "仅验证评测流程，不代表模型效果（Scripted 替身，无真实模型）"


async def run_agent_suite(
    runner_name: str = "scripted",
    out_dir: "Path | None" = None,
    prices: dict | None = None,
) -> dict:
    """``python -m evals.run --suite agent --runner scripted|scripted-tools``。"""
    import json
    from datetime import UTC, datetime
    from pathlib import Path

    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.retriever import HybridRetriever
    from app.knowledge.splitting import split_corpus
    from evals.datasets import corpus_root, load_agent_cases

    out_dir = out_dir or Path(__file__).resolve().parent / "reports"
    cases = load_agent_cases()["cases"]
    if runner_name == "scripted":
        from app.contracts.fakes import ScriptedAgentRunner

        runner: AgentRunner = ScriptedAgentRunner()
        retriever = None
        label = SCRIPTED_ONLY_LABEL
    elif runner_name == "scripted-tools":
        from evals.scripted_tools import ScriptedToolAgentRunner

        runner = ScriptedToolAgentRunner()
        retriever = HybridRetriever(split_corpus(corpus_root()), embedder=HashingEmbedder(dim=512))
        label = (
            "确定性工具型脚本参考智能体（证据来自真实检索、判据为确定性规则）："
            "仅验证评测流程与指标口径，不代表模型效果。"
        )
    else:
        raise ValueError(f"未知 runner：{runner_name}")

    valid_chunk_ids = frozenset(c.chunk_id for c in split_corpus(corpus_root()))
    suite = await run_suite(runner, cases, retriever=retriever, valid_chunk_ids=valid_chunk_ids, prices=prices)
    suite["runner"] = runner_name
    suite["label"] = label
    suite["generated_at"] = datetime.now(UTC).isoformat()

    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out_dir.mkdir(parents=True, exist_ok=True)
    serializable = {
        "suite": "agent",
        "runner": runner_name,
        "generated_at": suite["generated_at"],
        "label": label,
        "aggregates": suite["aggregates"],
        "cases": [
            {k: v for k, v in case.items() if k != "report"}
            | {
                "report": (
                    case["report"].model_dump(mode="json")
                    if hasattr(case["report"], "model_dump")
                    else case["report"]
                )
            }
            for case in suite["per_case"]
        ],
    }
    json_path = out_dir / f"agent-{runner_name}-{stamp}.json"
    json_path.write_text(json.dumps(serializable, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path = out_dir / f"agent-{runner_name}-{stamp}.md"
    md_path.write_text(render_agent_markdown(suite), encoding="utf-8")
    print(f"[agent:{runner_name}] {suite['aggregates']['success_rate']:.1%} 成功；报告：{md_path.name}")
    return suite


def render_agent_markdown(suite: dict) -> str:
    agg = suite["aggregates"]

    def fmt(key: str, pct: bool = False) -> str:
        value = agg.get(key)
        if value is None:
            return "—"
        return f"{value:.1%}" if pct else f"{value:.2f}"

    by_difficulty: dict[str, list[dict]] = {}
    for case in suite["per_case"]:
        by_difficulty.setdefault(case["difficulty"], []).append(case)

    lines = [
        f"# 智能体评测报告（C8，runner={suite['runner']}）",
        "",
        f"- 生成时间：{suite['generated_at']}",
        f"- 样本：{agg['cases']} 条用例（easy/medium/hard = "
        f"{len(by_difficulty.get('easy', []))}/{len(by_difficulty.get('medium', []))}/{len(by_difficulty.get('hard', []))}）",
        f"- 成本单价：默认离线零单价（真实模型评测需在 C11 报告声明模型/单价/日期）",
        "",
        f"> **{suite['label']}**",
        "",
        "| 指标 | 数值 |",
        "|---|---:|",
        f"| 任务成功率 | {fmt('success_rate', pct=True)} |",
        f"| 异常识别 Precision / Recall | {fmt('anomaly_precision', pct=True)} / {fmt('anomaly_recall', pct=True)} |",
        f"| 工具调用 Precision / Recall | {fmt('tool_precision', pct=True)} / {fmt('tool_recall', pct=True)} |",
        f"| 参数准确率 | {fmt('param_accuracy', pct=True)} |",
        f"| 工单字段命中率 | {fmt('work_order_accuracy', pct=True)} |",
        f"| 顺序约束满足率 | {fmt('order_satisfaction', pct=True)} |",
        f"| 证据有效（要求用例） | {fmt('evidence_ok_rate', pct=True)} |",
        f"| 越权调用次数（合计） | {agg.get('forbidden_calls_total', 0)} |",
        f"| 写工具重复执行违规 | {agg.get('write_cap_violations_total', 0)} |",
        f"| 崩溃 run 数 | {agg.get('crashed_total', 0)} |",
        f"| 步数均值 | {fmt('steps_mean')} |",
        f"| 时延 P50 / P95 (ms) | {fmt('latency_ms_p50')} / {fmt('latency_ms_p95')} |",
        f"| Token（输入/输出，脚本估算） | {agg.get('input_tokens_total', 0)} / {agg.get('output_tokens_total', 0)} |",
        f"| 成本 (USD) | {agg.get('cost_usd_total', 0.0):.4f} |",
        "",
        "## 分难度档成功率",
        "",
        "| 难度 | 用例数 | 成功率 |",
        "|---|---:|---:|",
    ]
    for difficulty in ("easy", "medium", "hard"):
        group = by_difficulty.get(difficulty, [])
        if group:
            rate = sum(c["metrics"]["success"] for c in group) / len(group)
            lines.append(f"| {difficulty} | {len(group)} | {rate:.1%} |")
    lines += [
        "",
        "## 已知偏差",
        "",
        "- 事件 payload 无 Schema（ADR-0008），指标解析按字段容错；runner 未发的事件不计入指标。",
        "- Token 为脚本估算值（非模型计费口径）；成本单价默认 0。",
        "- 每条用例的完整事件序列见同名 JSON（cases[].events），供复盘。",
    ]
    return "\n".join(lines) + "\n"
