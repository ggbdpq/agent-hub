"""工具契约（ADR-0009）：ToolSpec 注册表单元。

权限（effect × allowed_nodes）、超时与重试策略由工具执行器（B2）用代码强制，
不写在提示词里。handler 只收已校验的入参；需要运行时能力时由 B 用 partial/闭包绑定 ctx。
"""

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

ToolEffect = Literal["read", "write", "dangerous"]
ToolRetry = Literal["safe", "never", "ledger"]


class ToolSpec(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    effect: ToolEffect
    allowed_nodes: set[str]
    timeout_s: float = 10.0
    retry: ToolRetry = "safe"
    handler: Callable[[BaseModel], Awaitable[BaseModel]]
