"""混合检索融合（C4）：RRF 倒数排名融合（k=60）+ 可选重排器接口（默认关闭）。

RRF：``score = Σ 1/(k + rank)``，rank 为该 chunk 在某一路中的名次（1 起）。
只依赖名次不依赖原始分数量纲，是 BM25 分数与余弦相似度融合的标准做法（计划 §7.3-C4）。
同分按 chunk_id 字典序稳定排序，保证评测可复现。

重排器只定义接口（P2）：真实 rerank API 评测时再接，HybridRetriever 默认 None=关闭（E6）。
"""

from typing import Protocol

from app.contracts.knowledge import Chunk


def rrf_fuse(rankings: list[list[Chunk]], k: int = 60, top_n: int | None = None) -> list[Chunk]:
    """融合多路已排序结果；同 chunk_id 跨路去重，分数累加，按分数降序输出。"""
    if k < 1:
        raise ValueError("RRF 的 k 必须 >= 1")
    scores: dict[str, float] = {}
    chunks: dict[str, Chunk] = {}
    for ranking in rankings:
        for rank, chunk in enumerate(ranking, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            chunks.setdefault(chunk.chunk_id, chunk)
    ordered = sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
    limited = ordered if top_n is None else ordered[:top_n]
    return [chunks[cid].model_copy(update={"score": score}) for cid, score in limited]


class Reranker(Protocol):
    """可选重排器：对融合后的候选按查询相关性重排（默认关闭，评测时接兼容 API）。"""

    async def rerank(self, query: str, chunks: list[Chunk], top_n: int) -> list[Chunk]: ...
