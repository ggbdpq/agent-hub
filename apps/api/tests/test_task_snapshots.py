"""任务快照：重启恢复不覆盖内存里的同 id 任务；写入中途失败不毁掉上一版快照。"""
import logging
from pathlib import Path

import pytest

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


def test_interrupted_persist_keeps_previous_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "SNAPSHOT_DIR", tmp_path)
    task = _task("abc123", TaskStatus.RUNNING, 30)
    tasks._persist(task)

    real_write_text = Path.write_text

    def crash_midway(self, data, *args, **kwargs):
        real_write_text(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_text", crash_midway)
    task.progress = 40
    with pytest.raises(OSError):
        tasks._persist(task)
    monkeypatch.undo()

    saved = TaskSnapshot.model_validate_json((tmp_path / "abc123.json").read_text(encoding="utf-8"))
    assert saved.progress == 30


def test_restore_logs_corrupt_snapshot(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(tasks, "SNAPSHOT_DIR", tmp_path)
    monkeypatch.setattr(tasks, "_TASKS", {})
    (tmp_path / "broken.json").write_text("{", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert tasks.restore_snapshots() == 0

    assert "broken.json" in caplog.text
