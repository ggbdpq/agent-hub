"""运行指标（A10）：在途数、回收次数、租约丢失次数、平均时长。

- ``RuntimeMetrics``：进程内计数器（单事件循环内更新，无锁）；租约丢失 = 心跳失联
  或终态 CAS 被拒的执行尝试数；
- ``SqliteRunStore.stats()``：从库中派生（在途数 / 终态平均时长），平均时长无终态
  样本时为 None（不编造）；
- ``get_metrics(store, metrics)``：合并两者的一次性快照。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.runtime.store import SqliteRunStore


@dataclass
class RuntimeMetrics:
    """运行时计数器。claims/finished/lease_lost 由 RuntimeWorker 更新，reclaimed 由 Reclaimer 更新。"""

    claims: int = 0
    finished: int = 0
    lease_lost: int = 0
    reclaimed: int = 0

    def snapshot(self) -> dict[str, int]:
        return {
            "claims": self.claims,
            "finished": self.finished,
            "lease_lost": self.lease_lost,
            "reclaimed": self.reclaimed,
        }


def get_metrics(store: SqliteRunStore, metrics: RuntimeMetrics | None = None) -> dict:
    """合并库内派生指标与进程内计数器的一次性快照。"""
    stats = store.stats()
    counters = metrics.snapshot() if metrics is not None else {}
    return {
        "in_flight": stats["in_flight"],
        "avg_duration_s": stats["avg_duration_s"],
        "claims": counters.get("claims", 0),
        "finished": counters.get("finished", 0),
        "lease_lost": counters.get("lease_lost", 0),
        "reclaimed": counters.get("reclaimed", 0),
    }
