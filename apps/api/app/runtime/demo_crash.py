"""崩溃 → 回收 → 续跑 → 完成 的端到端演示（计划 §7.1-A9 验收命令）。

用法（apps/api 目录下）::

    .venv/bin/python -m app.runtime.demo_crash

流程：
1. 父进程建临时库、创建 inspection run（12 台设备）；
2. 启动**真实子进程** worker（victim-worker），认领并开始执行（心跳保活）；
3. 父进程 SIGKILL 子进程——无清理机会，租约与心跳戛然而止；
4. 租约（2s）到期后回收器扫描确认过期（claim 语义已把它视为可接管）；
5. 父进程的 replacement-worker 接管（run.resumed，attempt=2）并跑完；
6. 打印完整事件序列与最终状态。

说明：开发期 runner 为 ScriptedAgentRunner，事件粒度到 node 级（不逐设备发事件）；
"victim 崩溃前执行到第几台"由 B 的编排层检查点负责恢复，本演示只证明运行时机制：
接管、attempt 区分两次执行、终态最终落盘。
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile

from app.contracts.fakes import ScriptedAgentRunner
from app.runtime import Reclaimer, RuntimeWorker, SqliteLedger, SqliteRunStore
from app.runtime.worker import WorkerConfig

_API_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LEASE_S = 2  # 演示用短租约，崩溃后 ~2s 内即可被接管


async def _child(db: str, run_id: str) -> None:
    """子进程：认领目标 run 并持续执行（等待被父进程 SIGKILL）。"""
    store = SqliteRunStore(db)  # 未注入时钟：租约判定用 SQLite 时间函数
    ledger = SqliteLedger(db)
    worker = RuntimeWorker(
        store,
        ledger,
        ScriptedAgentRunner(delay_s=0.3),
        config=WorkerConfig(
            worker_id="victim-worker",
            lease_s=_LEASE_S,
            poll_interval_s=0.05,
            cancel_poll_s=0.05,
        ),
    )
    stop = asyncio.Event()
    try:
        await asyncio.wait_for(worker.run_forever(stop), timeout=120)
    except TimeoutError:
        pass  # 被杀前的兜底退出


async def _parent() -> None:
    db = os.path.join(tempfile.mkdtemp(prefix="demo-crash-"), "runs.sqlite")
    store = SqliteRunStore(db)
    ledger = SqliteLedger(db)

    devices = [f"DE-{i:03d}" for i in range(1, 13)]
    run = store.create_run("inspection", {"site": "demo-site", "device_ids": devices})
    print(f"[创建] run={run.run_id[:8]}…  设备 {len(devices)} 台  库={db}\n")

    child = subprocess.Popen(
        [sys.executable, "-m", "app.runtime.demo_crash", "--child", db, run.run_id],
        cwd=_API_ROOT,
    )
    try:
        # 等子进程完成认领
        while True:
            record = store.get(run.run_id)
            if (
                record is not None
                and record.status == "running"
                and record.lease_owner == "victim-worker"
            ):
                break
            await asyncio.sleep(0.05)
        print(f"[认领] victim-worker 持有租约（lease={_LEASE_S}s，心跳≈{_LEASE_S / 3:.2f}s）")
        await asyncio.sleep(1.0)  # 让 victim 跑一会儿（约 3 台设备）

        child.kill()  # SIGKILL：模拟 kill -9，进程无任何清理机会
        child.wait()
        print("[崩溃] victim-worker 已被 SIGKILL")

        reclaimer = Reclaimer(store, interval_s=10.0)
        while not reclaimer.scan_once():
            await asyncio.sleep(0.05)
        print("[回收] 租约过期被扫描确认（接管权开放）")

        replacement = RuntimeWorker(
            store,
            ledger,
            ScriptedAgentRunner(delay_s=0.01),
            config=WorkerConfig(
                worker_id="replacement-worker",
                lease_s=5,
                poll_interval_s=0.05,
                cancel_poll_s=0.05,
            ),
        )
        await replacement.run_one()
        print("[续跑] replacement-worker 接管并完成\n")

        print("事件序列：")
        for e in store.events_after(run.run_id, 0):
            print(f"  seq={e.seq:>2}  attempt={e.attempt}  {e.type:<15} node={e.node or '-'}")
        final = store.get(run.run_id)
        print(f"\n最终状态：{final.status}  attempt={final.attempt}  fencing_token={final.fencing_token}")
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        store.close()
        ledger.close()


def main() -> None:
    if len(sys.argv) >= 4 and sys.argv[1] == "--child":
        asyncio.run(_child(sys.argv[2], sys.argv[3]))
    else:
        asyncio.run(_parent())


if __name__ == "__main__":
    main()
