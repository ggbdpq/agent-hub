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

import functools
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from app.contracts.events import NewEvent, RunEvent
from app.contracts.runtime import Claim, LedgerEntry, LeaseLost, RunRecord, TerminalStatus
from app.runtime.state_machine import TERMINAL_STATUSES, can_transition

_TERMINAL_EVENTS = {
    "completed": "run.completed",
    "failed": "run.failed",
    "cancelled": "run.cancelled",
    "partial": "run.partial",
}

Clock = Callable[[], datetime]

_SCHEMA_VERSION = 1

_RUNS_SCHEMA = """
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
"""

_LEDGER_SCHEMA = """
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

# 全库模式由 SqliteRunStore 统一创建并盖 user_version 戳；SqliteLedger 独立初始化
# 时只自建 tool_ledger（见各自 _ensure / __init__）
_SCHEMA = _RUNS_SCHEMA + _LEDGER_SCHEMA


def to_us(dt: datetime) -> int:
    """aware UTC datetime → 微秒时间戳（整数精确，不走浮点乘法）。"""
    return int(dt.timestamp()) * 1_000_000 + dt.microsecond


def from_us(us: int) -> datetime:
    """微秒时间戳 → aware UTC datetime。"""
    return datetime.fromtimestamp(us // 1_000_000, tz=UTC).replace(microsecond=us % 1_000_000)


# -- database is locked 的有限退避重试（R1，对抗审查 P2）--

# 初始尝试 + 3 次重试，指数退避 50ms / 100ms / 200ms
LOCK_RETRY_ATTEMPTS = 4
LOCK_RETRY_BASE_S = 0.05


def _sleep(seconds: float) -> None:
    """退避睡眠（模块级间接：测试可注入假时钟）。"""
    time.sleep(seconds)


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    """仅 SQLITE_BUSY(5) / SQLITE_LOCKED(6)（含扩展码，掩码 0xFF）视为瞬态锁错。

    手工构造的异常缺 sqlite_errorcode 属性时按消息兜底判定。
    """
    code = getattr(exc, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    message = str(exc).lower()
    return "database is locked" in message or "table is locked" in message


def _retry_on_locked(op: Callable, *, attempts: int = LOCK_RETRY_ATTEMPTS,
                     base_delay_s: float = LOCK_RETRY_BASE_S) -> Callable:
    """对 database is locked 做有限次指数退避重试；其余异常原样上抛。

    不重试的异常：LeaseLost / KeyError / ValueError 等业务异常（非 OperationalError）；
    IntegrityError（唯一约束冲突，重试必然再撞）；非 BUSY 错误码的 OperationalError
    （如 no such table，重试不会自愈）。
    """
    @functools.wraps(op)
    def wrapper(*args, **kwargs):
        for i in range(attempts):
            try:
                return op(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not _is_busy_error(exc) or i == attempts - 1:
                    raise
                _sleep(base_delay_s * (2**i))

    return wrapper


def _retry_public_methods_on_locked(cls):
    """类装饰器：给 store / ledger 的全部公共方法整体包一层 BUSY 重试。

    整体重试的安全性：BEGIN IMMEDIATE 的 BUSY 发生在任何语句执行前（无部分写入）；
    单语句写在 autocommit 下失败即无副作用；WAL + IMMEDIATE 事务内不再出现写锁
    竞争，故"整个方法重跑"不会产生部分状态。
    """
    for name, attr in list(vars(cls).items()):
        if name.startswith("_") or not callable(attr):
            continue
        setattr(cls, name, _retry_on_locked(attr))
    return cls


@_retry_public_methods_on_locked
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

    # -- 公共 API --

    def claim(self, worker_id: str, lease_s: int) -> Claim | None:
        """认领一个 queued（或租约过期的 running）run；无可认领返回 None。

        单条 UPDATE + 子查询：候选行的筛选与占用在同一写事务内完成，
        并发认领由 SQLite 写锁串行化——后到者的子查询在锁内重评，必然看到前者
        的结果（单一胜者）。FIFO 与 fakes 一致：按创建序（冻结时钟同刻时 rowid 决胜）。
        """
        with self._lock, self._tx() as conn:
            now = self._now_us()
            row = conn.execute(
                "SELECT * FROM runs"
                " WHERE status = 'queued'"
                "    OR (status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)"
                " ORDER BY created_at, rowid LIMIT 1",
                (now,),
            ).fetchone()
            if row is None or not can_transition(row["status"], "running"):
                return None  # 状态机守卫（终态等），与 WHERE 条件双保险
            resumed = row["status"] == "running"
            new_token = row["fencing_token"] + 1
            new_attempt = row["attempt"] + 1
            conn.execute(
                "UPDATE runs SET status = 'running', attempt = ?, fencing_token = ?,"
                " lease_owner = ?, lease_expires_at = ?, updated_at = ?, version = version + 1"
                " WHERE run_id = ?",
                (new_attempt, new_token, worker_id, now + lease_s * 1_000_000, now, row["run_id"]),
            )
            self._insert_event(
                conn,
                row["run_id"],
                seq=self._next_seq(conn, row["run_id"]),
                type_="run.resumed" if resumed else "run.started",
                attempt=new_attempt,
                ts=now,
            )
            return Claim(
                run_id=row["run_id"],
                fencing_token=new_token,
                attempt=new_attempt,
                kind=row["kind"],
                input=json.loads(row["input"]),
            )

    def heartbeat(self, run_id: str, token: int, lease_s: int) -> bool:
        """续租；False = 已失去租约（被顶替 / 过期 / 终态），调用方应立即停止。

        单语句 CAS：status=running 且 token 是当前值且租约尚未过期才续期。
        与 fakes 一致：心跳不校验 lease_owner（token 即所有权凭据），失败不改任何状态。
        """
        with self._lock:
            now = self._now_us()
            cur = self._conn.execute(
                "UPDATE runs SET lease_expires_at = ?, updated_at = ?"
                " WHERE run_id = ? AND status = 'running' AND fencing_token = ?"
                "   AND lease_expires_at IS NOT NULL AND lease_expires_at > ?",
                (now + lease_s * 1_000_000, now, run_id, token, now),
            )
            return cur.rowcount == 1

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

    def append_event(self, run_id: str, token: int, event: NewEvent) -> RunEvent:
        """CAS 追加事件：token 是当前值且 status=running 才接受，否则 LeaseLost。

        seq 与事件插入在同一写事务内（MAX(seq)+1），锁内串行，无空洞无重复。
        租约过期但未被接管时 token 仍是当前值 → 放行（fencing 精确语义，ADR-0008 第 5 条）。
        """
        with self._lock, self._tx() as conn:
            run = conn.execute(
                "SELECT status, fencing_token, attempt FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run is None:
                raise KeyError(run_id)
            if run["status"] != "running" or run["fencing_token"] != token:
                raise LeaseLost(f"run {run_id} 不再接受 token={token} 的写入")
            seq = self._next_seq(conn, run_id)
            ts = self._now_us()
            self._insert_event(
                conn, run_id, seq=seq, type_=event.type, attempt=run["attempt"], ts=ts,
                node=event.node, payload=event.payload,
            )
            return RunEvent(
                **event.model_dump(),
                run_id=run_id,
                seq=seq,
                attempt=run["attempt"],
                ts=from_us(ts),
            )

    def finish(
        self,
        run_id: str,
        token: int,
        status: TerminalStatus,
        result: dict | None = None,
        error: str | None = None,
    ) -> bool:
        """CAS 写终态；False = 写入被拒（token 失效或已被终态）。

        UPDATE ... WHERE status='running' AND fencing_token=? 影响行数为 0 即失去
        所有权；终态事件与状态变更同事务落盘（崩溃不会出现"状态已终态但无事件"）。
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"finish 只接受终态 {TERMINAL_STATUSES}，收到 {status!r}")
        with self._lock, self._tx() as conn:
            now = self._now_us()
            cur = conn.execute(
                "UPDATE runs SET status = ?, result = ?, error = ?, lease_owner = NULL,"
                " lease_expires_at = NULL, updated_at = ?, version = version + 1"
                " WHERE run_id = ? AND status = 'running' AND fencing_token = ?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error,
                    now,
                    run_id,
                    token,
                ),
            )
            if cur.rowcount != 1:
                return False
            attempt = conn.execute(
                "SELECT attempt FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()["attempt"]
            self._insert_event(
                conn, run_id, seq=self._next_seq(conn, run_id),
                type_=_TERMINAL_EVENTS[status], attempt=attempt, ts=now,
                payload={"error": error},
            )
            return True

    def request_cancel(self, run_id: str) -> RunRecord:
        """协作式取消：只置标记，不改状态与租约；终态幂等返回原记录（不置标记）。

        终态由持有租约的 worker 写入（cancelled）；标记检查发生在编排层的节点边界
        与工具调用前。标记判定与写入同事务（BEGIN IMMEDIATE），与跨实例的 finish
        竞争时不会把终态 run 污染上取消标记。
        """
        with self._lock, self._tx() as conn:
            row = conn.execute("SELECT status FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            if row["status"] in ("queued", "running"):
                conn.execute(
                    "UPDATE runs SET cancel_requested = 1, updated_at = ? WHERE run_id = ?",
                    (self._now_us(), run_id),
                )
            return self._record(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())

    def is_cancel_requested(self, run_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT cancel_requested FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return row is not None and bool(row["cancel_requested"])

    def get(self, run_id: str) -> RunRecord | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return self._record(row) if row is not None else None

    def list_expired(self) -> list[RunRecord]:
        """status=running 且租约已过期的 run（回收器扫描用）。

        queued 不在列——它由 claim 直接认领；本方法只读，不迁移、不回收状态。
        """
        with self._lock:
            now = self._now_us()
            rows = self._conn.execute(
                "SELECT * FROM runs"
                " WHERE status = 'running' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?"
                " ORDER BY created_at, rowid",
                (now,),
            ).fetchall()
            return [self._record(row) for row in rows]

    def stats(self) -> dict[str, int | float | None]:
        """运行指标查询（A10）：在途数与终态平均时长（库内派生）。

        平均时长 = 终态 runs 的 (updated_at - created_at) 均值；无终态样本时为
        None（不编造）。进程内计数器（claims / reclaimed / lease_lost）见 metrics.py。
        """
        with self._lock:
            in_flight = self._conn.execute(
                "SELECT COUNT(*) FROM runs WHERE status = 'running'"
            ).fetchone()[0]
            avg_us = self._conn.execute(
                "SELECT AVG(updated_at - created_at) FROM runs"
                " WHERE status IN ('completed','failed','cancelled','partial')"
            ).fetchone()[0]
            return {
                "in_flight": in_flight,
                "avg_duration_s": None if avg_us is None else avg_us / 1_000_000,
            }

    def events_after(self, run_id: str, after_seq: int) -> list[RunEvent]:
        """after_seq 之后的事件按 seq 升序返回，支撑 SSE Last-Event-ID 断点续传。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM run_events WHERE run_id = ? AND seq > ? ORDER BY seq",
                (run_id, after_seq),
            ).fetchall()
            return [
                RunEvent(
                    type=row["type"],
                    node=row["node"],
                    payload=json.loads(row["payload"]),
                    run_id=row["run_id"],
                    seq=row["seq"],
                    attempt=row["attempt"],
                    ts=from_us(row["ts"]),
                )
                for row in rows
            ]

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
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass  # BUSY 发生在 BEGIN 阶段时事务未开启：无可回滚，让原始错误上抛
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
    def _next_seq(conn: sqlite3.Connection, run_id: str) -> int:
        """事务内分配：MAX(seq)+1，保证每个 run 内严格递增无空洞（等价 fakes 锁内 len+1）。"""
        return conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM run_events WHERE run_id = ?", (run_id,)
        ).fetchone()[0]

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


@_retry_public_methods_on_locked
class SqliteLedger:
    """ToolLedger 契约的 SQLite 实现：写工具幂等账本。

    与 fakes.InMemoryLedger 语义一致：
    - begin 同键返回原记录（completed 的结果一并返回，调用方不再执行工具）；
    - fail(retryable=True) → unknown（可能已执行，禁止自动重试）；False → failed；
    - complete / fail 对不存在的键抛 KeyError（编程错误）。

    表结构随 SqliteRunStore 的迁移一起建（生产路径 worker 先建 store）；本类
    独立初始化时用 IF NOT EXISTS 自建表、不动 user_version（不参与库级版本管理）。
    """

    def __init__(self, path: str, *, clock: Clock | None = None, busy_timeout_ms: int = 5000) -> None:
        self._clock = clock
        self.busy_timeout_ms = busy_timeout_ms
        self._conn = sqlite3.connect(
            path, timeout=busy_timeout_ms / 1000, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self._conn.executescript(_LEDGER_SCHEMA)

    def begin(self, key: str, tool: str, args_hash: str) -> LedgerEntry:
        """登记一次工具调用意图；同键已存在（含崩溃遗留的 in_progress）→ 返回原记录。"""
        with self._lock, self._tx() as conn:
            row = conn.execute("SELECT * FROM tool_ledger WHERE key = ?", (key,)).fetchone()
            if row is not None:
                return self._entry(row)
            conn.execute(
                "INSERT INTO tool_ledger (key, tool, args_hash, status, updated_at)"
                " VALUES (?, ?, ?, 'in_progress', ?)",
                (key, tool, args_hash, self._now_us()),
            )
            row = conn.execute("SELECT * FROM tool_ledger WHERE key = ?", (key,)).fetchone()
            assert row is not None
            return self._entry(row)

    def complete(self, key: str, result: dict) -> None:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tool_ledger SET status = 'completed', result = ?, error = NULL,"
                " retryable = NULL, updated_at = ? WHERE key = ?",
                (json.dumps(result, ensure_ascii=False), self._now_us(), key),
            )
            if cur.rowcount != 1:
                raise KeyError(key)

    def fail(self, key: str, error: str, retryable: bool) -> None:
        """retryable=True → unknown（可能已执行，禁止自动重试）；False → failed。"""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tool_ledger SET status = ?, error = ?, retryable = ?, updated_at = ?"
                " WHERE key = ?",
                ("unknown" if retryable else "failed", error, int(retryable), self._now_us(), key),
            )
            if cur.rowcount != 1:
                raise KeyError(key)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield self._conn
        except BaseException:
            try:
                self._conn.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass  # BUSY 发生在 BEGIN 阶段时事务未开启：无可回滚，让原始错误上抛
            raise
        self._conn.execute("COMMIT")

    def _now_us(self) -> int:
        if self._clock is not None:
            return to_us(self._clock())
        seconds = self._conn.execute("SELECT unixepoch('subsec')").fetchone()[0]
        return int(round(seconds * 1_000_000))

    def _entry(self, row: sqlite3.Row) -> LedgerEntry:
        return LedgerEntry(
            key=row["key"],
            tool=row["tool"],
            args_hash=row["args_hash"],
            status=row["status"],
            result=json.loads(row["result"]) if row["result"] is not None else None,
            error=row["error"],
            retryable=bool(row["retryable"]) if row["retryable"] is not None else None,
            updated_at=from_us(row["updated_at"]),
        )
