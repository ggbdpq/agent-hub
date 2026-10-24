"""契约双镜像对账：fixtures/contract-example.json 中 Python 仍在使用的部分必须能被 pydantic 解析。

智能体事件（thought/token/card/evidence/done）与 chatRequest 自 WP9 起只由 TS 侧
（packages/shared/src/contracts.test.ts）校验——Python 不再消费这些结构。
fixture 按"只追加不删除"原则保留它们的样例。
"""
import json
from pathlib import Path

import pytest

from app.schemas import (
    Order,
    RecommendScene,
    TaskSnapshot,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "contract-example.json"


@pytest.fixture(scope="module")
def example() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_task_snapshot(example):
    task = TaskSnapshot.model_validate(example["taskCompleted"])
    assert task.kind == "care_plan"  # Literal 字段校验后保持 str
    assert task.progress == 100


def test_order_paid(example):
    order = Order.model_validate(example["orderPaid"])
    assert order.status.value == "paid"
    assert order.paidAt is not None


def test_scene_enum():
    """场景枚举仍被 /api/scenes 使用。"""
    assert [s.value for s in RecommendScene] == ["daily", "gift", "comfort"]


def test_invalid_status_rejected():
    """枚举外的状态值必须被拒绝——pydantic 侧白名单证明（pydantic 默认忽略额外字段，故用非法值断言）。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TaskSnapshot.model_validate(
            {
                "taskId": "t1",
                "taskNo": "YH20260101001",
                "kind": "care_plan",
                "status": "hacked",
                "progress": 100,
            }
        )


def test_run_event_approval_required(example):
    """I6：巡检 RunEvent 双端对账（camelCase JSON；ts ISO 字符串）。"""
    from app.contracts.events import RunEvent

    event = RunEvent.model_validate(example["runEventApprovalRequired"])
    assert event.run_id == "r-demo-0001"
    assert event.seq == 12
    assert event.payload["approvalId"] == "r-demo-0001:approval:1"

    completed = RunEvent.model_validate(example["runEventCompleted"])
    assert completed.type == "run.completed"
    assert completed.node is None  # pydantic None → JSON null（CLAUDE.md 已知坑）


def test_inspection_report(example):
    from app.contracts.agent import InspectionReport

    report = InspectionReport.model_validate(example["inspectionReport"])
    assert report.coverage == 1.0
    assert report.work_orders[0].status == "draft"
    assert report.findings[1].level == "critical"
    # 序列化输出 camelCase（by_alias）——前端消费的形状
    dumped = report.model_dump(by_alias=True)
    assert "evidenceChunkIds" in dumped["findings"][0]
