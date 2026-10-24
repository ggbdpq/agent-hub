"""A2：状态机——合法迁移表 + 终态吸收（参数化覆盖全部 6×6 迁移）。

契约冻结语义（fakes.InMemoryRunStore 参考实现）：
- queued → running（首次认领，run.started）
- running → running（过期租约被重新认领，attempt+1，run.resumed）
- running → completed / failed / cancelled / partial（持有租约者 CAS 写终态）
- 终态无出边：completed / failed / cancelled / partial 之后任何迁移都拒绝
"""

import pytest

from app.runtime.state_machine import TERMINAL_STATUSES, VALID_TRANSITIONS, can_transition

ALL_STATUSES = ["queued", "running", "completed", "failed", "cancelled", "partial"]

LEGAL_TRANSITIONS = {
    ("queued", "running"),
    ("running", "running"),
    ("running", "completed"),
    ("running", "failed"),
    ("running", "cancelled"),
    ("running", "partial"),
}


@pytest.mark.parametrize("src", ALL_STATUSES)
@pytest.mark.parametrize("dst", ALL_STATUSES)
def test_transition_table_matches_contract(src: str, dst: str) -> None:
    assert can_transition(src, dst) == ((src, dst) in LEGAL_TRANSITIONS)


def test_unknown_status_is_never_legal() -> None:
    assert can_transition("queued", "bananas") is False
    assert can_transition("bananas", "running") is False


def test_terminal_statuses_are_absorbing() -> None:
    assert set(TERMINAL_STATUSES) == {"completed", "failed", "cancelled", "partial"}
    for terminal in TERMINAL_STATUSES:
        assert VALID_TRANSITIONS[terminal] == set(), f"{terminal} 必须无出边（终态吸收）"
