"""质量门（C10，P1）：离线基线指标低于阈值 → 退出码 1，真正让 CI 失败。

吸取"检查只打印不失败"的教训：本模块是验收命令的一等公民
（``python -m evals.gate``），阈值内建于 DEFAULT_THRESHOLDS，可用
``evals/gate_thresholds.json`` 覆盖（结构相同）。

口径：门检查的是**离线确定性基线**（检索 hybrid-bigram + 工具型脚本参考智能体），
用于守护评测管道健康（切分/检索/判据/评分链路任一退化即红）；不代表模型效果。
真实模型基线的阈值应在评测报告中另行声明（C11）。
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

THRESHOLDS_FILE = Path(__file__).resolve().parent / "gate_thresholds.json"

DEFAULT_THRESHOLDS: dict = {
    "retrieval": {
        "hybrid-bigram": {  # C6 实测 2026-10-24：@5=0.908 @3=0.792 mrr=0.752，阈值为健康下限
            "recall_at_5_min": 0.88,
            "recall_at_3_min": 0.75,
            "mrr_min": 0.65,
        }
    },
    "agent": {  # scripted-tools 实测 2026-10-24：success=1.0 anomaly_recall=0.872，阈值留出波动余量
        "success_rate_min": 0.9,
        "anomaly_recall_min": 0.8,
        "forbidden_calls_max": 0,  # 越权调用：零容忍
        "write_cap_violations_max": 0,
        "crashed_max": 0,
    },
}


def load_thresholds() -> dict:
    if THRESHOLDS_FILE.exists():
        return json.loads(THRESHOLDS_FILE.read_text(encoding="utf-8"))
    return DEFAULT_THRESHOLDS


async def _retrieval_baseline() -> dict:
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.retriever import HybridRetriever
    from app.knowledge.splitting import split_corpus
    from evals.datasets import corpus_root, load_retrieval_annotations
    from evals.retrieval_eval import annotations_to_cases, run_retrieval_eval

    chunks = split_corpus(corpus_root())
    retriever = HybridRetriever(chunks, embedder=HashingEmbedder(dim=512), token_mode="bigram")
    report = await run_retrieval_eval(retriever, annotations_to_cases(load_retrieval_annotations()))
    return {"hybrid-bigram": report.aggregates}


async def _agent_baseline() -> dict:
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.retriever import HybridRetriever
    from app.knowledge.splitting import split_corpus
    from evals.agent_eval import run_suite
    from evals.datasets import corpus_root, load_agent_cases
    from evals.scripted_tools import ScriptedToolAgentRunner

    corpus_chunks = split_corpus(corpus_root())
    runner = ScriptedToolAgentRunner()
    retriever = HybridRetriever(corpus_chunks, embedder=HashingEmbedder(dim=512))
    suite = await run_suite(
        runner,
        load_agent_cases()["cases"],
        retriever=retriever,
        valid_chunk_ids=frozenset(c.chunk_id for c in corpus_chunks),
    )
    return suite["aggregates"]


def check_thresholds(thresholds: dict, retrieval: dict, agent: dict) -> list[str]:
    """比对指标与阈值，返回失败项描述（空列表 = 通过）。"""
    failures: list[str] = []
    for variant, limits in thresholds.get("retrieval", {}).items():
        agg = retrieval.get(variant)
        if agg is None:
            failures.append(f"retrieval/{variant}: 基线缺失")
            continue
        for key, floor in limits.items():
            metric = key.removesuffix("_min")
            value = agg.get(metric)
            if value is None or value < floor:
                failures.append(f"retrieval/{variant}: {metric}={value} 低于阈值 {floor}")
    a = thresholds.get("agent", {})
    for key in ("success_rate", "anomaly_recall"):
        floor = a.get(f"{key}_min")
        if floor is not None and (agent.get(key) is None or agent[key] < floor):
            failures.append(f"agent: {key}={agent.get(key)} 低于阈值 {floor}")
    for key in ("forbidden_calls", "write_cap_violations", "crashed"):
        ceiling = a.get(f"{key}_max")
        if ceiling is not None and (agent.get(f"{key}_total", 0) if key != "crashed" else agent.get("crashed_total", 0)) > ceiling:
            failures.append(f"agent: {key} 超过上限 {ceiling}")
    return failures


def load_baseline(suite: str) -> dict:
    if suite == "retrieval":
        return asyncio.run(_retrieval_baseline())
    if suite == "agent":
        return asyncio.run(_agent_baseline())
    raise ValueError(f"未知套件：{suite}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.gate", description="评测质量门：低于阈值退出码 1")
    parser.add_argument("--thresholds", type=Path, default=None, help="覆盖阈值 JSON（默认内建/gate_thresholds.json）")
    args = parser.parse_args(argv)
    thresholds = json.loads(args.thresholds.read_text(encoding="utf-8")) if args.thresholds else load_thresholds()

    print("[gate] 采集离线基线：retrieval(hybrid-bigram) + agent(scripted-tools) ...")
    retrieval = load_baseline("retrieval")
    agent = load_baseline("agent")
    failures = check_thresholds(thresholds, retrieval, agent)

    print(f"[gate] retrieval hybrid-bigram recall@5={retrieval['hybrid-bigram']['recall_at_5']:.3f} "
          f"mrr={retrieval['hybrid-bigram']['mrr']:.3f}")
    print(f"[gate] agent success={agent['success_rate']:.1%} anomaly_recall={agent['anomaly_recall']:.3f} "
          f"forbidden={agent['forbidden_calls_total']} crashed={agent['crashed_total']}")
    if failures:
        for line in failures:
            print(f"[gate][FAIL] {line}")
        print("[gate] 质量门未通过：退出码 1")
        return 1
    print("[gate] 质量门通过：离线确定性基线达标（不代表模型效果）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
