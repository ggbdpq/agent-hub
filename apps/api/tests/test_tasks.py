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
