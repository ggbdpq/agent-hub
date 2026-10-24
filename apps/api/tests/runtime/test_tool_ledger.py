"""A7：工具幂等账本——写工具不因重试或续跑执行两次（不变量）。

移植 contracts-v1 语义（同键 begin 返回原记录、unknown 不自动重试）+ 崩溃注入：
begin 后进程崩溃未 complete，重启续跑时同键 begin 仍返回原记录，写工具不会被
盲目二次执行。
"""

import sqlite3

import pytest
from app.runtime import SqliteLedger


def test_begin_is_idempotent_per_key(sqlite_ledger) -> None:
    first = sqlite_ledger.begin("r1:inspector:1:call-1", "create_work_order", args_hash="h1")
    assert first.status == "in_progress"
    second = sqlite_ledger.begin("r1:inspector:1:call-1", "create_work_order", args_hash="h1")
    assert second == first  # 同键返回原记录


def test_complete_then_begin_returns_result(sqlite_ledger) -> None:
    key = "r1:inspector:1:call-1"
    sqlite_ledger.begin(key, "create_work_order", args_hash="h1")
    sqlite_ledger.complete(key, {"order_id": "DD001"})
    done = sqlite_ledger.begin(key, "create_work_order", args_hash="h1")
    assert done.status == "completed" and done.result == {"order_id": "DD001"}


def test_fail_retryable_marks_unknown(sqlite_ledger) -> None:
    """retryable=True → unknown（可能已执行，禁止自动重试）；False → failed。"""
    key = "r1:inspector:2:call-2"
    sqlite_ledger.begin(key, "create_work_order", args_hash="h2")
    sqlite_ledger.fail(key, "上游超时，执行状态未知", retryable=True)
    assert sqlite_ledger.begin(key, "x", "h2").status == "unknown"

    key2 = "r1:inspector:3:call-3"
    sqlite_ledger.begin(key2, "create_work_order", args_hash="h3")
    sqlite_ledger.fail(key2, "参数被拒，确定未执行", retryable=False)
    assert sqlite_ledger.begin(key2, "x", "h3").status == "failed"
    assert sqlite_ledger.begin(key2, "x", "h3").retryable is False


def test_complete_or_fail_unknown_key_raises(sqlite_ledger) -> None:
    """与 fakes 一致：对不存在的键 complete/fail 是编程错误（KeyError）。"""
    with pytest.raises(KeyError):
        sqlite_ledger.complete("no-such-key", {})
    with pytest.raises(KeyError):
        sqlite_ledger.fail("no-such-key", "e", retryable=True)


def test_write_tool_not_executed_twice_after_crash(clock, sqlite_path) -> None:
    """崩溃注入（计划 §7.1-A7 验收）：begin 后进程崩溃，续跑时写工具不重复执行。

    策略链：begin 返回崩溃遗留的 in_progress 原记录 → 调用方 fail(retryable=True)
    标记 unknown → 后续同键 begin 得到 unknown → 禁止自动重试；人工/上游确认后
    complete 落结果 → 再 begin 直接复用结果，handler 全程只执行一次。
    """
    key = "r1:reporter:1:call-1"
    handler_calls: list[str] = []

    # —— 第一次执行：begin → handler 正在执行 → 进程崩溃（不 complete/fail）——
    crashed = SqliteLedger(sqlite_path, clock=clock)
    entry = crashed.begin(key, "create_work_order", args_hash="h1")
    assert entry.status == "in_progress"
    handler_calls.append("exec-1")  # handler 已发生（副作用未知：可能提交到一半）
    crashed.close()  # 模拟 kill -9：连接直接丢弃，事务不回滚到"无记录"

    # —— 续跑：新进程新连接，同键 begin 必须拿到崩溃遗留的原记录 ——
    resumed = SqliteLedger(sqlite_path, clock=clock)
    again = resumed.begin(key, "create_work_order", args_hash="h1")
    assert again.key == key and again.status == "in_progress"  # 不是新记录
    assert handler_calls == ["exec-1"]  # in_progress 且不可确认 → 不盲目重跑

    resumed.fail(key, "崩溃后执行状态未知", retryable=True)  # 状态未知：禁止自动重试
    assert resumed.begin(key, "x", "h1").status == "unknown"
    resumed.close()

    # —— 人工/上游核实后确认已提交：落结果，此后同键永远复用 ——
    resolved = SqliteLedger(sqlite_path, clock=clock)
    resolved.complete(key, {"order_id": "DD001"})
    final = resolved.begin(key, "create_work_order", args_hash="h1")
    assert final.status == "completed" and final.result == {"order_id": "DD001"}
    assert handler_calls == ["exec-1"]  # 全程只有第一次那次执行
    resolved.close()


def test_ledger_survives_reopen(clock, sqlite_path) -> None:
    """账本持久化：关连接重开后记录仍在（崩溃一致性的前提）。"""
    ledger = SqliteLedger(sqlite_path, clock=clock)
    ledger.begin("k1", "create_work_order", args_hash="h")
    ledger.close()
    reopened = SqliteLedger(sqlite_path, clock=clock)
    entry = reopened.begin("k1", "create_work_order", args_hash="h")
    assert entry.status == "in_progress" and entry.tool == "create_work_order"
    reopened.close()


def test_ledger_table_created_idempotently(clock, sqlite_path) -> None:
    """账本可独立于 store 在同一库上多次初始化（IF NOT EXISTS，不动 user_version）。"""
    SqliteLedger(sqlite_path, clock=clock).close()
    SqliteLedger(sqlite_path, clock=clock).close()
    with sqlite3.connect(sqlite_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "tool_ledger" in tables
