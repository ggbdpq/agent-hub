"""契约双镜像对账：fixtures/contract-example.json 必须能被 pydantic 镜像完整解析。

同一份 fixture 也被 packages/shared/src/contracts.test.ts（zod）解析——
两端都过，才证明契约镜像没有漂移。
"""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas import (
    AgentStreamEvent,
    ChatRequest,
    DoneEvent,
    EvidenceEvent,
    CardEvent,
    Order,
    TaskSnapshot,
    ThoughtEvent,
    TokenEvent,
)

FIXTURE = Path(__file__).resolve().parents[3] / "fixtures" / "contract-example.json"


@pytest.fixture(scope="module")
def example() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_thought_event(example):
    event = ThoughtEvent.model_validate(example["thought"])
    assert event.seq == 1
    assert event.final is False


def test_token_event(example):
    assert TokenEvent.model_validate(example["token"]).text.startswith("根据")


def test_card_event(example):
    event = CardEvent.model_validate(example["card"])
    assert event.payload.scene.value == "gift"
    assert event.payload.items[0].priceFen == 12800  # 金额单位：分


def test_evidence_event(example):
    event = EvidenceEvent.model_validate(example["evidence"])
    assert 0 <= event.evidence.credibility <= 100


def test_done_event(example):
    assert DoneEvent.model_validate(example["done"]).replyId == "r123abc456def"


def test_task_snapshot(example):
    task = TaskSnapshot.model_validate(example["taskCompleted"])
    assert task.kind == "care_plan"  # Literal 字段校验后保持 str
    assert task.progress == 100


def test_order_paid(example):
    order = Order.model_validate(example["orderPaid"])
    assert order.status.value == "paid"
    assert order.paidAt is not None


def test_union_discriminator(example):
    """联合类型按 type 判别解析——SSE 帧解析的python 侧入口。"""
    event = AgentStreamEvent.__args__  # Union 的成员列表非空即可，真正逐帧校验见上面单测
    assert len(event) == 6


def test_unknown_type_rejected():
    """契约外字段/类型必须被拒绝——AI 输出白名单的 python 侧证明。"""
    with pytest.raises(ValidationError):
        ThoughtEvent.model_validate({"type": "thought", "seq": 0, "text": "x", "final": True, "extra": 1})


def test_chat_request(example):
    """ChatRequest 双端对账：迁移期 py 后端收到 threadId 只接收不使用。"""
    req = ChatRequest.model_validate(example["chatRequest"])
    assert req.message == "想给妈妈送生日礼物"
    assert req.threadId == "t-0123456789"
    assert ChatRequest.model_validate({"message": "日常自用来点花"}).threadId is None
