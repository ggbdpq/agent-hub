"""chunk_id 编解码（C0/C1 共用）。

格式：``doc_id#标题路径#序号``。
- doc_id：语料文件名去扩展名（如 ``pv_inverter-thresholds``），不含 ``#``；
- 标题路径：从 H1 起逐级标题文本，以 ``/`` 连接（如 ``异常判据与告警阈值/告警阈值``），
  标题文本内不允许出现 ``#``；
- 序号：同一标题路径下第 n 块（1 起）。

不变量：内容不变则 ID 不变（证据引用稳定，ADR 评审要求）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ChunkRef:
    doc_id: str
    heading_path: str  # 以 / 连接的标题层级路径
    seq: int  # 同路径下的第几块（1 起）

    def format(self) -> str:
        return f"{self.doc_id}#{self.heading_path}#{self.seq}"


def format_chunk_id(doc_id: str, heading_path: str, seq: int) -> str:
    if "#" in doc_id or "#" in heading_path:
        raise ValueError(f"doc_id 与标题路径不得包含 '#'：{doc_id}#{heading_path}")
    return f"{doc_id}#{heading_path}#{seq}"


def parse_chunk_id(chunk_id: str) -> ChunkRef:
    parts = chunk_id.split("#")
    if len(parts) != 3:
        raise ValueError(f"chunk_id 必须是 doc_id#标题路径#序号 三段：{chunk_id!r}")
    doc_id, heading_path, seq_raw = parts
    if not doc_id or not heading_path:
        raise ValueError(f"chunk_id 的 doc_id/标题路径不能为空：{chunk_id!r}")
    try:
        seq = int(seq_raw)
    except ValueError as e:
        raise ValueError(f"chunk_id 序号必须是整数：{chunk_id!r}") from e
    if seq < 1:
        raise ValueError(f"chunk_id 序号必须从 1 起：{chunk_id!r}")
    return ChunkRef(doc_id=doc_id, heading_path=heading_path, seq=seq)
