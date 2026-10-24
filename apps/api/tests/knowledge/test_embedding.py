"""C3：Embedder——离线 HashingEmbedder 确定性 + OpenAI 兼容（可选，不发真实网络请求）。

验收点（计划 7.3-C3）：离线嵌入确定性；向量存 numpy 数组 + 余弦相似度。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

from app.knowledge.embedding import HashingEmbedder, OpenAIEmbedder  # noqa: E402
from app.knowledge.splitting import split_corpus  # noqa: E402
from app.knowledge.vector import VectorSearcher  # noqa: E402
from evals.datasets import corpus_root  # noqa: E402


def test_hashing_embedder_deterministic() -> None:
    e1, e2 = HashingEmbedder(dim=256), HashingEmbedder(dim=256)
    v1 = e1.embed_query("风机振动阶跃越限怎么判定")
    v2 = e2.embed_query("风机振动阶跃越限怎么判定")
    assert v1.shape == (256,)
    assert np.array_equal(v1, v2), "不同实例对同一文本必须产出逐位相同的向量"
    assert np.isclose(np.linalg.norm(v1), 1.0), "向量应 L2 归一化"


def test_related_scores_higher_than_disjoint() -> None:
    embedder = HashingEmbedder(dim=512)
    chunk_text = "风机振动超过 7.1 mm/s 立即上报并启动异常处理流程"
    v_chunk = embedder.embed_query(chunk_text)

    def cos(t: str) -> float:
        return float(embedder.embed_query(t) @ v_chunk)

    related = cos("振动超过多少要立即上报")
    disjoint = cos("红烧肉炖土豆怎么做才软烂入味")
    assert related > 0.1, f"相关文本相似度过低：{related}"
    assert disjoint < 0.05, f"无关文本相似度过高（哈希碰撞泄漏）：{disjoint}"
    assert related > disjoint


def test_vector_searcher_top_k_and_min_sim_floor() -> None:
    embedder = HashingEmbedder(dim=512)
    chunks = split_corpus(corpus_root())
    searcher = VectorSearcher(chunks, embedder, min_sim=0.05)
    hits = searcher.search_sync("桥梁监测传感位移超过多少要立即上报", k=5)
    assert hits, "相关查询必须命中"
    assert all(c.score >= 0.05 for c in hits)
    scores = [c.score for c in hits]
    assert scores == sorted(scores, reverse=True)
    assert len(hits) <= 5
    assert any("告警阈值" in c.chunk_id for c in hits), "语义通道应召回告警阈值节"
    # 无关查询：全部低于相似度下限 → 空列表（不编造）
    assert searcher.search_sync("红烧肉炖土豆怎么做才软烂入味", k=5) == []


def test_vector_searcher_batch_matches_single() -> None:
    embedder = HashingEmbedder(dim=256)
    chunks = split_corpus(corpus_root())[:10]
    searcher = VectorSearcher(chunks, embedder, min_sim=0.0)
    texts = ["风机振动阈值", "桥梁位移阈值"]
    batch = searcher.embed_documents([t for t in texts])
    for i, t in enumerate(texts):
        assert np.allclose(batch[i], embedder.embed_query(t)), "批量嵌入必须与单条一致"


class _FakeTransport:
    """记录请求并回放固定 embedding 响应。"""

    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def __call__(self, url: str, payload: dict, headers: dict) -> dict:
        self.payloads.append({"url": url, "payload": payload, "headers": headers})
        inputs = payload["input"]
        return {
            "data": [{"embedding": [1.0, 0.0, 0.0] if i % 2 == 0 else [0.0, 1.0, 0.0]} for i in range(len(inputs))],
            "usage": {"prompt_tokens": 10, "total_tokens": 10},
        }


def test_openai_embedder_payload_and_batching() -> None:
    transport = _FakeTransport()
    embedder = OpenAIEmbedder(
        endpoint="https://example.invalid/v1/embeddings",
        api_key="sk-test",
        model="text-embedding-test",
        batch_size=2,
        transport=transport,
    )
    vecs = embedder.embed_documents(["甲文本", "乙文本", "丙文本"])
    assert vecs.shape == (3, 3)
    # batch_size=2 → 两次请求，第二个请求补齐余量
    assert len(transport.payloads) == 2
    assert transport.payloads[0]["payload"]["model"] == "text-embedding-test"
    assert transport.payloads[0]["payload"]["input"] == ["甲文本", "乙文本"]
    assert transport.payloads[1]["payload"]["input"] == ["丙文本"]
    assert transport.payloads[0]["headers"]["Authorization"] == "Bearer sk-test"
    assert np.allclose(vecs[0], [1.0, 0.0, 0.0])


def test_openai_embedder_from_env_requires_config(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("OPENAI_BASE_URL", "OPENAI_API_KEY", "EMBEDDING_MODEL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(RuntimeError, match="EMBEDDING_MODEL"):
        OpenAIEmbedder.from_env()
