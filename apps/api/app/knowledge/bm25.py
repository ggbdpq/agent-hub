"""BM25 词法检索（C2）：rank_bm25 封装，分数 >0 才返回，不编造结果。

filters（契约 ``Retriever.search`` 的 dict 语义，C5 沿用）：
- ``doc_ids``：list[str]，限定 doc_id；
- ``device_type``：str，按 doc_id 前缀（``-`` 之前）过滤，如 ``pv_inverter``。
"""

from rank_bm25 import BM25Okapi

from app.contracts.knowledge import Chunk
from app.knowledge.tokenize import tokenize


def device_type_of(doc_id: str) -> str:
    return doc_id.split("-", 1)[0]


class Bm25Searcher:
    def __init__(self, chunks: list[Chunk], token_mode: str = "bigram") -> None:
        self._chunks = chunks
        self._mode = token_mode
        self._corpus_tokens = [tokenize(f"{c.title} {c.text}", token_mode) for c in chunks]
        self._bm25 = BM25Okapi(self._corpus_tokens) if chunks else None

    async def search(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        return self.search_sync(query, k, filters)

    def search_sync(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        if self._bm25 is None or k <= 0:
            return []
        allowed = self._filter_mask(filters)
        query_tokens = tokenize(query, self._mode)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(
            (score, idx) for idx, score in enumerate(scores) if score > 0 and allowed[idx]
        )
        return [
            self._chunks[idx].model_copy(update={"score": score})
            for score, idx in sorted(ranked, reverse=True)[:k]
        ]

    def _filter_mask(self, filters: dict | None) -> list[bool]:
        n = len(self._chunks)
        if not filters:
            return [True] * n
        doc_ids = filters.get("doc_ids")
        device_type = filters.get("device_type")
        mask = [True] * n
        for i, chunk in enumerate(self._chunks):
            if doc_ids is not None and chunk.doc_id not in set(doc_ids):
                mask[i] = False
            if device_type is not None and device_type_of(chunk.doc_id) != device_type:
                mask[i] = False
        return mask
