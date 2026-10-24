"""C2：BM25 检索——关键词命中与中文分词（字符 bigram 起步，unigram 供对比）。

jieba 未安装（禁改 pyproject，集成请求在 docs/integration-requests/C.md）；
分词方案按 Recall@5 对比后在 C6 报告中定论。
"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from evals.datasets import corpus_root  # noqa: E402
from app.knowledge.bm25 import Bm25Searcher  # noqa: E402
from app.knowledge.splitting import load_corpus_docs, split_corpus  # noqa: E402
from app.knowledge.tokenize import tokenize  # noqa: E402

HIT_ID = "pv_inverter-thresholds#光伏逆变器异常判据与告警阈值/告警阈值#1"


def test_tokenize_mixed_cjk_ascii() -> None:
    tokens = tokenize("光伏逆变器 cabinet_temp_c > 85 时立即上报")
    assert "cabinet_temp_c" in tokens  # ASCII 标识符整词保留
    assert "85" in tokens
    assert "光伏" in tokens and "伏逆" in tokens  # CJK bigram


def test_tokenize_deterministic() -> None:
    q = "风电振动超限处理"
    assert tokenize(q) == tokenize(q)


def test_bm25_keyword_hit_in_top3() -> None:
    searcher = Bm25Searcher(split_corpus(corpus_root()))
    results = searcher.search_sync("光伏逆变器柜温超过多少要立即上报", k=5)
    assert results, "关键词查询必须命中"
    assert HIT_ID in [c.chunk_id for c in results[:3]]


def test_bm25_metric_identifier_hits_exact_section() -> None:
    searcher = Bm25Searcher(split_corpus(corpus_root()))
    results = searcher.search_sync("cabinet_temp_c 告警阈值是多少", k=3)
    assert results
    assert HIT_ID == results[0].chunk_id  # 标识符唯一锚定该节


def test_bm25_no_match_returns_empty() -> None:
    searcher = Bm25Searcher(split_corpus(corpus_root()))
    assert searcher.search_sync("红烧肉炖土豆怎么做才软烂入味", k=5) == []


def test_bm25_filters_restrict_doc_ids() -> None:
    searcher = Bm25Searcher(split_corpus(corpus_root()))
    results = searcher.search_sync("告警阈值 立即上报", k=10, filters={"doc_ids": ["wind_turbine-thresholds"]})
    assert results
    assert {c.doc_id for c in results} == {"wind_turbine-thresholds"}


def test_bm25_scores_descending_and_chunk_fields() -> None:
    searcher = Bm25Searcher(split_corpus(corpus_root()))
    results = searcher.search_sync("风机 振动 阈值", k=5)
    scores = [c.score for c in results]
    assert scores == sorted(scores, reverse=True)
    assert all(c.score > 0 for c in results)
    assert all(c.source_path for c in results)


def test_bm25_unigram_mode_also_hits() -> None:
    searcher = Bm25Searcher(split_corpus(corpus_root()), token_mode="unigram")
    results = searcher.search_sync("光伏逆变器柜温超过多少要立即上报", k=5)
    assert HIT_ID in [c.chunk_id for c in results[:5]]
