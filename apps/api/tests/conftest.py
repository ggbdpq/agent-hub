"""公共夹具（计划 §5.1，仅主线程维护）：可控时钟、临时 SQLite 路径、替身实例。"""

from datetime import UTC, datetime, timedelta

import pytest

from app.contracts.fakes import InMemoryLedger, InMemoryRunStore, StaticRetriever


class FrozenClock:
    """冻结起点、可推进的时钟（aware UTC）。advance 模拟时间流逝以测租约过期。"""

    def __init__(self) -> None:
        self._now = datetime(2026, 10, 24, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> FrozenClock:
    return FrozenClock()


@pytest.fixture
def sqlite_path(tmp_path) -> str:
    return str(tmp_path / "runs.sqlite")


@pytest.fixture
def store(clock) -> InMemoryRunStore:
    return InMemoryRunStore(clock=clock)


@pytest.fixture
def ledger(clock) -> InMemoryLedger:
    return InMemoryLedger(clock=clock)


@pytest.fixture
def retriever() -> StaticRetriever:
    return StaticRetriever()
