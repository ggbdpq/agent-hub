"""评测入口（C6/C8）：``python -m evals.run --suite retrieval|agent``。

检索套件在 C0 标注上跑多种确定性检索配置（混合/单路 × bigram/unigram），
产出 evals/reports/retrieval-*.md|json；分词方案按 Recall@5 对比定论（U4）。
"""

import argparse
import asyncio
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from evals.datasets import corpus_root, load_retrieval_annotations
from evals.retrieval_eval import annotations_to_cases, run_retrieval_eval
from app.knowledge.bm25 import Bm25Searcher
from app.knowledge.embedding import HashingEmbedder
from app.knowledge.retriever import HybridRetriever
from app.knowledge.splitting import split_corpus

DISCLAIMER = (
    "合成语料 + 人工标注的离线评测：量化的是确定性检索管道在本标注集上的表现，"
    "仅验证评测流程，不代表真实业务检索效果，不代表模型效果。"
)

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def _build_variants() -> dict[str, object]:
    """name → Retriever。全部确定性、离线可复现。"""
    chunks = split_corpus(corpus_root())
    embedder = HashingEmbedder(dim=512)
    return {
        "hybrid-bigram": HybridRetriever(chunks, embedder=embedder, token_mode="bigram"),
        "hybrid-unigram": HybridRetriever(chunks, embedder=embedder, token_mode="unigram"),
        "bm25-only-bigram": Bm25Searcher(chunks, token_mode="bigram"),
        "vector-only-bigram": _VectorOnly(chunks, embedder),
    }


class _VectorOnly:
    """仅向量单路（E5 消融基线）：复用 VectorSearcher 的契约签名。"""

    def __init__(self, chunks, embedder) -> None:
        from app.knowledge.vector import VectorSearcher

        self._inner = VectorSearcher(chunks, embedder, min_sim=0.05)

    async def search(self, query: str, k: int, filters: dict | None = None) -> list:
        return await self._inner.search(query, k, filters)


async def run_retrieval_suite(out_dir: Path = REPORTS_DIR, k: int = 5) -> dict:
    cases = annotations_to_cases(load_retrieval_annotations())
    variants = _build_variants()
    results = []
    for name, retriever in variants.items():
        report = await run_retrieval_eval(retriever, cases, k=k)
        agg = report.aggregates
        results.append({"name": name, "aggregates": agg, "config": report.config})
        print(
            f"[retrieval] {name}: recall@1={agg['recall_at_1']:.3f} @3={agg['recall_at_3']:.3f} "
            f"@5={agg['recall_at_5']:.3f} mrr={agg['mrr']:.3f} p95={agg['latency_ms_p95']:.2f}ms"
        )

    n_chunks = len(split_corpus(corpus_root()))
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    payload = {
        "suite": "retrieval",
        "generated_at": datetime.now(UTC).isoformat(),
        "disclaimer": DISCLAIMER,
        "corpus": {"docs": 30, "chunks": n_chunks},
        "annotations": len(cases),
        "k": k,
        "variants": results,
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"retrieval-{stamp}.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path = out_dir / f"retrieval-{stamp}.md"
    md_path.write_text(render_retrieval_markdown(payload), encoding="utf-8")
    print(f"[retrieval] 报告：{md_path.name} / {json_path.name}")
    return payload


def render_retrieval_markdown(payload: dict) -> str:
    lines = [
        "# 检索评测报告（C6）",
        "",
        f"- 生成时间：{payload['generated_at']}",
        f"- 语料：{payload['corpus']['docs']} 篇合成规程 / {payload['corpus']['chunks']} chunks；标注 {payload['annotations']} 条",
        f"- 指标：Recall@1/3/5（宏平均）、MRR、检索耗时（perf_counter 实测）",
        "",
        f"> **{payload['disclaimer']}**",
        "",
        "| 变体 | Recall@1 | Recall@3 | Recall@5 | MRR | P50 (ms) | P95 (ms) | 空结果率 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for v in payload["variants"]:
        a = v["aggregates"]
        lines.append(
            f"| {v['name']} | {a['recall_at_1']:.3f} | {a['recall_at_3']:.3f} | {a['recall_at_5']:.3f} "
            f"| {a['mrr']:.3f} | {a['latency_ms_p50']:.2f} | {a['latency_ms_p95']:.2f} | {a['empty_rate']:.1%} |"
        )
    best_recall5 = max(payload["variants"], key=lambda v: v["aggregates"]["recall_at_5"])
    lines += [
        "",
        "## 分词方案结论（U4）",
        "",
        f"- 按 Recall@5：最优变体 **{best_recall5['name']}**（{best_recall5['aggregates']['recall_at_5']:.3f}）。",
        "- jieba 受依赖限制未安装（见 docs/integration-requests/C.md），对比在 bigram vs unigram 之间完成；",
        "  结论与差异幅度见上表，方案随最优变体确定。",
        "",
        "## 方法与已知偏差",
        "",
        "- 每条标注人工指定 1~2 个相关 chunk，Recall@k 为宏平均；标注覆盖 5 类设备 × 12 条。",
        "- 耗时为单进程内存检索的实测值，不包含磁盘/网络因素。",
        "- 标注与语料同为合成数据，查询措辞与正文高度对齐，指标天然偏乐观，不能外推到真实业务。",
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run", description="巡检多智能体评测入口")
    parser.add_argument("--suite", choices=["retrieval", "agent"], required=True)
    parser.add_argument("--runner", choices=["scripted", "scripted-tools"], default="scripted")
    parser.add_argument("--k", type=int, default=5)
    parser.add_argument("--out-dir", type=Path, default=REPORTS_DIR)
    args = parser.parse_args(argv)
    start = time.perf_counter()
    if args.suite == "retrieval":
        asyncio.run(run_retrieval_suite(out_dir=args.out_dir, k=args.k))
        return 0
    if args.suite == "agent":
        from evals.agent_eval import run_agent_suite  # C8 提供

        asyncio.run(run_agent_suite(runner_name=args.runner, out_dir=args.out_dir))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
