"""检索评测（C6）：Recall@1/3/5、MRR、平均耗时；输入为 C0 标注（问题 → 相关 chunk_id）。

合成语料 + 人工标注上的检索指标（词法/向量确定性通道），不代表真实业务检索效果；
含 Scripted 成分的口径声明在报告层完成（C11）。
"""

import time
from dataclasses import dataclass, field

from app.contracts.knowledge import Retriever
from app.knowledge.chunk_id import parse_chunk_id
from evals.metrics import mrr, percentile, recall_at_k
from evals.report import std

Case = tuple[str, str, set[str]]  # (qid, query, relevant_chunk_ids)


@dataclass
class QueryResult:
    qid: str
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    mrr: float
    latency_ms: float
    n_returned: int


@dataclass
class RetrievalReport:
    config: dict
    per_query: list[QueryResult] = field(default_factory=list)

    @property
    def aggregates(self) -> dict:
        n = len(self.per_query)

        def mean(key: str) -> float:
            return sum(getattr(q, key) for q in self.per_query) / n if n else 0.0

        latencies = [q.latency_ms for q in self.per_query]
        return {
            "queries": n,
            "recall_at_1": mean("recall_at_1"),
            "recall_at_3": mean("recall_at_3"),
            "recall_at_5": mean("recall_at_5"),
            "mrr": mean("mrr"),
            "latency_ms_mean": mean("latency_ms"),
            "latency_ms_p50": percentile(latencies, 50),
            "latency_ms_p95": percentile(latencies, 95),
            "empty_rate": (sum(1 for q in self.per_query if q.n_returned == 0) / n) if n else 0.0,
            # 方差（C11）：逐条标注上的波动，报告与消融共用
            "recall_at_5_std": std([q.recall_at_5 for q in self.per_query]),
            "mrr_std": std([q.mrr for q in self.per_query]),
            "latency_ms_std": std(latencies),
        }


async def run_retrieval_eval(retriever: Retriever, cases: list[Case], k: int = 5) -> RetrievalReport:
    """逐条查询计时检索（k=5），对每条标注计算 Recall@1/3/5 与 MRR。"""
    report = RetrievalReport(config={"k": k})
    for qid, query, relevant in cases:
        start = time.perf_counter()
        chunks = await retriever.search(query, k)
        latency_ms = (time.perf_counter() - start) * 1000.0
        ranked = [c.chunk_id for c in chunks]
        report.per_query.append(
            QueryResult(
                qid=qid,
                recall_at_1=recall_at_k(ranked, relevant, 1),
                recall_at_3=recall_at_k(ranked, relevant, 3),
                recall_at_5=recall_at_k(ranked, relevant, 5),
                mrr=mrr(ranked, relevant),
                latency_ms=latency_ms,
                n_returned=len(chunks),
            )
        )
    return report


def annotations_to_cases(annotations: dict) -> list[Case]:
    """C0 标注 → 评测用例；顺带校验 chunk_id 可解析（与 C0 测试同源的运行时防线）。"""
    cases: list[Case] = []
    for q in annotations["queries"]:
        relevant = {parse_chunk_id(cid).format() for cid in q["relevant_chunk_ids"]}
        cases.append((q["qid"], q["query"], relevant))
    return cases
