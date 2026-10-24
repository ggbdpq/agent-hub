"""R1（对抗审查 P2）：database is locked 的有限退避重试。

审查复现：busy_timeout(5s) 耗尽后 OperationalError 直接上抛，无重试——一次瞬态
锁错会击穿整条调用链（worker 单轮崩溃、emit 误判 failed）。

修复语义：
- store / ledger 的全部公共方法对 SQLITE_BUSY / SQLITE_LOCKED 做"初始尝试 + 3 次
  指数退避重试"（50ms/100ms/200ms）；其他 OperationalError（no such table 等）与
  业务异常（LeaseLost / KeyError / IntegrityError）一律不重试；
- 重试整体安全：BEGIN IMMEDIATE 的 BUSY 发生在任何语句执行前（无部分写入可残留）；
  单语句写在 autocommit 下失败即无副作用；WAL + IMMEDIATE 下事务内不出现 BUSY。
"""

import sqlite3
import threading

import pytest
from app.contracts.runtime import LeaseLost

from app.runtime import SqliteRunStore
from app.runtime import store as store_module
from app.runtime.store import LOCK_RETRY_ATTEMPTS, _retry_on_locked


def _busy_error() -> sqlite3.OperationalError:
    return sqlite3.OperationalError("database is locked")


# -- 重试助手单测（不碰真实锁）--


def test_retry_recovers_with_exponential_backoff(monkeypatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(store_module, "_sleep", sleeps.append)
    calls = {"n": 0}

    def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _busy_error()
        return "ok"

    wrapped = _retry_on_locked(flaky)  # 装饰器语义：返回包装函数
    assert wrapped() == "ok"
    assert calls["n"] == 3
    assert sleeps == [0.05, 0.1]  # 指数退避：base × 2^i


def test_retry_does_not_touch_business_or_non_busy_errors() -> None:
    def fenced() -> None:
        raise LeaseLost("token 已被顶替")

    with pytest.raises(LeaseLost):
        _retry_on_locked(fenced)()  # 业务异常不重试

    calls = {"n": 0}

    def not_busy() -> None:
        calls["n"] += 1
        raise sqlite3.OperationalError("no such table: runs")

    with pytest.raises(sqlite3.OperationalError, match="no such table"):
        _retry_on_locked(not_busy)()
    assert calls["n"] == 1  # 非 BUSY 错误码不重试


def test_retry_gives_up_after_max_attempts(monkeypatch) -> None:
    monkeypatch.setattr(store_module, "_sleep", lambda _s: None)
    calls = {"n": 0}

    def always_busy() -> None:
        calls["n"] += 1
        raise _busy_error()

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _retry_on_locked(always_busy)()
    assert calls["n"] == LOCK_RETRY_ATTEMPTS  # 初始尝试 + 3 次重试后放弃


# -- 真实锁竞争（审查复现路径）--


def test_store_writes_retry_until_foreign_lock_released(clock, sqlite_path) -> None:
    """外部连接持写锁超过 busy_timeout → 另一实例写入修复前直接抛
    OperationalError，修复后退避重试至锁释放后成功。"""
    writer = SqliteRunStore(sqlite_path, clock=clock, busy_timeout_ms=50)
    # check_same_thread=False：Timer 线程要替主线程释放这把锁
    holder = sqlite3.connect(sqlite_path, isolation_level=None, check_same_thread=False)
    holder.execute("BEGIN IMMEDIATE")  # 外部事务持写锁
    release = threading.Timer(0.3, holder.execute, args=("ROLLBACK",))
    release.start()
    try:
        # busy_timeout 仅 50ms < 300ms 持锁时长：修复前此处必然 OperationalError
        created = writer.create_run("inspection", {"site": "s1"})
        claim = writer.claim("w1", lease_s=15)  # 同一竞态路径（认领）
        assert claim is not None and claim.run_id == created.run_id
        assert writer.get(created.run_id).status == "running"
    finally:
        release.join()
        holder.close()
        writer.close()
