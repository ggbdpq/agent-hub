"""租约心跳循环（A5）：间隔 = 租约 / 3，续租失败置 lost 事件。

心跳失败意味着执行者已失去租约（被顶替 / 过期 / 终态）——这是编排层主动停止的
信号（经 RunContext.cancelled() 传导），与 fencing 的"过期未接管不误拒"互补：
ADR-0008 第 5 条把过期防护交给"心跳失败 + 编排层主动停止"承担。
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from app.runtime.store import SqliteRunStore


class HeartbeatLoop:
    """单个 run 的后台心跳任务。

    - 间隔默认 lease_s / 3（计划 §7.1-A5：默认租约 15s → 心跳 5s）；
    - store 为同步 SQLite 调用，经 to_thread 执行，不阻塞事件循环；
    - heartbeat 返回 False 时置 ``lost`` 事件并退出——调用方在 cancelled() 里读它。
    """

    def __init__(
        self,
        store: SqliteRunStore,
        run_id: str,
        token: int,
        lease_s: int,
        *,
        lost: asyncio.Event | None = None,
        interval_s: float | None = None,
    ) -> None:
        self._store = store
        self._run_id = run_id
        self._token = token
        self._lease_s = lease_s
        self.lost: asyncio.Event = lost if lost is not None else asyncio.Event()
        self.interval_s = interval_s if interval_s is not None else lease_s / 3.0
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is not None:
            raise RuntimeError("HeartbeatLoop 已启动")
        self._task = asyncio.create_task(self._runner(), name=f"heartbeat:{self._run_id}")
        return self._task

    def stop(self) -> None:
        """取消心跳任务；调用方随后 ``await task`` 收尾（CancelledError 已被吸收）。"""
        if self._task is not None:
            self._task.cancel()

    async def _runner(self) -> None:
        try:
            await self._beat_forever()
        except asyncio.CancelledError:
            pass  # stop() 的取消即正常退出路径，吸收后任务以完成态结束

    async def _beat_forever(self) -> None:
        beat: Callable[[], object] = lambda: self._store.heartbeat(
            self._run_id, self._token, self._lease_s
        )
        while True:
            await asyncio.sleep(self.interval_s)
            ok = await asyncio.to_thread(beat)
            if not ok:
                self.lost.set()
                return
