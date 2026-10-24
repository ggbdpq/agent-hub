"""按标题层级切分 Markdown 语料（C1）。

- 每个"标题路径"下的正文成为一个逻辑节；节体超过 ``max_chars`` 时按行滑动窗口再切，
  相邻块带 ``overlap`` 字符级重叠，同一标题路径下序号 1..n 连续递增；
- chunk_id = doc_id#标题路径#序号（``chunk_id.format_chunk_id``），**内容不变则 ID 不变**——
  切分是纯函数：相同 (文档文本, 配置) 必然产出相同 ID 序列；
- 块文本以 ``[标题路径]`` 行为前缀，标题词可被词法与向量通道匹配。

块大小默认 800 / 重叠 100：合成语料的节均远小于该值（实测全部单块），
阈值只为长节兜底，调大调小不改变既有短节 ID。
"""

from dataclasses import dataclass
from pathlib import Path

from app.contracts.knowledge import Chunk
from app.knowledge.chunk_id import format_chunk_id

_REPO_ROOT = Path(__file__).resolve().parents[3]  # apps/api/app/knowledge/splitting.py → 仓库根


@dataclass(frozen=True)
class SplitConfig:
    max_chars: int = 800
    overlap: int = 100

    def __post_init__(self) -> None:
        if self.max_chars < 1:
            raise ValueError("max_chars 必须 >= 1")
        if not 0 <= self.overlap < self.max_chars:
            raise ValueError("overlap 必须在 [0, max_chars) 内")


@dataclass(frozen=True)
class CorpusDoc:
    doc_id: str
    source_path: str
    text: str


def load_corpus_docs(corpus_dir: Path) -> list[CorpusDoc]:
    """读取语料目录全部 .md（按文件名排序，保证确定性）。"""
    docs: list[CorpusDoc] = []
    for file in sorted(corpus_dir.glob("*.md")):
        try:
            source = str(file.relative_to(_REPO_ROOT))
        except ValueError:
            source = str(file)
        docs.append(CorpusDoc(doc_id=file.stem, source_path=source, text=file.read_text(encoding="utf-8")))
    return docs


def _sections(text: str) -> list[tuple[str, str]]:
    """解析 (标题路径, 节体)。标题路径从 H1 起、以 / 连接；首个标题前的导语归入 H1 节体之前忽略。"""
    stack: list[tuple[int, str]] = []
    sections: list[tuple[str, str]] = []
    body: list[str] = []
    started = False

    def flush() -> None:
        if started and stack:
            joined = "\n".join(body).strip()
            if joined:
                sections.append(("/".join(t for _, t in stack), joined))
        body.clear()

    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#") and " " in stripped:
            level = len(stripped) - len(stripped.lstrip("#"))
            heading = stripped[level:].strip()
            if not heading:
                continue
            flush()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, heading))
            started = True
            continue
        if started:
            body.append(line)
    flush()
    return sections


def split_document(doc: CorpusDoc, config: SplitConfig | None = None) -> list[Chunk]:
    cfg = config or SplitConfig()
    chunks: list[Chunk] = []
    seqs: dict[str, int] = {}
    for path, body in _sections(doc.text):
        seq = seqs.get(path, 1)
        for window in _windows(body, cfg):
            chunks.append(
                Chunk(
                    chunk_id=format_chunk_id(doc.doc_id, path, seq),
                    doc_id=doc.doc_id,
                    title=path.rsplit("/", 1)[-1],
                    text=f"[{path}]\n{window}",
                    source_path=doc.source_path,
                )
            )
            seq += 1
        seqs[path] = seq
    return chunks


def _windows(body: str, cfg: SplitConfig) -> list[str]:
    lines = body.splitlines()
    windows: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        j, total = i, 0
        while j < n:
            cost = len(lines[j]) + (1 if j > i else 0)
            if j > i and total + cost > cfg.max_chars:
                break
            total += cost
            j += 1
        if j == i:  # 单行超限：硬切该行
            line = lines[i]
            for k in range(0, len(line), cfg.max_chars):
                windows.append(line[k : k + cfg.max_chars])
            i += 1
            continue
        windows.append("\n".join(lines[i:j]))
        if j >= n:
            break
        k, carried = j, 0  # 回退收集重叠尾部行
        while k - 1 > i and carried + len(lines[k - 1]) + 1 <= cfg.overlap:
            k -= 1
            carried += len(lines[k]) + 1
        i = k
    return windows


def split_corpus(corpus_dir: Path, config: SplitConfig | None = None) -> list[Chunk]:
    result: list[Chunk] = []
    for doc in load_corpus_docs(corpus_dir):
        result.extend(split_document(doc, config))
    return result
