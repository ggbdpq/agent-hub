"""SQLite 版持久化运行时（ADR-0006）：RunStore 与 ToolLedger。

语义必须与 contracts/fakes.py 的参考实现一致：
- claim 单调递增 fencing_token / attempt，租约过期判定发生在 SQL WHERE 中；
- fencing：写入校验"token 是当前值且 status=running"——租约过期但未被重新认领时
  token 仍是当前值，合法写入不误拒；被重新认领后旧执行者一律 LeaseLost / False；
- 终态吸收：completed/failed/cancelled/partial 之后任何迁移与写入都被拒绝；
- seq 事务内分配（MAX(seq)+1），每个 run 内严格递增无空洞。

时间语义（对抗审查清单第 1 条）：默认（未注入 clock）所有"现在"取自 SQLite
``unixepoch('subsec')``——多进程看到同一份数据库时钟，不受进程时钟漂移影响；
测试注入可控时钟（冻结 / 可推进）。时间统一存 INTEGER 微秒时间戳，避免文本
比较歧义与浮点精度问题，读出时转 aware UTC datetime。

并发模型（ADR-0006 第 4 条）：单进程多协程为主，本类单连接 + 线程锁串行化；
多进程仅用于崩溃注入测试，依赖 WAL + busy_timeout 短事务竞争写锁。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from app.contracts.runtime import RunRecord

Clock = Callable[[], datetime]

_SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN
                     ('queued','running','completed','failed','cancelled','partial')),
    input           TEXT NOT NULL,              -- JSON
    result          TEXT,                       -- JSON
    error           TEXT,
    version         INTEGER NOT NULL DEFAULT 0, -- 行版本号：每次状态变更 +1（审计用）
    attempt         INTEGER NOT NULL DEFAULT 0,
    fencing_token   INTEGER NOT NULL DEFAULT 0,
    lease_owner     TEXT,
    lease_expires_at INTEGER,                   -- 微秒时间戳；NULL = 无租约
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    idempotency_key TEXT UNIQUE,
    created_at      INTEGER NOT NULL,
    updated_at      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_runs_claimable ON runs (status, lease_expires_at);

CREATE TABLE IF NOT EXISTS run_events (
    run_id  TEXT NOT NULL REFERENCES runs(run_id),
    seq     INTEGER NOT NULL,
    type    TEXT NOT NULL,
    node    TEXT,
    payload TEXT NOT NULL,                      -- JSON
    attempt INTEGER NOT NULL,
    ts      INTEGER NOT NULL,                   -- 微秒时间戳
    PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS tool_ledger (
    key        TEXT PRIMARY KEY,                -- run_id:node:step:tool_call_id（编排层生成）
    tool       TEXT NOT NULL,
    args_hash  TEXT NOT NULL,
    status     TEXT NOT NULL CHECK (status IN
                ('in_progress','completed','failed','unknown')),
    result     TEXT,                            -- JSON
    error      TEXT,
    retryable  INTEGER,
    updated_at INTEGER NOT NULL
);
"""


def to_us(dt: datetime) -> int:
    """aware UTC datetime → 微秒时间戳（整数精确，不走浮点乘法）。"""
    return int(dt.timestamp()) * 1_000_000 + dt.microsecond


def from_us(us: int) -> datetime:
    """微秒时间戳 → aware UTC datetime。"""
    return datetime.fromtimestamp(us // 1_000_000, tz=UTC).replace(microsecond=us % 1_000_000)


class SqliteRunStore:
    """RunStore 契约的 SQLite 实现（多语句操作经 BEGIN IMMEDIATE 保持原子）。"""

    def __init__(self, path: str, *, clock: Clock | None = None, busy_timeout_ms: int = 5000) -> None:
        self._path = path
        self._clock = clock
        self.busy_timeout_ms = busy_timeout_ms
        self._conn = sqlite3.connect(
            path, timeout=busy_timeout_ms / 1000, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._configure()
        self._ensure_schema()

    # -- 公共 API（A1 范围：create_run / get；其余方法随 A3~A8 逐个落地）--

    def create_run(self, kind: str, input: dict, idempotency_key: str | None = None) -> RunRecord:
        input_json = json.dumps(input, ensure_ascii=False)
        with self._lock, self._tx() as conn:
            if idempotency_key:
                row = conn.execute(
                    "SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    return self._record(row)  # 幂等：同一 key 返回同一 run
            now = self._now_us()
            run_id = uuid.uuid4().hex
            try:
                conn.execute(
                    "INSERT INTO runs (run_id, kind, status, input, idempotency_key,"
                    " cancel_requested, created_at, updated_at)"
                    " VALUES (?, ?, 'queued', ?, ?, 0, ?, ?)",
                    (run_id, kind, input_json, idempotency_key, now, now),
                )
            except sqlite3.IntegrityError:
                # 跨实例并发撞同一幂等键：唯一约束胜者已提交，返回它的 run
                row = conn.execute(
                    "SELECT * FROM runs WHERE idempotency_key = ?", (idempotency_key,)
                ).fetchone()
                assert row is not None
                return self._record(row)
            self._insert_event(conn, run_id, seq=1, type_="run.queued", attempt=0, ts=now)
            return self._record(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._record(row) if row is not None else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 内部 --

    def _configure(self) -> None:
        conn = self._conn
        conn.execute("PRAGMA journal_mode=WAL")  # 库级持久：读不阻塞写，崩溃恢复靠 WAL
        conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        conn.execute("PRAGMA synchronous=NORMAL")  # WAL 下的常规选择；掉电丢最近事务不损坏库
        conn.execute("PRAGMA foreign_keys=ON")

    def _ensure_schema(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == _SCHEMA_VERSION:
                return
            if version > _SCHEMA_VERSION:
                raise RuntimeError(
                    f"数据库模式版本 {version} 高于代码支持的 {_SCHEMA_VERSION}，请升级代码"
                )
            self._conn.executescript(_SCHEMA)  # CREATE TABLE IF NOT EXISTS：重复执行安全
            self._conn.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE：进入时即取写锁（busy_timeout 内等待），避免写锁升级死锁。"""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")

    def _now_us(self) -> int:
        """生产路径取 SQLite 时间函数（多进程一致）；测试路径取注入时钟。"""
        if self._clock is not None:
            return to_us(self._clock())
        # unixepoch('subsec')：3.42+，REAL 秒（毫秒精度），够租约与事件时间戳使用
        seconds = self._conn.execute("SELECT unixepoch('subsec')").fetchone()[0]
        return int(round(seconds * 1_000_000))

    @staticmethod
    def _insert_event(
        conn: sqlite3.Connection,
        run_id: str,
        *,
        seq: int,
        type_: str,
        attempt: int,
        ts: int,
        node: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO run_events (run_id, seq, type, node, payload, attempt, ts)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (run_id, seq, type_, node, json.dumps(payload or {}, ensure_ascii=False), attempt, ts),
        )

    def _record(self, row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            kind=row["kind"],
            status=row["status"],
            input=json.loads(row["input"]),
            result=json.loads(row["result"]) if row["result"] is not None else None,
            error=row["error"],
            attempt=row["attempt"],
            fencing_token=row["fencing_token"],
            lease_owner=row["lease_owner"],
            lease_expires_at=from_us(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None,
            cancel_requested=bool(row["cancel_requested"]),
            idempotency_key=row["idempotency_key"],
            created_at=from_us(row["created_at"]),
            updated_at=from_us(row["updated_at"]),
        )
