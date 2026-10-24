"""B3 附带：真实模型适配（OpenAI 兼容）契约测试——本地假 /chat/completions 上游。

不做真实网络调用；验证请求组装、响应解析与 usage 透传。
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.models import OpenAICompatChatModel, env_chat_model_factory


def _fake_upstream(responses: dict, received: dict):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            received.update(body)
            data = json.dumps(responses).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:  # 静默
            pass

    return Handler


def test_openai_compat_roundtrip_with_usage() -> None:
    received: dict = {}
    upstream = {
        "choices": [{"message": {"role": "assistant", "content": "收到"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    }
    Handler = _fake_upstream(upstream, received)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        model = OpenAICompatChatModel(
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="test-key",
            model="test-model",
        )
        result = model.invoke([SystemMessage("系统提示"), HumanMessage("用户输入")])
    finally:
        server.shutdown()

    assert result.content == "收到"
    assert received["model"] == "test-model"
    assert received["messages"][0] == {"role": "system", "content": "系统提示"}
    assert received["messages"][1] == {"role": "user", "content": "用户输入"}
    assert result.usage_metadata["total_tokens"] == 7


def test_upstream_error_raises_runtime_error() -> None:
    received: dict = {}

    class ErrHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["Content-Length"]))
            data = b'{"error": {"message": "boom"}}'
            self.send_response(500)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), ErrHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        model = OpenAICompatChatModel(
            base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="k", model="m"
        )
        with pytest.raises(RuntimeError, match="HTTP 500"):
            model.invoke([HumanMessage("x")])
    finally:
        server.shutdown()


def test_env_factory_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    factory = env_chat_model_factory()
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        factory()
