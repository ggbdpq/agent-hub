"""C9：消融配置矩阵——组合展开正确，一次跑完出对比表（计划 §11-E5/E6 检索侧）。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from evals.ablation import expand_matrix, build_retriever  # noqa: E402
from app.knowledge.bm25 import Bm25Searcher  # noqa: E402
from app.knowledge.retriever import HybridRetriever  # noqa: E402
from app.knowledge.splitting import split_corpus  # noqa: E402
from evals.datasets import corpus_root  # noqa: E402


def test_expand_matrix_default_6_configs() -> None:
    configs = expand_matrix()
    assert len(configs) == 6  # channel(3) × token_mode(2)
    names = [c["name"] for c in configs]
    assert len(set(names)) == len(names), "配置名必须唯一"
    for c in configs:
        assert set(c.keys()) >= {"name", "channel", "token_mode", "rerank"}
        assert c["rerank"] is False  # P2：重排默认关闭，仅接口
    assert {"ablation-hybrid-bigram", "ablation-bm25-only-unigram"} <= set(names)


def test_expand_matrix_subset_axes() -> None:
    configs = expand_matrix(axes={"channel": ["hybrid"]})
    assert len(configs) == 2
    assert all(c["channel"] == "hybrid" for c in configs)
    assert {c["token_mode"] for c in configs} == {"bigram", "unigram"}


def test_build_retriever_maps_channel() -> None:
    chunks = split_corpus(corpus_root())
    hybrid = build_retriever({"channel": "hybrid", "token_mode": "bigram"}, chunks)
    bm25 = build_retriever({"channel": "bm25-only", "token_mode": "bigram"}, chunks)
    vector = build_retriever({"channel": "vector-only", "token_mode": "bigram"}, chunks)
    assert isinstance(hybrid, HybridRetriever)
    assert isinstance(bm25, Bm25Searcher)
    assert vector is not None and not isinstance(vector, HybridRetriever)
