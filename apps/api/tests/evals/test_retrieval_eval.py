"""C6：检索评测指标——手算样例锁定 Recall@k / MRR / 分位数语义。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402

from evals.metrics import mrr, percentile, precision_recall, recall_at_k  # noqa: E402
from evals.retrieval_eval import run_retrieval_eval  # noqa: E402
from app.knowledge.embedding import HashingEmbedder  # noqa: E402
from app.knowledge.retriever import HybridRetriever  # noqa: E402
from app.knowledge.splitting import split_corpus  # noqa: E402
from evals.datasets import corpus_root, load_retrieval_annotations  # noqa: E402


def test_recall_at_k_hand_computed() -> None:
    ranked = ["a", "x", "b", "c"]
    assert recall_at_k(ranked, {"b"}, 1) == 0.0
    assert recall_at_k(ranked, {"b"}, 3) == 1.0
    assert recall_at_k(ranked, {"a", "b"}, 1) == 0.5
    assert recall_at_k(ranked, {"a", "b"}, 3) == 1.0
    assert recall_at_k([], {"a"}, 3) == 0.0


def test_mrr_hand_computed() -> None:
    assert mrr(["a", "x", "b"], {"a"}) == 1.0
    assert mrr(["x", "a", "b"], {"a"}) == pytest.approx(1 / 2)
    assert mrr(["x", "y", "a"], {"a"}) == pytest.approx(1 / 3)
    assert mrr(["x", "y", "z"], {"a"}) == 0.0
    assert mrr(["x", "a", "y", "b"], {"a", "b"}) == pytest.approx(1 / 2)  # 首个命中


def test_precision_recall_sets() -> None:
    p, r = precision_recall({"a", "b"}, {"a"})
    assert p == pytest.approx(0.5)
    assert r == pytest.approx(1.0)
    p, r = precision_recall(set(), {"a"})
    assert p == 0.0 and r == 0.0


def test_percentile_linear_interpolation() -> None:
    assert percentile(list(range(1, 101)), 50) == pytest.approx(50.5)
    assert percentile(list(range(1, 101)), 95) == pytest.approx(95.05)
    assert percentile([], 50) == 0.0


async def test_run_retrieval_eval_over_corpus() -> None:
    retriever = HybridRetriever(split_corpus(corpus_root()), embedder=HashingEmbedder(dim=512))
    cases = [
        (q["qid"], q["query"], set(q["relevant_chunk_ids"]))
        for q in load_retrieval_annotations()["queries"]
    ]
    report = await run_retrieval_eval(retriever, cases)
    assert len(report.per_query) == 60
    agg = report.aggregates
    # 确定性检索器 + 关键词对齐标注：管道健康下限（2026-10-24 实测 recall@3=0.79/@5=0.91），非模型效果声明
    assert agg["recall_at_3"] >= 0.75, f"recall@3 过低：{agg}"
    assert agg["recall_at_5"] >= 0.88, f"recall@5 过低：{agg}"
    assert agg["recall_at_5"] >= agg["recall_at_3"] >= agg["recall_at_1"]
    assert agg["mrr"] > 0.6
    assert agg["latency_ms_mean"] >= 0
    assert agg["queries"] == 60
