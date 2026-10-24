"""向量检索（C3）：numpy 余弦相似度，仅返回相似度 >= min_sim 且 >0 的结果，不编造。"""

import numpy as np

from app.contracts.knowledge import Chunk
from app.knowledge.bm25 import device_type_of
from app.knowledge.embedding import Embedder


class VectorSearcher:
    def __init__(self, chunks: list[Chunk], embedder: Embedder, min_sim: float = 0.05) -> None:
        self._chunks = chunks
        self._embedder = embedder
        self._min_sim = min_sim
        self._matrix = embedder.embed_documents([c.text for c in chunks]) if chunks else None

    async def search(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        return self.search_sync(query, k, filters)

    def search_sync(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        if self._matrix is None or k <= 0 or not len(self._chunks):
            return []
        q = self._embedder.embed_query(query)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return []
        q = q / q_norm
        norms = np.linalg.norm(self._matrix, axis=1)
        norms[norms == 0] = 1.0  # 零向量（无 token）相似度按 0 处理
        sims = (self._matrix / norms[:, None]) @ q
        allowed = self._filter_mask(filters)
        candidates = [
            (float(sims[i]), i)
            for i in range(len(self._chunks))
            if sims[i] >= self._min_sim and sims[i] > 0 and allowed[i]
        ]
        candidates.sort(key=lambda pair: (-pair[0], pair[1]))  # 分数降序，同分按索引稳定
        return [
            self._chunks[i].model_copy(update={"score": s})
            for s, i in candidates[:k]
        ]

    def embed_documents(self, texts: list[str]) -> "np.ndarray":
        return self._embedder.embed_documents(texts)

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
