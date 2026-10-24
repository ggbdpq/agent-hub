"""A5：kill -9 故障注入（验收硬门槛，计划 §7.1-A5）。

子进程真实认领并持有租约 → 父进程 SIGKILL（无清理、无续租）→ 租约到期后
回收器扫描到过期 → 新 worker 接管（attempt=2）→ 完成。全程使用 SQLite 时间函数
（未注入时钟），验证跨进程的真实时间语义。
"""

import os
import subprocess
import sys
import time

from app.runtime import Reclaimer, SqliteRunStore

# 子进程脚本：认领 run 后死循环心跳（等待被杀）
_CHILD_SCRIPT = f"""
import sys, time
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
from app.contracts.events import NewEvent
from app.runtime import SqliteRunStore

db, run_id = sys.argv[1], sys.argv[2]
store = SqliteRunStore(db)  # 未注入时钟：租约判定用 SQLite 时间函数（真实时钟）
claim = store.claim("victim-worker", lease_s=2)
assert claim is not None and claim.run_id == run_id
store.append_event(claim.run_id, claim.fencing_token, NewEvent(type="node.started"))
while True:
    time.sleep(0.1)
    store.heartbeat(claim.run_id, claim.fencing_token, lease_s=2)
"""


def test_kill9_then_reclaimed_within_lease(tmp_path) -> None:
    db = str(tmp_path / "runs.sqlite")
    parent = SqliteRunStore(db)  # 不注入时钟：与子进程共享数据库时钟
    run = parent.create_run("inspection", {"site": "s1"})

    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT, db, run.run_id],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    try:
        # 等子进程完成认领并开始持有租约
        deadline = time.time() + 15
        record = None
        while time.time() < deadline:
            record = parent.get(run.run_id)
            if record is not None and record.status == "running" and record.lease_owner == "victim-worker":
                break
            time.sleep(0.05)
        assert record is not None and record.lease_owner == "victim-worker", (
            f"子进程未在期限内认领：status={record and record.status}"
        )

        proc.kill()  # SIGKILL：进程无任何清理机会，租约与心跳戛然而止
        proc.wait()

        # 回收器在租约期（2s）+ 余量内必须扫描到过期租约
        reclaimer = Reclaimer(parent, interval_s=10.0)
        deadline = time.time() + 5
        expired: list = []
        while time.time() < deadline:
            expired = reclaimer.scan_once()
            if expired:
                break
            time.sleep(0.05)
        assert [r.run_id for r in expired] == [run.run_id], "租约期内未回收：任务会永远 running"

        # 新 worker 接管（attempt/fencing 递增）并完成——任务最终结束，不僵尸
        claim = parent.claim("replacement", lease_s=15)
        assert claim is not None and claim.run_id == run.run_id
        assert claim.attempt == 2 and claim.fencing_token == 2
        assert parent.finish(run.run_id, claim.fencing_token, "completed", result={"by": "replacement"})
        assert parent.get(run.run_id).status == "completed"
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        parent.close()
