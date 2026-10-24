"""评测入口（C6/C8/C11）：``python -m evals.run --suite retrieval|agent``。

报告统一经 evals.report 生成（方法/样本量/方差/已知偏差/离线标注必备段），
落 evals/reports/retrieval-*.md|json 与 agent-<runner>-*.md|json。
"""

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from evals.datasets import corpus_root, load_retrieval_annotations
from evals.retrieval_eval import annotations_to_cases, run_retrieval_eval
from evals.report import finalize, write_report
from app.knowledge.bm25 import Bm25Searcher
from app.knowledge.embedding import HashingEmbedder
from app.knowledge.retriever import HybridRetriever
from app.knowledge.splitting import split_corpus

DISCLAIMER = (
    "合成语料 + 人工标注的离线评测：量化的是确定性检索管道在本标注集上的表现，"
    "仅验证评测流程，不代表真实业务检索效果，不代表模型效果。"
)

REPORTS_DIR = Path(__file__).resolve().parent / "reports"


class _VectorOnly:
    """仅向量单路（E5 消融基线）：实现契约 Retriever 签名。"""

    def __init__(self, chunks, embedder) -> None:
        from app.knowledge.vector import VectorSearcher

        self._inner = VectorSearcher(chunks, embedder, min_sim=0.05)

    async def search(self, query: str, k: int, filters: dict | None = None) -> list:
        return await self._inner.search(query, k, filters)


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
            f"@5={agg['recall_at_5']:.3f}±{agg['recall_at_5_std']:.3f} mrr={agg['mrr']:.3f} "
            f"p95={agg['latency_ms_p95']:.2f}ms"
        )

    best_recall5 = max(results, key=lambda v: v["aggregates"]["recall_at_5"])
    n_chunks = len(split_corpus(corpus_root()))
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    payload = finalize(
        {
            "suite": "retrieval",
            "title": "检索评测报告（C6）",
            "meta": [
                ("语料", f"30 篇合成规程 / {n_chunks} chunks"),
                ("标注", f"{len(cases)} 条（问题 → 相关 chunk_id，人工指定）"),
                ("最优变体（按 Recall@5）", f"{best_recall5['name']}（{best_recall5['aggregates']['recall_at_5']:.3f}）"),
            ],
            "method": [
                "对每条标注逐条计时检索（k=5），计算 Recall@1/3/5（宏平均）与 MRR。",
                "耗时用 time.perf_counter 实测（单进程内存检索，不含磁盘/网络因素）。",
                "四个变体：混合（BM25+向量 RRF）× bigram/unigram、BM25 单路、向量单路。",
            ],
            "sample_size": {"queries": len(cases), "chunks": n_chunks, "variants": len(results)},
            "variance": {
                "hybrid-bigram recall_at_5_std": round(results[0]["aggregates"]["recall_at_5_std"], 4),
                "hybrid-bigram mrr_std": round(results[0]["aggregates"]["mrr_std"], 4),
                "hybrid-bigram latency_ms_std": round(results[0]["aggregates"]["latency_ms_std"], 4),
            },
            "known_biases": [
                "标注与语料同为合成数据，查询措辞与正文高度对齐，指标天然偏乐观，不能外推到真实业务。",
                "标注为单人指定（60 条、每条 1~2 个相关 chunk），无多人交叉一致性检验。",
                "向量通道使用 HashingEmbedder（无真实语义），其对混合检索的贡献待真实 embedding API 复核（E5）。",
            ],
            "disclaimer": DISCLAIMER,
            "variants": [{"name": v["name"], "aggregates": v["aggregates"]} for v in results],
            "tables": [
                {
                    "caption": "变体对比",
                    "header": ["变体", "Recall@1", "Recall@3", "Recall@5", "Recall@5±σ", "MRR", "P50 (ms)", "P95 (ms)", "空结果率"],
                    "rows": [
                        [
                            v["name"],
                            f"{v['aggregates']['recall_at_1']:.3f}",
                            f"{v['aggregates']['recall_at_3']:.3f}",
                            f"{v['aggregates']['recall_at_5']:.3f}",
                            f"±{v['aggregates']['recall_at_5_std']:.3f}",
                            f"{v['aggregates']['mrr']:.3f}",
                            f"{v['aggregates']['latency_ms_p50']:.2f}",
                            f"{v['aggregates']['latency_ms_p95']:.2f}",
                            f"{v['aggregates']['empty_rate']:.1%}",
                        ]
                        for v in results
                    ],
                }
            ],
            "sections": [
                {
                    "caption": "分词方案结论（U4）",
                    "bullets": [
                        f"按 Recall@5：最优变体 **{best_recall5['name']}**（{best_recall5['aggregates']['recall_at_5']:.3f}）。",
                        "jieba 受依赖限制未安装（见 docs/integration-requests/C.md），对比在 bigram vs unigram 之间完成；",
                        "方案随最优变体确定，接真实 embedding 后需复核混合检索的构成（E5 消融）。",
                    ],
                }
            ],
        },
        generated_at=datetime.now(UTC).isoformat(),
    )
    md_path, json_path = write_report(payload, out_dir, f"retrieval-{stamp}")
    print(f"[retrieval] 报告：{md_path.name} / {json_path.name}")
    return payload


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
        from evals.agent_eval import run_agent_suite

        asyncio.run(run_agent_suite(runner_name=args.runner, out_dir=args.out_dir))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
