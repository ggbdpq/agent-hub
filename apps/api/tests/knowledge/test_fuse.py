"""C4：RRF 倒数排名融合（k=60）与重排器接口（默认关闭）。

验收点（计划 7.3-C4）：融合结果包含两路头部结果；重排器只是接口，默认不启用。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402

from app.contracts.knowledge import Chunk  # noqa: E402
from app.knowledge.fuse import Reranker, rrf_fuse  # noqa: E402


def _chunk(cid: str) -> Chunk:
    return Chunk(chunk_id=cid, doc_id=cid.split("#")[0], title=cid, text=cid, source_path="x.md")


def test_rrf_hand_computed_scores_and_order() -> None:
    # 手算（rank 从 1 起）：A=[a,b,c]，B=[b,d]
    # b = 1/61 + 1/62 = 0.032519…；a = 1/61 = 0.016393…；d = 1/62 = 0.016129…；c = 1/63 = 0.015873…
    fused = rrf_fuse([[_chunk("a"), _chunk("b"), _chunk("c")], [_chunk("b"), _chunk("d")]], k=60)
    assert [c.chunk_id for c in fused] == ["b", "a", "d", "c"]
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert fused[1].score == pytest.approx(1 / 61)
    assert fused[2].score == pytest.approx(1 / 62)
    assert fused[3].score == pytest.approx(1 / 63)


def test_rrf_contains_head_of_both_channels() -> None:
    a_head, b_head = _chunk("docA#x#1"), _chunk("docB#y#1")
    others_a = [_chunk(f"docA#{i}#1") for i in range(10)]
    others_b = [_chunk(f"docB#{i}#1") for i in range(10)]
    fused = rrf_fuse([[a_head, *others_a], [b_head, *others_b]], k=60)
    top_ids = {c.chunk_id for c in fused[:2]}
    assert {a_head.chunk_id, b_head.chunk_id} == top_ids, "两路头部都应在融合前两名"


def test_rrf_deduplicates_by_chunk_id() -> None:
    fused = rrf_fuse([[_chunk("a"), _chunk("a")]], k=60)
    assert [c.chunk_id for c in fused] == ["a"]
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)  # 重复出现按名次各计一次，只输出一条


def test_reranker_protocol_default_off_shape() -> None:
    class _Noop:
        async def rerank(self, query: str, chunks: list[Chunk], top_n: int) -> list[Chunk]:
            return chunks[:top_n]

    reranker: Reranker = _Noop()  # 协议可被普通类实现；C5 中默认 None=关闭
    assert reranker is not None
