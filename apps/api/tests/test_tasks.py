"""任务状态机：推进/取消/确认归档/确定性失败。"""
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app

# with 上下文持有 portal：后台 asyncio 任务的生命周期跨越多次请求
# （裸 TestClient 每个请求独立事件循环，create_task 会被冻结）
@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def wait_for_terminal(client, task_id: str, timeout: float = 8.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = client.get(f"/api/tasks/{task_id}").json()
        if task["status"] in ("completed", "failed", "cancelled"):
            return task
        time.sleep(0.2)
    raise AssertionError("任务未在时限内到达终态")


def test_task_runs_to_completed(client, monkeypatch):
    """主路径：强制非失败分支，任务跑到 completed。"""
    from app import tasks as tasks_module

    monkeypatch.setattr(tasks_module, "_should_fail", lambda _task_id: False)
    task = client.post("/api/tasks").json()
    assert task["status"] == "queued"
    final = wait_for_terminal(client, task["taskId"])
    assert final["status"] == "completed"
    assert final["progress"] == 100
    assert final["result"] is not None
    assert len(final["result"]["sections"]) == 3


def test_task_cancel(client):
    task = client.post("/api/tasks").json()
    cancelled = client.post(f"/api/tasks/{task['taskId']}/cancel").json()
    assert cancelled["status"] == "cancelled"
    # 终态后再确认归档：不满足条件，保持 cancelled
    again = client.post(f"/api/tasks/{task['taskId']}/confirm").json()
    assert again["status"] == "cancelled"


def test_task_confirm_archives(client, monkeypatch):
    """completed → confirm → archived。"""
    from app import tasks as tasks_module

    monkeypatch.setattr(tasks_module, "_should_fail", lambda _task_id: False)
    task = client.post("/api/tasks").json()
    final = wait_for_terminal(client, task["taskId"])
    assert final["status"] == "completed"
    archived = client.post(f"/api/tasks/{task['taskId']}/confirm").json()
    assert archived["status"] == "archived"


def test_task_deterministic_failure(client, monkeypatch):
    """强制失败分支：failed + failReason，confirm 不改变失败态。"""
    from app import tasks as tasks_module

    monkeypatch.setattr(tasks_module, "_should_fail", lambda _task_id: True)
    task = client.post("/api/tasks").json()
    final = wait_for_terminal(client, task["taskId"])
    assert final["status"] == "failed"
    assert final["failReason"]
    archived = client.post(f"/api/tasks/{task['taskId']}/confirm").json()
    assert archived["status"] == "failed"


def test_task_not_found(client):
    assert client.get("/api/tasks/nonexistent").status_code == 404


# ---------------- WP6：重启续跑与后台任务强引用（追加，原用例不动） ----------------

def _wp6_task(task_id: str, status, progress: int):
    from app.schemas import TaskSnapshot
    return TaskSnapshot(taskId=task_id, taskNo="YH20260101001", kind="care_plan", status=status, progress=progress)


def _tasks_module():
    from app import tasks as tasks_module
    return tasks_module


def test_restore_resumes_running_snapshot(tmp_path, monkeypatch):
    """running+progress=40 的快照恢复后续跑到终态，进度从 40 继续、不归零。"""
    from app.schemas import TaskStatus

    tasks = _tasks_module()
    monkeypatch.setattr(tasks, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(tasks, "_TASKS", {})
    monkeypatch.setattr(tasks, "_should_fail", lambda _tid: False)
    (tmp_path / "wp6resume01.json").write_text(
        _wp6_task("wp6resume01", TaskStatus.RUNNING, 40).model_dump_json(), encoding="utf-8"
    )

    observed: list[int] = []
    with TestClient(app) as client:  # lifespan 调 restore_snapshots → 重新调度续跑
        import time

        deadline = time.time() + 8.0
        final = None
        while time.time() < deadline:
            snap = client.get("/api/tasks/wp6resume01").json()
            observed.append(snap["progress"])
            if snap["status"] in ("completed", "failed"):
                final = snap
                break
            time.sleep(0.05)

    assert final is not None, f"续跑任务未在时限内到达终态，observed={observed}"
    assert final["status"] == "completed"
    assert final["progress"] == 100
    assert all(p >= 40 for p in observed), f"进度出现回退/归零：{observed}"
    assert final["result"] is not None


def test_restore_does_not_duplicate_task_ids(tmp_path, monkeypatch):
    """同一 taskId 的快照不会重复载入（回归旧 :68 的 status not in _TASKS）。"""
    from app.schemas import TaskStatus

    tasks = _tasks_module()
    monkeypatch.setattr(tasks, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(tasks, "_TASKS", {})
    (tmp_path / "wp6dup01.json").write_text(
        _wp6_task("wp6dup01", TaskStatus.QUEUED, 0).model_dump_json(), encoding="utf-8"
    )
    assert tasks.restore_snapshots(schedule=False) == 1
    assert tasks.restore_snapshots(schedule=False) == 0
    assert len(tasks._TASKS) == 1


def test_background_tasks_strongly_referenced_and_reaped(client):
    """create_task 后 _BACKGROUND 持有任务（防 GC），完成后被移除。"""
    import time

    tasks = _tasks_module()
    before = set(tasks._BACKGROUND)
    created = client.post("/api/tasks").json()
    live = tasks._BACKGROUND - before
    assert live, "后台任务未被强引用持有"
    assert all(not t.done() for t in live)
    wait_for_terminal(client, created["taskId"])
    time.sleep(0.3)  # 等 done_callback 执行
    assert all(t.done() for t in tasks._BACKGROUND)


def test_restore_only_schedules_incomplete_tasks(tmp_path, monkeypatch):
    """queued/running 快照重新调度（resume=True）；completed/archived/cancelled/failed 不调度。"""
    from app.schemas import TaskStatus

    tasks = _tasks_module()
    monkeypatch.setattr(tasks, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(tasks, "_TASKS", {})
    for tid, status in [
        ("wp6que01", TaskStatus.QUEUED),
        ("wp6run01", TaskStatus.RUNNING),
        ("wp6done1", TaskStatus.COMPLETED),
        ("wp6arch1", TaskStatus.ARCHIVED),
        ("wp6canc1", TaskStatus.CANCELLED),
        ("wp6fail1", TaskStatus.FAILED),
    ]:
        (tmp_path / f"{tid}.json").write_text(_wp6_task(tid, status, 30).model_dump_json(), encoding="utf-8")

    spawned: list[tuple[str, bool]] = []

    def fake_run(task_id, resume=False):
        spawned.append((task_id, resume))

        async def noop():
            pass

        return noop()

    def fake_spawn(coro):
        coro.close()  # 不真正执行，避免需要事件循环
        return None

    monkeypatch.setattr(tasks, "_run", fake_run)
    monkeypatch.setattr(tasks, "_spawn", fake_spawn)
    assert tasks.restore_snapshots() == 6
    assert sorted(spawned) == [("wp6que01", True), ("wp6run01", True)]
