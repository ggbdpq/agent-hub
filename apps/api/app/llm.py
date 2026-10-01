"""真实 LLM 适配层：OpenAI 兼容协议（deepseek / kimi / glm / openai 通用）。

设计要点：
- 供应商差异收敛在 BASE_URL 表 + 环境变量，前端与事件协议零改动（协议即适配层）；
- 流式输出里推荐结论用 ```card ...``` 围栏承载结构化 JSON：
  后端做流式围栏状态机——围栏内缓冲、闭合后 pydantic 校验并注入服务端依据，
  校验失败/围栏未闭合降级为普通 token 文本（容错降级）；
- 证据（evidenceRefs）由服务端按场景注入，不让模型幻觉证据 ID；
- 凭据只从环境变量/.env 读取，不落代码。
"""
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import NamedTuple

import httpx

from app.evidence import get_evidence
from app.schemas import (
    CardEvent,
    DoneEvent,
    EvidenceEvent,
    EvidenceRef,
    FlowerCardPayload,
    RecommendScene,
    TokenEvent,
)
from app.sse import sse_frame

# .env 极简加载（KEY=VALUE 逐行），避免为个人项目引入 python-dotenv
_ENV_LOADED = False


def load_env() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_file = Path(__file__).resolve().parents[1] / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    _ENV_LOADED = True


class Provider(NamedTuple):
    name: str
    base_url: str
    api_key: str
    model: str


PROVIDER_BASE_URLS = {
    "deepseek": "https://api.deepseek.com/v1",
    "kimi": "https://api.moonshot.cn/v1",
    "glm": "https://open.bigmodel.cn/api/paas/v4",
    "openai": "https://api.openai.com/v1",
}

# 各供应商的默认模型（可用 OPENAI_MODEL 覆盖）
PROVIDER_DEFAULT_MODEL = {
    "deepseek": "deepseek-chat",
    "kimi": "moonshot-v1-8k",
    "glm": "glm-4-flash",
    "openai": "gpt-4o-mini",
}


def resolve_provider() -> Provider | None:
    """读取环境变量解析供应商。未配置任何 key 时返回 None（回退剧本模式）。"""
    load_env()
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None
    provider_name = os.environ.get("OPENAI_PROVIDER", "")
    base_url = os.environ.get("OPENAI_BASE_URL") or PROVIDER_BASE_URLS.get(
        provider_name, PROVIDER_BASE_URLS["openai"]
    )
    model = os.environ.get("OPENAI_MODEL", PROVIDER_DEFAULT_MODEL.get(provider_name, "gpt-4o-mini"))
    return Provider(name=provider_name or "custom", base_url=base_url, api_key=api_key, model=model)


SYSTEM_PROMPT = """你是线上花店的花语顾问，帮助顾客挑选鲜花。用简体中文，语气亲切专业。

输出约定（严格遵守）：
1. 分析与建议用普通文本直接输出；
2. 给出具体商品推荐时，必须把推荐包在 ```card 围栏里，围栏内是一段 JSON：
{"scene": "daily|gift|comfort 之一", "title": "推荐标题", "reason": "一句话推荐理由", "items": [{"id": "唯一id", "name": "花名", "priceFen": 整数价格(分), "typeLabel": "花束|盆栽|花盒|永生花", "emoji": "花类 emoji"}]}
3. priceFen 是分（例：69 元写 6900），items 至少 1 个；
4. 不要自己编造证据编号，依据由系统按场景自动附加；
5. 每次回答输出 1 个 card 围栏和一段简短文字说明。
"""

# 服务端按场景注入依据（模型不产生证据 ID——防幻觉）
SCENE_EVIDENCE = {
    RecommendScene.DAILY: ["ev-purchase-01", "ev-care-03"],
    RecommendScene.GIFT: ["ev-survey-02"],
    RecommendScene.COMFORT: ["ev-care-03"],
}

_TOKEN_SLICE = 12
_CARD_OPEN = "```card"
_FENCE_CLOSE = "```"


def _scene_evidence_refs(scene: RecommendScene) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for evidence_id in SCENE_EVIDENCE.get(scene, []):
        evidence = get_evidence(evidence_id)
        refs.append(
            EvidenceRef(evidenceId=evidence_id, label=evidence.source if evidence else evidence_id)
        )
    return refs


