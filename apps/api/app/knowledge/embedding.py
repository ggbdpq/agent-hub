"""嵌入（C3）：离线确定性 HashingEmbedder（CI/离线评测）+ OpenAI 兼容 Embedder（可选）。

- HashingEmbedder：token 级特征哈希（MD5 派生桶与符号），L2 归一化。无模型、无随机源，
  同文本逐位可复现——离线评测与 CI 的唯一嵌入实现；
- OpenAIEmbedder：OpenAI 兼容 ``/v1/embeddings``，供真实评测报告使用；测试经注入
  transport 替身，本仓库不持有任何真实密钥（令牌只经环境变量流转）。
"""

import hashlib
import json
import os
import urllib.request
from typing import Callable, Protocol

import numpy as np

from app.knowledge.tokenize import tokenize

_Transport = Callable[[str, dict, dict], dict]


class Embedder(Protocol):
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> "np.ndarray":
        """返回 (n, dim) 矩阵，逐行 L2 归一化。"""

    def embed_query(self, text: str) -> "np.ndarray": ...


class HashingEmbedder:
    """特征哈希嵌入：md5(token) 高位选桶、次位定符号，出现次数累加后归一化。

    参数 dim 默认 512：合成语料约 170 个 chunk、每次查询 token 数 < 60，
    桶间碰撞导致的无关文本相似度实测 < 0.05（见 tests/knowledge/test_embedding.py）。
    """

    def __init__(self, dim: int = 512, token_mode: str = "bigram") -> None:
        if dim < 8:
            raise ValueError("dim 至少 8，避免哈希碰撞不可用")
        self._dim = dim
        self._mode = token_mode

    def dim(self) -> int:
        return self._dim

    def embed_documents(self, texts: list[str]) -> "np.ndarray":
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        return np.stack([self._embed_one(t) for t in texts])

    def embed_query(self, text: str) -> "np.ndarray":
        return self._embed_one(text)

    def _embed_one(self, text: str) -> "np.ndarray":
        vec = np.zeros(self._dim, dtype=np.float32)
        for token in tokenize(text, self._mode):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            bucket = int.from_bytes(digest[:4], "little") % self._dim
            sign = 1.0 if digest[8] & 1 else -1.0
            vec[bucket] += sign
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec


class OpenAIEmbedder:
    """OpenAI 兼容嵌入端点客户端（可选启用，离线评测不用）。"""

    def __init__(
        self,
        endpoint: str,
        api_key: str,
        model: str,
        batch_size: int = 64,
        transport: _Transport | None = None,
    ) -> None:
        if not endpoint or not model:
            raise ValueError("endpoint 与 model 必填")
        self._endpoint = endpoint
        self._api_key = api_key
        self._model = model
        self._batch_size = max(1, batch_size)
        self._transport = transport or self._http_post
        self._dim: int | None = None

    @classmethod
    def from_env(cls) -> "OpenAIEmbedder":
        """从 OPENAI_BASE_URL / OPENAI_API_KEY / EMBEDDING_MODEL 构建。"""
        base = os.environ.get("OPENAI_BASE_URL")
        key = os.environ.get("OPENAI_API_KEY")
        model = os.environ.get("EMBEDDING_MODEL")
        if not (base and key and model):
            missing = [k for k, v in (("OPENAI_BASE_URL", base), ("OPENAI_API_KEY", key), ("EMBEDDING_MODEL", model)) if not v]
            raise RuntimeError(f"OpenAI 兼容嵌入未配置，缺少环境变量：{', '.join(missing)}")
        return cls(endpoint=base.rstrip("/") + "/embeddings", api_key=key, model=model)

    def dim(self) -> int:
        if self._dim is None:
            raise RuntimeError("dim 未知：先调用 embed_documents/embed_query")
        return self._dim

    def embed_documents(self, texts: list[str]) -> "np.ndarray":
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._batch_size):
            vectors.extend(self._embed_batch(texts[i : i + self._batch_size]))
        arr = np.asarray(vectors, dtype=np.float32)
        return arr.reshape(len(texts), -1) if len(texts) else np.zeros((0, self._dim or 0), dtype=np.float32)

    def embed_query(self, text: str) -> "np.ndarray":
        return self.embed_documents([text])[0]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self._model, "input": texts}
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        response = self._transport(self._endpoint, payload, headers)
        data = response["data"]
        if len(data) != len(texts):
            raise ValueError(f"嵌入返回条数不符：请求 {len(texts)}，返回 {len(data)}")
        vectors = [d["embedding"] for d in data]
        self._dim = len(vectors[0])
        return vectors

    @staticmethod
    def _http_post(url: str, payload: dict, headers: dict) -> dict:
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
