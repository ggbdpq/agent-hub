"""HybridRetriever（C5）：BM25 + 向量双路召回 → RRF 融合 → 可选重排，实现契约 Retriever。

- 双路各召回 ``channel_results``（默认 20）个候选，RRF（k=60，C4）融合后取前 k；
- 无相关内容 → 空列表，不编造（BM25 零分、向量低于 min_sim 的候选不进入融合池）；
- 重排器默认 None=关闭（E6 消融留位）；传入 ``token_mode`` 可切换分词（U4 对比）。
"""

from app.contracts.knowledge import Chunk, Retriever
from app.knowledge.bm25 import Bm25Searcher
from app.knowledge.embedding import Embedder
from app.knowledge.fuse import Reranker, rrf_fuse
from app.knowledge.vector import VectorSearcher


class HybridRetriever(Retriever):
    def __init__(
        self,
        chunks: list[Chunk],
        embedder: Embedder,
        rrf_k: int = 60,
        channel_results: int = 20,
        reranker: Reranker | None = None,
        min_sim: float = 0.05,
        token_mode: str = "bigram",
    ) -> None:
        self._bm25 = Bm25Searcher(chunks, token_mode=token_mode)
        self._vector = VectorSearcher(chunks, embedder, min_sim=min_sim)
        self._rrf_k = rrf_k
        self._channel_results = channel_results
        self.reranker = reranker

    async def search(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        recall = max(k, self._channel_results)
        bm25_hits = await self._bm25.search(query, recall, filters)
        vector_hits = await self._vector.search(query, recall, filters)
        if not bm25_hits and not vector_hits:
            return []  # 两路全空：无相关内容，不编造
        fused = rrf_fuse([bm25_hits, vector_hits], k=self._rrf_k)
        if self.reranker is not None:
            fused = await self.reranker.rerank(query, fused, k)
            return fused
        return fused[:k]
