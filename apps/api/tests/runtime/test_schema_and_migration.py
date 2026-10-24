"""A1：SQLite 模式与迁移。

验收（计划 §7.1-A1）：模式创建幂等；重复 idempotency_key 返回同一 run；
WAL 模式与 busy_timeout 就位；重启（重新打开连接）后数据仍在。
"""

import sqlite3

from app.runtime import SqliteRunStore


def test_schema_creation_is_idempotent(clock, sqlite_path) -> None:
    """同一文件上多次打开 store：不报错、不丢数据、表只建一次。"""
    first = SqliteRunStore(sqlite_path, clock=clock)
    run = first.create_run("inspection", {"site": "s1"})

    second = SqliteRunStore(sqlite_path, clock=clock)
    assert second.get(run.run_id) is not None

    tables = {
        row[0]
        for row in sqlite3.connect(sqlite_path).execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"runs", "run_events", "tool_ledger"} <= tables


def test_schema_version_stamped_for_future_migrations(sqlite_path) -> None:
    """user_version 记录模式版本，迁移器据此判断是否需要 ALTER（当前 v1）。"""
    SqliteRunStore(sqlite_path)
    version = sqlite3.connect(sqlite_path).execute("PRAGMA user_version").fetchone()[0]
    assert version == 1


def test_wal_mode_persists_in_db_file(sqlite_path) -> None:
    SqliteRunStore(sqlite_path)
    # journal_mode=WAL 是库级持久属性：用全新连接读，模拟另一个进程看到的模式
    mode = sqlite3.connect(sqlite_path).execute("PRAGMA busy_timeout=0").execute(
        "PRAGMA journal_mode"
    ).fetchone()[0]
    assert mode == "wal"


def test_busy_timeout_configured(sqlite_path) -> None:
    store = SqliteRunStore(sqlite_path, busy_timeout_ms=5000)
    assert store.busy_timeout_ms == 5000


def test_duplicate_idempotency_key_returns_same_run(sqlite_store) -> None:
    a = sqlite_store.create_run("inspection", {"site": "s1"}, idempotency_key="op-1")
    b = sqlite_store.create_run("inspection", {"site": "s1"}, idempotency_key="op-1")
    assert a.run_id == b.run_id


def test_run_survives_reopen_and_key_dedup_across_reopen(clock, sqlite_path) -> None:
    """崩溃一致性前提：已提交的 run 在进程重启（关连接再开）后仍可读，幂等键去重跨重启生效。"""
    s1 = SqliteRunStore(sqlite_path, clock=clock)
    run = s1.create_run("inspection", {"site": "s1"}, idempotency_key="op-1")
    s1.close()

    s2 = SqliteRunStore(sqlite_path, clock=clock)
    restored = s2.get(run.run_id)
    assert restored is not None and restored.status == "queued"
    assert restored.input == {"site": "s1"}
    again = s2.create_run("inspection", {"site": "s1"}, idempotency_key="op-1")
    assert again.run_id == run.run_id
    s2.close()
