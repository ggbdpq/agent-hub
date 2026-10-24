"""检索契约：Retriever 与证据块 Chunk（C 实现 HybridRetriever，B 消费）。"""

from typing import Protocol

from pydantic import BaseModel


class Chunk(BaseModel):
    chunk_id: str  # doc_id#标题路径#序号；内容不变则 ID 不变（证据引用稳定）
    doc_id: str
    title: str
    text: str
    score: float = 0.0
    source_path: str


class Retriever(Protocol):
    async def search(self, query: str, k: int, filters: dict | None = None) -> list[Chunk]:
        """无相关内容返回空列表，不编造（C5）。"""
