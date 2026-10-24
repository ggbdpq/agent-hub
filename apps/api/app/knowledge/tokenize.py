"""中文检索分词（C2 / U4 闭环）。

- ``bigram``（默认）：CJK 连续段切字符 bigram（段长 1 退化为单字）；ASCII 连续段
  [a-zA-Z0-9_]+（含点号连接的小数）作为整词。零依赖、确定性。
- ``unigram``：CJK 单字切分，作为分词方案对比基线。
- ``jieba``：CJK 段交 jieba 词典切词（需安装 jieba，懒导入；ASCII 口径与前两者一致）。
  三种模式对同一文本均确定性（jieba 词典随版本固定）。

U4 结论以 Recall@5 对比为准，写入 evals/reports/retrieval-*.md。
"""

import re

_ASCII_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:\.[0-9]+)?")
_CJK = re.compile(r"[\u4e00-\u9fff]+")

TokenMode = str  # "bigram" | "unigram" | "jieba"


def _cjk_tokens(run: str, mode: TokenMode) -> list[str]:
    if mode == "unigram":
        return list(run)
    if mode == "jieba":
        import jieba  # 懒导入：未安装时该模式不可用，其余模式不受影响

        return [w for w in jieba.cut(run) if w.strip()]
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str, mode: TokenMode = "bigram") -> list[str]:
    if mode not in ("bigram", "unigram", "jieba"):
        raise ValueError(f"未知分词模式：{mode!r}（可选 bigram/unigram/jieba）")
    lowered = text.lower()
    tokens: list[str] = []
    pos = 0
    for m in _ASCII_TOKEN.finditer(lowered):
        for run in _CJK.findall(lowered[pos : m.start()]):
            tokens.extend(_cjk_tokens(run, mode))
        tokens.append(m.group(0))
        pos = m.end()
    for run in _CJK.findall(lowered[pos:]):
        tokens.extend(_cjk_tokens(run, mode))
    return tokens
