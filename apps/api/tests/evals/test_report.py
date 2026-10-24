"""C11：统一报告生成——方法/样本量/方差/已知偏差/离线标注 + 快照测试。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import pytest  # noqa: E402

from evals.report import finalize, render_markdown, std  # noqa: E402


def test_std_population_semantics() -> None:
    assert std([1.0, 2.0, 3.0]) == pytest.approx(0.8164965809)  # 总体标准差
    assert std([]) == 0.0
    assert std([5.0]) == 0.0


def test_finalize_requires_and_fills_sections() -> None:
    payload = {
        "suite": "demo",
        "title": "演示报告",
        "method": ["方法一", "方法二"],
        "sample_size": {"queries": 60},
        "variance": {"mrr_std": 0.1},
        "known_biases": ["偏差甲"],
        "disclaimer": "仅验证流程，不代表模型效果。",
    }
    out = finalize(payload, generated_at="2026-10-24T00:00:00+00:00")
    for key in ("method", "sample_size", "variance", "known_biases", "disclaimer", "generated_at"):
        assert key in out, f"报告缺少必要段：{key}"
    with pytest.raises(ValueError):
        finalize({"suite": "demo", "title": "缺标注"})  # 离线口径声明缺失必须报错


def test_render_markdown_snapshot() -> None:
    """快照：固定 generated_at 下逐字节稳定（报告可复现）。"""
    payload = finalize(
        {
            "suite": "demo",
            "title": "演示报告",
            "meta": [("语料", "30 篇"), ("标注", "60 条")],
            "method": ["确定性检索通道，逐条计时。"],
            "sample_size": {"queries": 2},
            "variance": {"recall_at_5_std": 0.05, "mrr_std": 0.03},
            "known_biases": ["查询措辞与正文高度对齐，指标天然偏乐观。"],
            "disclaimer": "仅验证流程，不代表模型效果。",
            "tables": [
                {
                    "caption": "变体对比",
                    "header": ["变体", "Recall@5"],
                    "rows": [["hybrid-bigram", "0.908"], ["bm25-only", "0.925"]],
                }
            ],
        },
        generated_at="2026-10-24T00:00:00+00:00",
    )
    expected = """# 演示报告

- 生成时间：2026-10-24T00:00:00+00:00
- 语料：30 篇
- 标注：60 条
- 样本量：queries=2

> **仅验证流程，不代表模型效果。**

## 方法

- 确定性检索通道，逐条计时。

## 变体对比

| 变体 | Recall@5 |
|---|---|
| hybrid-bigram | 0.908 |
| bm25-only | 0.925 |

## 方差与波动

- recall_at_5_std = 0.05
- mrr_std = 0.03

## 已知偏差

- 查询措辞与正文高度对齐，指标天然偏乐观。
"""
    assert render_markdown(payload) == expected, "报告快照不一致（必须逐字节可复现）"
