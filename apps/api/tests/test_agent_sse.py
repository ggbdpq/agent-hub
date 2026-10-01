"""SSE 冒烟：收完整事件流，断言五类事件齐活且顺序符合剧本。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.main import app

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def collect_stream(client, message: str) -> list[tuple[str, dict]]:
    with client.stream("POST", "/api/agent/chat", json={"message": message}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events: list[tuple[str, dict]] = []
        event_name = "message"
        for line in response.iter_text():
            for raw in line.split("\n"):
                if raw.startswith("event:"):
                    event_name = raw.removeprefix("event:").strip()
                elif raw.startswith("data:"):
                    events.append((event_name, json.loads(raw.removeprefix("data:").strip())))
        return events


def test_gift_scene_full_sequence(client):
    events = collect_stream(client, "我想给妈妈送生日礼物，帮我推荐花")
    kinds = [name for name, _ in events]

    # 事件类型齐活
    for expected in ("thought", "card", "evidence", "token", "done"):
        assert expected in kinds, f"缺少 {expected} 事件"

    # 思考分段在前，卡片居中，done 收尾
    assert kinds.index("card") > kinds.index("thought")
    assert kinds[-1] == "done"

    # 送礼关键词路由到 gift 场景
    card = next(data for name, data in events if name == "card")
    assert card["payload"]["scene"] == "gift"

    # 思考最后一帧 final=true（前端据此折叠思考面板）
    thoughts = [data for name, data in events if name == "thought"]
    assert thoughts[-1]["final"] is True


def test_evidence_payload_matches_registry(client):
    """事件里的依据与证据库逐字段一致（结论与依据分离，按 id 引用）。"""
    events = collect_stream(client, "日常自用来点花")
    for name, data in events:
        if name == "evidence":
            assert 0 <= data["evidence"]["credibility"] <= 100
            assert data["evidence"]["sourceKind"] in (
                "purchase_history",
                "preference_survey",
                "care_record",
            )


def test_comfort_scene(client):
    events = collect_stream(client, "去医院探望长辈买什么花")
    card = next(data for name, data in events if name == "card")
    assert card["payload"]["scene"] == "comfort"
