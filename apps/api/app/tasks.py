"""异步任务状态机（养护方案生成）。

- 状态推进 queued → running(progress) → completed/failed/cancelled → archived
- 进程内 store + 本地快照；重启恢复时读回快照、续单号，并把 queued/running 任务
  重新调度续跑（从快照进度继续，不归零）
- 确定性失败：taskId 尾号 == 7 时失败（演示失败分级与重试）
- confirm 归档：前端确认保存后任务进入 archived
"""
import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from pathlib import Path

from app.schemas import TaskResult, TaskSection, TaskSnapshot, TaskStatus

logger = logging.getLogger(__name__)

# 进程内任务表（重启读回快照并续跑在途任务；页面刷新续追要求 API 进程持续运行）
_TASKS: dict[str, TaskSnapshot] = {}

# 后台任务强引用（asyncio 文档：裸 create_task 的引用可能被 GC 中途回收）；
# 完成后经 done_callback 自动移除
_BACKGROUND: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    """创建后台任务并持有强引用，完成后自动从集合移除。"""
    task = asyncio.create_task(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task

# 快照目录：环境变量优先，默认系统临时目录
SNAPSHOT_DIR = Path(os.environ.get("AGENT_HUB_SNAPSHOT_DIR", Path(os.environ.get("TEMP", "/tmp")) / "agent-hub-snapshots"))

# 每步推进的间隔秒数（演示节奏）
TICK_SECONDS = 0.4
TOTAL_TICKS = 10

# 业务单号：YH(养护)+日期+每日序号，每日从 001 递增；重启后从快照续号
_SEQ_DATE = ""
_SEQ_N = 0


def _today() -> str:
    return datetime.now().strftime("%Y%m%d")


def _next_task_no() -> str:
    global _SEQ_DATE, _SEQ_N
    today = _today()
    if _SEQ_DATE != today:
        _SEQ_DATE, _SEQ_N = today, 0
    _SEQ_N += 1
    return f"YH{today}{_SEQ_N:03d}"


def _snapshot_path(task_id: str) -> Path:
    return SNAPSHOT_DIR / f"{task_id}.json"


def _persist(task: TaskSnapshot) -> None:
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = _snapshot_path(task.taskId)
    # 先写临时文件再原子替换：写到一半崩溃时保留上一版快照（.tmp 不匹配 restore 的 *.json）
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(task.model_dump_json(), encoding="utf-8")
    os.replace(tmp, path)


def restore_snapshots(schedule: bool = True) -> int:
    """重启恢复：把快照目录里的任务读回内存，并把当日单号计数器续到最大值。

    queued/running 的任务重新调度续跑（从快照进度继续）；schedule=False 只读回不调度，
    供无事件循环的单测使用。旧格式快照（缺 taskNo）无法通过校验，逐个跳过不中断服务。
    """
    if not SNAPSHOT_DIR.exists():
        return 0
    count = 0
    for path in SNAPSHOT_DIR.glob("*.json"):
        try:
            task = TaskSnapshot.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as e:  # 旧格式/损坏快照：跳过，但留下痕迹
            logger.warning("跳过无法恢复的任务快照 %s：%s", path.name, e)
            continue
        if task.taskId not in _TASKS:
            _TASKS[task.taskId] = task
            count += 1
            if schedule and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                _spawn(_run(task.taskId, resume=True))
        global _SEQ_DATE, _SEQ_N
        if task.taskNo.startswith("YH") and task.taskNo[2:10] == _today():
            _SEQ_DATE = _today()
            _SEQ_N = max(_SEQ_N, int(task.taskNo[10:]))
    return count


def get_task(task_id: str) -> TaskSnapshot | None:
    return _TASKS.get(task_id)


def cancel_task(task_id: str) -> TaskSnapshot | None:
    task = _TASKS.get(task_id)
    if task and task.status in (TaskStatus.QUEUED, TaskStatus.RUNNING):
        task.status = TaskStatus.CANCELLED
        _persist(task)
    return task


def confirm_task(task_id: str) -> TaskSnapshot | None:
    """前端确认保存后归档。"""
    task = _TASKS.get(task_id)
    if task and task.status == TaskStatus.COMPLETED:
        task.status = TaskStatus.ARCHIVED
        _persist(task)
    return task


def _should_fail(task_id: str) -> bool:
    """确定性失败演示：taskId 哈希尾号 7 → failed（可复现，便于前端演示失败态）。"""
    return hashlib.sha256(task_id.encode()).hexdigest()[-1] == "7"


async def create_task() -> TaskSnapshot:
    task_id = uuid.uuid4().hex[:12]
    task = TaskSnapshot(
        taskId=task_id,
        taskNo=_next_task_no(),
        kind="care_plan",
        status=TaskStatus.QUEUED,
        progress=0,
    )
    _TASKS[task_id] = task
    _persist(task)
    _spawn(_run(task_id))
    return task


async def _run(task_id: str, resume: bool = False) -> None:
    task = _TASKS.get(task_id)
    # resume：接受重启恢复时快照里的 RUNNING 态，从上次进度继续；新调度只接受 QUEUED
    allowed = (TaskStatus.QUEUED, TaskStatus.RUNNING) if resume else (TaskStatus.QUEUED,)
    if task is None or task.status not in allowed:
        return
    task.status = TaskStatus.RUNNING
    _persist(task)
    try:
        # 进度每 tick +10：续跑从 progress//10+1 个 tick 继续（40% → 从第 5 个 tick 开始）
        start_tick = task.progress // 10 + 1 if resume else 1
        for tick in range(start_tick, TOTAL_TICKS + 1):
            await asyncio.sleep(TICK_SECONDS)
            current = _TASKS.get(task_id)
            if current is None or current.status == TaskStatus.CANCELLED:
                return  # 用户取消：状态已由 cancel_task 落库
            current.progress = int(tick / TOTAL_TICKS * 100)
            _persist(current)
        if _should_fail(task_id):
            task.status = TaskStatus.FAILED
            task.failReason = "养护方案生成超时（模拟失败：演示失败分级与重试）"
        else:
            task.status = TaskStatus.COMPLETED
            task.result = TaskResult(
                title=f"养护方案 {task.taskNo}",
                sections=[
                    TaskSection(heading="浇水", body="玫瑰类 2 天一次，沿盆沿浇，避开花头；桔梗见干见湿。"),
                    TaskSection(heading="光照", body="每日 4 小时散射光即可，避免正午直晒。"),
                    TaskSection(heading="换水修剪", body="夏季 2 天换水一次，45° 斜剪根部 1-2cm。"),
                ],
            )
        _persist(task)
    except Exception as exc:  # 兜底：任何异常都落到 failed 态，不让任务悬死
        task.status = TaskStatus.FAILED
        task.failReason = f"内部错误：{exc}"
        _persist(task)
