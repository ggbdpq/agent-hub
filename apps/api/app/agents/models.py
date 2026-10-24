"""真实模型适配（OpenAI 兼容）：自建 ChatOpenAI 等价物（ADR-0003 后 llm.py 已删除）。

约束：不引新依赖（httpx 在运行时依赖之外）——用 stdlib urllib 同步 POST，
异步走 BaseChatModel 默认的 executor 线程桥（``_agenerate`` 缺省实现）。
开发与测试一律用 contracts/fakes.py 的 FakeLLM；本模块仅在配置了
OPENAI_API_KEY 时被 demo/集成使用。
"""

import json
import os
import urllib.error
import urllib.request
from typing import Any, List, Optional

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.contracts.agent import ChatModelFactory

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"


def message_to_dict(message: BaseMessage) -> dict:
    role = {
        "system": "system",
        "human": "user",
        "ai": "assistant",
        "tool": "tool",
    }.get(message.type, "user")
    content = message.content if isinstance(message.content, str) else json.dumps(message.content, ensure_ascii=False)
    return {"role": role, "content": content}


class OpenAICompatChatModel(BaseChatModel):
    """最小 OpenAI 兼容 chat/completions 客户端（非流式）。

    假设：上游实现标准 /chat/completions 协议（含 usage 字段可选）。
    只实现 non-streaming：巡检编排每步一条完整回复即可（YAGNI）。
    """

    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    timeout_s: float = 60.0

    @property
    def _llm_type(self) -> str:
        return "openai-compat"

    def _generate(
        self,
        messages: List[BaseMessage],
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> ChatResult:
        payload = {
            "model": self.model,
            "messages": [message_to_dict(m) for m in messages],
            "temperature": self.temperature,
        }
        if stop:
            payload["stop"] = stop
        request = urllib.request.Request(
            self.base_url.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as resp:
                body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"LLM 上游 HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM 上游不可达: {exc.reason}") from exc
        try:
            choice = body["choices"][0]["message"]
        except (KeyError, IndexError) as exc:
            raise RuntimeError(f"LLM 上游响应缺 choices: {json.dumps(body)[:300]}") from exc
        usage = body.get("usage")
        usage_metadata = None
        if usage:
            usage_metadata = {
                "input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0),
            }
        message = AIMessage(content=choice.get("content") or "", usage_metadata=usage_metadata)
        return ChatResult(generations=[ChatGeneration(message=message)])


def env_chat_model_factory() -> ChatModelFactory:
    """从环境变量构建真实模型工厂；未配置 OPENAI_API_KEY 时显式报错（不静默降级）。"""

    def build() -> OpenAICompatChatModel:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            raise RuntimeError(
                "OPENAI_API_KEY 未设置：真实模型不可用；开发与测试请使用 FakeLLM（contracts/fakes.py）"
            )
        return OpenAICompatChatModel(
            base_url=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL),
            api_key=api_key,
            model=os.environ.get("AGENT_HUB_LLM_MODEL", os.environ.get("OPENAI_MODEL", DEFAULT_MODEL)),
        )

    return build
