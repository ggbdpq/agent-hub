"""租约回收器（A5）：启动时与每 interval 秒扫描过期租约。

职责边界："回收"指检测并上报（回调 / 日志 / 指标）；真正接管由 worker 的 claim
惰性完成——claim 的候选条件已把过期 running 视为可认领。回收器保证过期被及时
发现与计数（含"崩溃后任务最终结束"不变量的可观测面），自身不改任何状态。
同一 (run_id, attempt) 只上报一次：接管前反复扫描不虚增"回收次数"，
接管后新 attempt 再过期则作为新记录上报。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.contracts.runtime import RunRecord
from app.runtime.store import SqliteRunStore


class Reclaimer:
    def __init__(
        self,
        store: SqliteRunStore,
        interval_s: float = 10.0,
        on_expired: Callable[[RunRecord], None] | None = None,
    ) -> None:
        self._store = store
        self.interval_s = interval_s
        self._on_expired = on_expired
        self._seen: set[tuple[str, int]] = set()

    def scan_once(self) -> list[RunRecord]:
        """扫描一轮，返回新发现的过期租约（不含已上报过的 (run_id, attempt)）。"""
        expired = [
            rec
            for rec in self._store.list_expired()
            if (rec.run_id, rec.attempt) not in self._seen
        ]
        for rec in expired:
            self._seen.add((rec.run_id, rec.attempt))
            if self._on_expired is not None:
                self._on_expired(rec)
        return expired

    async def run_forever(self, stop: asyncio.Event) -> None:
        """启动即扫描一次，此后每 interval_s 扫描一轮；stop 置位后尽快退出。"""
        self.scan_once()
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.interval_s)
            except TimeoutError:
                pass
            self.scan_once()
