"""C0：检索评测标注数据集校验——每条标注引用的 chunk 必须真实存在。

存在性判定（chunk_id = doc_id#标题路径#序号）：
1. doc_id 对应语料目录中的 .md 文件；
2. 标题路径逐级对应该文档的标题层级（第 1 段 = H1，第 2 段 = H2）；
3. 序号为正整数（同标题路径下第 n 块）。

语料为合成数据（fixtures/inspection/README.md 口径），标注供离线检索评测使用。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))  # 允许从任意 cwd 运行 pytest 时导入 evals 包

from evals.datasets import corpus_root, load_retrieval_annotations  # noqa: E402
from app.knowledge.chunk_id import parse_chunk_id  # noqa: E402


def _doc_headings(corpus_dir: Path, doc_id: str) -> list[tuple[int, str]]:
    """返回文档 (级别, 标题文本) 列表，级别取 # 个数。"""
    levels: list[tuple[int, str]] = []
    for line in (corpus_dir / f"{doc_id}.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            text = line.lstrip("#").strip()
            levels.append((len(line) - len(line.lstrip("#")), text))
    return levels


def test_annotations_has_60_entries_with_unique_qid() -> None:
    data = load_retrieval_annotations()
    queries = data["queries"]
    assert len(queries) == 60, f"标注应为 60 条，实际 {len(queries)}"
    qids = [q["qid"] for q in queries]
    assert len(set(qids)) == len(qids), "qid 必须唯一"
    assert all(q["query"].strip() for q in queries), "问题文本非空"
    assert all(q["relevant_chunk_ids"] for q in queries), "每条至少一个相关 chunk"


def test_every_relevant_chunk_exists() -> None:
    data = load_retrieval_annotations()
    corpus_dir = corpus_root()
    problems: list[str] = []
    for q in data["queries"]:
        for cid in q["relevant_chunk_ids"]:
            ref = parse_chunk_id(cid)
            doc_id, path, seq = ref.doc_id, ref.heading_path, ref.seq
            doc_file = corpus_dir / f"{doc_id}.md"
            if not doc_file.exists():
                problems.append(f"{q['qid']}: 文档不存在 {doc_id}")
                continue
            headings = _doc_headings(corpus_dir, doc_id)
            # 逐级核对标题路径：第 i 段（0 基）必须是级别 i+1 的标题
            parts = path.split("/")
            for depth, part in enumerate(parts, start=1):
                if (depth, part) not in headings:
                    problems.append(f"{q['qid']}: {cid} 缺少级别 {depth} 标题「{part}」")
            if seq < 1:
                problems.append(f"{q['qid']}: 序号必须从 1 起，得到 {seq}")
    assert not problems, "标注引用了不存在的 chunk：\n" + "\n".join(problems)


def test_annotations_cover_all_five_device_types() -> None:
    data = load_retrieval_annotations()
    docs = {parse_chunk_id(cid).doc_id for q in data["queries"] for cid in q["relevant_chunk_ids"]}
    prefixes = {d.split("-", 1)[0] for d in docs}
    assert prefixes == {
        "pv_inverter",
        "wind_turbine",
        "storage_pcs",
        "substation_bay",
        "bridge_sensor",
    }, f"标注应覆盖全部 5 类设备语料，实际 {prefixes}"
