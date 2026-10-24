"""运行状态机（A2）：合法迁移表与终态吸收。

对计划 §7.1-A2 草图的一处偏差（以冻结契约为准）：草图含 ``running→queued``
（租约过期回收）；contracts-v1 的参考实现 ``fakes.InMemoryRunStore.claim`` 把
过期 running 直接 ``running→running``（attempt+1、发 run.resumed），不存在回
queued 的路径，因此迁移表不含该边。claim / finish 在执行迁移前用 can_transition
守卫（见 store.py）。
"""

from app.contracts.runtime import RunStatus, TerminalStatus

# 契约 RunStatus 的全部取值（新状态加入时此处与 contracts 同步升版）
ALL_STATUSES: tuple[RunStatus, ...] = (
    "queued",
    "running",
    "completed",
    "failed",
    "cancelled",
    "partial",
)

# 终态：无出边（终态吸收——之后任何迁移与写入都被拒绝）
TERMINAL_STATUSES: tuple[TerminalStatus, ...] = ("completed", "failed", "cancelled", "partial")

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    "queued": frozenset({"running"}),
    # running→running：过期租约被重新认领（续跑），心跳不改状态、不算迁移
    "running": frozenset({"running", "completed", "failed", "cancelled", "partial"}),
    "completed": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "partial": frozenset(),
}


def can_transition(src: str, dst: str) -> bool:
    """dst 是否为 src 的合法出边；未知状态一律非法（fail closed）。"""
    return dst in VALID_TRANSITIONS.get(src, frozenset())
