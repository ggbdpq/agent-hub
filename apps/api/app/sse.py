"""SSE 帧构造：script 模式与 LLM 模式共用同一线缆协议。"""
from pydantic import BaseModel


def sse_frame(event: str, payload: BaseModel) -> str:
    """SSE 帧：event + data 两行 + 空行派发。payload 先过 pydantic 序列化，保证与契约一致。"""
    data = payload.model_dump_json()
    return f"event: {event}\ndata: {data}\n\n"
