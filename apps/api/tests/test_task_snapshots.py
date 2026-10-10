"""任务快照：重启恢复不覆盖内存里的同 id 任务。"""
from app import tasks
from app.schemas import TaskSnapshot, TaskStatus


def _task(task_id: str, status: TaskStatus, progress: int) -> TaskSnapshot:
    return TaskSnapshot(taskId=task_id, taskNo="YH20260101001", kind="care_plan", status=status, progress=progress)


def test_restore_keeps_live_task(tmp_path, monkeypatch):
    live = _task("abc123", TaskStatus.RUNNING, 50)
    monkeypatch.setattr(tasks, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(tasks, "_TASKS", {live.taskId: live})
    (tmp_path / "abc123.json").write_text(
        _task("abc123", TaskStatus.QUEUED, 0).model_dump_json(), encoding="utf-8"
    )

    restored = tasks.restore_snapshots()

    assert restored == 0
    assert tasks._TASKS["abc123"] is live
    assert (live.status, live.progress) == (TaskStatus.RUNNING, 50)
