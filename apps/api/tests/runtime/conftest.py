"""tests/runtime 共享夹具：SQLite 版 store / ledger（子智能体 A 维护）。

时间注入：与 contracts 语义测试一致，传入可控时钟（tests/conftest.FrozenClock），
使租约过期 / 心跳续期无需真实 sleep 即可测。未注入时钟的生产路径使用 SQLite 时间函数。
"""

import pytest

from app.runtime import SqliteRunStore


@pytest.fixture
def sqlite_store(clock, sqlite_path) -> SqliteRunStore:
    return SqliteRunStore(sqlite_path, clock=clock)