async def llm_stream(
    message: str,
    provider: Provider,
    client: httpx.AsyncClient | None = None,
) -> AsyncIterator[str]:
    """从 OpenAI 兼容接口拉流式补全，映射为花语事件协议。"""
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=120, write=10, pool=10))
    try:
        async for frame in _stream_with(message, provider, client):
            yield frame
    finally:
        if own_client:
            await client.aclose()


class _StreamStateMachine:
    """围栏外文本 → token；```card 围栏 → 缓冲闭合后校验成 card 事件（失败降级 token）。"""

    def __init__(self) -> None:
        self.buffer = ""  # 尚未判定的字符（保留可能被劈开的标记尾部）
        self.in_card = False
        self.card_buf = ""
        self.text_pending = ""  # 待凑满 _TOKEN_SLICE 的正文

    def _text_frames(self, text: str) -> list[str]:
        self.text_pending += text
        frames: list[str] = []
        while len(self.text_pending) >= _TOKEN_SLICE:
            frames.append(
                sse_frame("token", TokenEvent(type="token", text=self.text_pending[:_TOKEN_SLICE]))
            )
            self.text_pending = self.text_pending[_TOKEN_SLICE:]
        return frames

    def _card_frames(self, json_text: str) -> list[str]:
        try:
            data = json.loads(json_text)
            scene = RecommendScene(data.get("scene"))
            data.pop("evidenceRefs", None)  # 模型不产生证据，服务端注入
            refs = _scene_evidence_refs(scene)
            payload = FlowerCardPayload.model_validate({**data, "evidenceRefs": refs})
        except Exception:
            # 容错降级：结构化校验失败，按普通文本吐出，不中断对话
            return self._text_frames(json_text)
        frames = [sse_frame("card", CardEvent(type="card", payload=payload))]
        for evidence_ref in refs:  # 依据随卡片推送（结论与依据分离，按 id 装配）
            evidence = get_evidence(evidence_ref.evidenceId)
            if evidence is not None:
                frames.append(sse_frame("evidence", EvidenceEvent(type="evidence", evidence=evidence)))
        return frames

    def feed(self, delta: str) -> list[str]:
        frames: list[str] = []
        self.buffer += delta
        while True:
            hold = (len(_FENCE_CLOSE) if self.in_card else len(_CARD_OPEN)) - 1
            if len(self.buffer) <= hold:
                break
            if self.in_card:
                idx = self.buffer.find(_FENCE_CLOSE)
                if idx == -1:
                    self.card_buf += self.buffer[:-hold]
                    self.buffer = self.buffer[-hold:]
                    break
                self.card_buf += self.buffer[:idx]
                self.buffer = self.buffer[idx + len(_FENCE_CLOSE) :].lstrip("\r\n")
                self.in_card = False
                frames += self._card_frames(self.card_buf)
                self.card_buf = ""
            else:
                idx = self.buffer.find(_CARD_OPEN)
                if idx != -1:
                    frames += self._text_frames(self.buffer[:idx])
                    self.buffer = self.buffer[idx + len(_CARD_OPEN) :].lstrip("\r\n")
                    self.in_card = True
                else:
                    frames += self._text_frames(self.buffer[:-hold])
                    self.buffer = self.buffer[-hold:]
                    break
        return frames

    def flush(self) -> list[str]:
        frames: list[str] = []
        if self.in_card:
            # 围栏未闭合：降级为普通文本
            frames += self._text_frames(self.card_buf)
            self.card_buf = ""
        frames += self._text_frames(self.buffer)
        self.buffer = ""
        if self.text_pending:
            frames.append(sse_frame("token", TokenEvent(type="token", text=self.text_pending)))
            self.text_pending = ""
        return frames


async def _stream_with(message: str, provider: Provider, client: httpx.AsyncClient) -> AsyncIterator[str]:
    request_body = {
        "model": provider.model,
        "stream": True,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": message},
        ],
    }
    state = _StreamStateMachine()
    async with client.stream(
        "POST",
        f"{provider.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {provider.api_key}"},
        json=request_body,
    ) as response:
        if response.status_code != 200:
            detail = (await response.aread()).decode("utf-8", "replace")[:200]
            raise RuntimeError(f"{provider.name} HTTP {response.status_code}: {detail}")
        async for line in response.aiter_lines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line.removeprefix("data:").strip()
            if data == "[DONE]":
                break
            delta = json.loads(data)["choices"][0]["delta"].get("content") or ""
            for frame in state.feed(delta):
                yield frame
        for frame in state.flush():
            yield frame
        yield sse_frame("done", DoneEvent(type="done", replyId=provider.model))
