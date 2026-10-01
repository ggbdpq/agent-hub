"""LLM 适配层测试：httpx MockTransport 模拟 OpenAI 兼容流，不依赖真实 Key。"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import llm
from app.llm import Provider, llm_stream
from app.main import app

pytestmark = pytest.mark.anyio


def sse_lines(deltas: list[str]) -> list[bytes]:
    lines = []
    for delta in deltas:
        payload = json.dumps({"choices": [{"delta": {"content": delta}}]}, ensure_ascii=False)
        lines.append(f"data: {payload}\n\n".encode())
    lines.append(b"data: [DONE]\n\n")
    return lines


def make_client(respond_with: list[bytes]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"".join(respond_with))

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


PROVIDER = Provider(name="deepseek", base_url="https://mock/v1", api_key="sk-test", model="deepseek-chat")

CARD_JSON = json.dumps(
    {
        "scene": "gift",
        "title": "送礼首选",
        "reason": "浅色低香",
        "items": [{"id": "f1", "name": "香槟玫瑰", "priceFen": 9900, "typeLabel": "花束", "emoji": "🌹"}],
    },
    ensure_ascii=False,
)


async def collect(message: str, respond_with: list[bytes]) -> list[tuple[str, dict]]:
    events = []
    async for frame in llm_stream(message, PROVIDER, client=make_client(respond_with)):
        name, _, data = frame.partition("\ndata: ")
        events.append((name.removeprefix("event: "), json.loads(data)))
    return events


@pytest.mark.anyio
async def test_fenced_card_becomes_card_event_with_injected_evidence():
    """围栏 JSON → card 事件；依据由服务端按场景注入；外围文本走 token。"""
    deltas = ["你好，", "为你推荐：", "```card\n", CARD_JSON, "\n```", "，祝送礼愉快。"]
    events = await collect("送妈妈生日花", sse_lines(deltas))

    kinds = [name for name, _ in events]
    assert kinds.count("token") >= 2
    assert "card" in kinds
    assert kinds[-1] == "done"

    card = next(data for name, data in events if name == "card")
    assert card["payload"]["scene"] == "gift"
    # 依据服务端注入，模型没输出 evidenceRefs
    assert len(card["payload"]["evidenceRefs"]) == 1

    # 依据随卡片以 evidence 事件推送（抽屉数据源）
    assert "evidence" in kinds


@pytest.mark.anyio
async def test_malformed_card_degrades_to_token():
    """围栏内容非法 JSON：容错降级为普通文本，不中断对话。"""
    deltas = ["```card\n", "{这不是JSON}", "\n```", "直接文字回答"]
    events = await collect("随便聊聊", sse_lines(deltas))

    kinds = [name for name, _ in events]
    assert "card" not in kinds
    token_text = "".join(data["text"] for name, data in events if name == "token")
    assert "直接文字回答" in token_text
    assert kinds[-1] == "done"


@pytest.mark.anyio
async def test_unclosed_fence_degrades():
    """围栏未闭合（流中断）：缓冲内容降级为 token，仍发 done。"""
    deltas = ["先说结论：", "```card\n", CARD_JSON]
    events = await collect("未闭合", sse_lines(deltas))

    kinds = [name for name, _ in events]
    assert "card" not in kinds
    assert kinds[-1] == "done"


def test_resolve_provider_env(monkeypatch):
    """环境变量 → Provider；无 key → None（剧本模式）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert llm.resolve_provider() is None

    monkeypatch.setenv("OPENAI_PROVIDER", "glm")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    provider = llm.resolve_provider()
    assert provider.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert provider.model == "glm-4-flash"


def test_chat_route_uses_script_without_key(monkeypatch):
    """未配置 key：对话路由回退剧本模式（事件协议不变）。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(app) as client:
        with client.stream("POST", "/api/agent/chat", json={"message": "日常自用"}) as response:
            assert response.status_code == 200
            body = b"".join(response.iter_bytes()).decode()
            assert "event: card" in body  # 剧本模式卡片照常出现
