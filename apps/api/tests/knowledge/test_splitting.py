"""C1：按标题层级切分——chunk_id 稳定性与标注可解析性。

验收点（计划 7.3-C1）：
- chunk_id = doc_id#标题路径#序号，内容不变则 ID 不变（重复切分 ID 稳定）；
- 块大小与重叠可配置；
- 全部 60 条检索标注能解析到真实 Chunk（各引用恰好命中一个 chunk）。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from evals.datasets import corpus_root, load_retrieval_annotations  # noqa: E402
from app.knowledge.chunk_id import parse_chunk_id  # noqa: E402
from app.knowledge.splitting import (  # noqa: E402
    SplitConfig,
    load_corpus_docs,
    split_document,
)

DOC = """# 示例规程

> 合成数据文档。

适用对象：示例设备。

## 第一节
第一段正文。

## 第二节
- 要点一
- 要点二

## 修订记录
- 版本 1.0
"""


def _index(chunks) -> dict:
    return {c.chunk_id: c for c in chunks}


def test_split_is_deterministic_ids_stable() -> None:
    docs = load_corpus_docs(corpus_root())
    first = [c for d in docs for c in split_document(d)]
    second = [c for d in docs for c in split_document(d)]
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
    assert len(first) == len(second)


def test_corpus_yields_30_docs_with_expected_sections() -> None:
    docs = load_corpus_docs(corpus_root())
    assert len(docs) == 30
    doc = next(d for d in docs if d.doc_id == "pv_inverter-thresholds")
    chunks = {c.title: c for c in split_document(doc)}
    assert "告警阈值" in chunks
    chunk = chunks["告警阈值"]
    assert chunk.chunk_id == "pv_inverter-thresholds#光伏逆变器异常判据与告警阈值/告警阈值#1"
    assert "> 85" in chunk.text  # 告警阈值正文随块保留
    assert chunk.source_path.endswith("fixtures/inspection/corpus/pv_inverter-thresholds.md")
    # 标题路径作为前缀出现在块文本中（供词法/向量通道匹配标题词）
    assert chunk.text.startswith("[光伏逆变器异常判据与告警阈值/告警阈值]")


def test_long_section_splits_by_max_chars_with_overlap() -> None:
    doc = type(load_corpus_docs(corpus_root())[0])(
        doc_id="long-doc",
        source_path="fixtures/inspection/corpus/long-doc.md",
        text="# 长文档\n\n## 长节\n" + "\n".join(f"第{i}行内容。" for i in range(200)) + "\n",
    )
    cfg = SplitConfig(max_chars=200, overlap=50)
    chunks = split_document(doc, cfg)
    assert len(chunks) >= 3
    seqs = sorted(parse_chunk_id(c.chunk_id).seq for c in chunks)
    assert seqs == list(range(1, len(chunks) + 1))  # 同一路径序号连续从 1 起
    tail_line = chunks[0].text.splitlines()[-1]
    assert any(tail_line in c.text for c in chunks[1:]), "相邻块应有重叠内容"


def test_annotations_resolve_to_real_chunks() -> None:
    docs = {d.doc_id: d for d in load_corpus_docs(corpus_root())}
    index: dict[str, int] = {}
    for d in docs.values():
        for c in split_document(d):
            index[c.chunk_id] = index.get(c.chunk_id, 0) + 1
    missing: list[str] = []
    for q in load_retrieval_annotations()["queries"]:
        for cid in q["relevant_chunk_ids"]:
            n = index.get(cid, 0)
            if n != 1:
                missing.append(f"{q['qid']}: {cid} 命中 {n} 次")
    assert not missing, "标注应恰好命中一个 chunk：\n" + "\n".join(missing)
