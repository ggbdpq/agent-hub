"""中文检索分词（C2）。

- ``bigram``（默认）：CJK 连续段切字符 bigram（段长 1 退化为单字）；ASCII 连续段
  [a-zA-Z0-9_]+（含点号连接的小数）作为整词。零依赖、确定性。
- ``unigram``：CJK 单字切分，作为分词方案对比基线（U4：按 Recall@5 与 bigram 对比后定）。

jieba 对比受依赖限制未跑（见 docs/integration-requests/C.md），结论以 bigram vs unigram
的离线对比为准，写入 evals/reports/retrieval-*.md。
"""

import re

_ASCII_TOKEN = re.compile(r"[A-Za-z0-9_]+(?:\.[0-9]+)?")
_CJK = re.compile(r"[\u4e00-\u9fff]+")

TokenMode = str  # "bigram" | "unigram"


def _cjk_tokens(run: str, mode: TokenMode) -> list[str]:
    if mode == "unigram":
        return list(run)
    if len(run) == 1:
        return [run]
    return [run[i : i + 2] for i in range(len(run) - 1)]


def tokenize(text: str, mode: TokenMode = "bigram") -> list[str]:
    if mode not in ("bigram", "unigram"):
        raise ValueError(f"未知分词模式：{mode!r}（可选 bigram/unigram）")
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
