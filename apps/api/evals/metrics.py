"""评测指标（C6/C8 共用）：全部为纯函数，手算样例锁定语义（tests/evals/）。"""

from collections.abc import Sequence

import numpy as np


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int) -> float:
    """ranked 前 k 项命中相关集合的比例（宏平均的分子；relevant 为空按 0）。"""
    if not relevant or k <= 0:
        return 0.0
    hits = sum(1 for cid in list(ranked)[:k] if cid in relevant)
    return hits / len(relevant)


def mrr(ranked: Sequence[str], relevant: set[str]) -> float:
    """首个相关结果倒数的均值（单查询）；未命中为 0。"""
    for rank, cid in enumerate(ranked, start=1):
        if cid in relevant:
            return 1.0 / rank
    return 0.0


def precision_recall(predicted: set[str], expected: set[str]) -> tuple[float, float]:
    """集合级 P/R；predicted 为空 → (0, 0)（不把空预测当满分召回）。"""
    if not predicted:
        return 0.0, 0.0
    hits = len(predicted & expected)
    return hits / len(predicted), (hits / len(expected) if expected else 0.0)


def percentile(values: Sequence[float], q: float) -> float:
    """线性插值分位数（numpy 语义）；空集返回 0。"""
    if not values:
        return 0.0
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))
