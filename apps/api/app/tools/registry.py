"""B1：ToolSpec 注册表（ADR-0009 单一事实源）。

注册期校验（Schema/权限/超时）；调用期检查走 ``check_call``——未注册或越权
返回 denial 并发 ``tool.denied`` 事件，绝不以异常崩溃（可解释性要求）。
"""

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from app.contracts.agent import RunContext
from app.contracts.events import NewEvent
from app.contracts.tools import ToolSpec


class ToolRegistryError(ValueError):
    """注册期校验失败（非法 Schema / 空权限 / 重复名 / 非正超时）。"""


@dataclass(frozen=True)
class CallCheck:
    """调用前检查结果：denied=False 时 spec 可用。"""

    spec: ToolSpec | None
    denied: bool
    reason: str | None = None  # not_registered / node_not_allowed
    detail: str | None = None


class ToolRegistry:
    """按名索引的 ToolSpec 集合；MCP Server 与执行器共用同一份（单一事实源）。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if not (isinstance(spec.input_schema, type) and issubclass(spec.input_schema, BaseModel)):
            raise ToolRegistryError(f"{spec.name}: input_schema 必须是 BaseModel 子类")
        if not (isinstance(spec.output_schema, type) and issubclass(spec.output_schema, BaseModel)):
            raise ToolRegistryError(f"{spec.name}: output_schema 必须是 BaseModel 子类")
        if not spec.allowed_nodes:
            raise ToolRegistryError(f"{spec.name}: allowed_nodes 为空——权限没有边界等于没有权限模型")
        if spec.timeout_s <= 0:
            raise ToolRegistryError(f"{spec.name}: timeout_s 必须为正")
        if spec.name in self._specs:
            raise ToolRegistryError(f"{spec.name}: 重复注册")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def require(self, name: str) -> ToolSpec:
        spec = self._specs.get(name)
        if spec is None:
            raise ToolRegistryError(f"工具未注册: {name}")
        return spec

    def names(self) -> list[str]:
        return sorted(self._specs)

    def for_node(self, node: str) -> list[ToolSpec]:
        return [s for s in self._specs.values() if node in s.allowed_nodes]


async def check_call(registry: ToolRegistry, ctx: RunContext, *, node: str, name: str) -> CallCheck:
    """调用前权限检查；denial 发 ``tool.denied`` 事件（payload 记录原因，可解释）。"""
    spec = registry.get(name)
    if spec is None:
        await ctx.emit(
            NewEvent(
                type="tool.denied",
                node=node,
                payload={"tool": name, "reason": "not_registered"},
            )
        )
        return CallCheck(None, True, "not_registered", f"工具未注册: {name}")
    if node not in spec.allowed_nodes:
        await ctx.emit(
            NewEvent(
                type="tool.denied",
                node=node,
                payload={
                    "tool": name,
                    "reason": "node_not_allowed",
                    "allowed_nodes": sorted(spec.allowed_nodes),
                },
            )
        )
        return CallCheck(
            spec,
            True,
            "node_not_allowed",
            f"节点 {node} 无权调用 {name}（允许: {sorted(spec.allowed_nodes)}）",
        )
    return CallCheck(spec, False)
