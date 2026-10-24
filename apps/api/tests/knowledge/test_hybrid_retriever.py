"""C5：HybridRetriever 实现 Retriever 契约——带分数与来源路径，空结果不编造。

验收点（计划 7.3-C5）：返回带分数与来源路径的 Chunk；无相关内容的问题返回空列表；
重排器默认关闭。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402

from app.contracts.knowledge import Chunk, Retriever  # noqa: E402
from app.knowledge.embedding import HashingEmbedder  # noqa: E402
from app.knowledge.retriever import HybridRetriever  # noqa: E402
from app.knowledge.splitting import split_corpus  # noqa: E402
from evals.datasets import corpus_root  # noqa: E402


def _build() -> HybridRetriever:
    return HybridRetriever(split_corpus(corpus_root()), embedder=HashingEmbedder(dim=512))


async def test_implements_retriever_contract() -> None:
    retriever: Retriever = _build()  # 协议一致性：签名 search(query, k, filters)
    results = await retriever.search("光伏逆变器柜温超过多少要立即上报", k=5)
    assert results
    assert all(isinstance(c, Chunk) for c in results)
    assert all(c.score > 0 for c in results)
    assert all(c.source_path for c in results)


async def test_hybrid_beats_single_channel_head() -> None:
    retriever = _build()
    results = await retriever.search("桥梁传感器位移超过多少毫米要立即上报", k=5)
    assert any("bridge_sensor-thresholds#桥梁监测传感异常判据与告警阈值/告警阈值" in c.chunk_id for c in results)


async def test_no_match_returns_empty_list() -> None:
    retriever = _build()
    assert await retriever.search("红烧肉炖土豆怎么做才软烂入味", k=5) == []


async def test_filters_forwarded_to_channels() -> None:
    retriever = _build()
    results = await retriever.search(
        "告警阈值 立即上报", k=10, filters={"device_type": "wind_turbine"}
    )
    assert results
    assert all(c.doc_id.startswith("wind_turbine") for c in results)


async def test_reranker_disabled_by_default() -> None:
    retriever = _build()
    assert retriever.reranker is None  # 默认关闭（E6 消融留位）


async def test_reranker_applied_when_configured() -> None:
    class SortedReranker:
        """把候选池按 chunk_id 降序重排：其输出顺序必须被 retriever 原样保留。"""

        def __init__(self) -> None:
            self.called = False
            self.pool_size = 0

        async def rerank(self, query: str, chunks: list[Chunk], top_n: int) -> list[Chunk]:
            self.called = True
            self.pool_size = len(chunks)
            return sorted(chunks, key=lambda c: c.chunk_id, reverse=True)[:top_n]

    reranker = SortedReranker()
    retriever = HybridRetriever(
        split_corpus(corpus_root()), embedder=HashingEmbedder(dim=512), reranker=reranker
    )
    results = await retriever.search("风机 振动 阈值", k=5)
    assert reranker.called
    assert reranker.pool_size >= 5, "重排器应收到融合候选池而非截断后的 top-k"
    assert [c.chunk_id for c in results] == sorted(
        (c.chunk_id for c in results), reverse=True
    ), "检索结果必须保持重排器给出的顺序"
