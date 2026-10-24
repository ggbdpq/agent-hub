"""巡检 MCP Server（B9）：从同一 ToolSpec 注册表生成 MCP 工具（ADR-0009 单一事实源）。

- 危险工具（effect=dangerous）默认不暴露——MCP 侧没有人工审批通道（ADR-0009）；
- 入参经 ToolSpec.input_schema 校验后再进 handler（与执行器同一套 Schema）；
- stdio / streamable HTTP 双传输：`--serve`（stdio）或嵌入 ASGI（streamable_http_app）。

运行：`python -m apps.mcp_inspection.server --list-tools` / `--serve`。
本模块自举 apps/api 到 sys.path（app 包位于 apps/api）。
"""

import argparse
import json
import sys
from inspect import Parameter, Signature
from pathlib import Path
from typing import Any

_API_ROOT = Path(__file__).resolve().parents[2] / "api"
if str(_API_ROOT) not in sys.path:
    sys.path.insert(0, str(_API_ROOT))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from app.contracts.agent import Budget, RunContext  # noqa: E402
from app.contracts.fakes import InMemoryLedger, StaticRetriever  # noqa: E402
from app.contracts.events import NewEvent  # noqa: E402
from app.contracts.tools import ToolSpec  # noqa: E402
from app.tools.inspection_tools import build_registry  # noqa: E402
from app.tools.registry import ToolRegistry  # noqa: E402

SERVER_NAME = "agent-hub-inspection"

_INSTRUCTIONS = """巡检工具面（与编排侧同一 ToolSpec 注册表生成）：
list_devices / get_telemetry / detect_anomaly / search_procedures / create_work_order。
危险工具（dispatch_drone）不在 MCP 侧暴露——它必须走编排内的人工审批。"""


async def _noop_emit(_event: NewEvent) -> None:
    """MCP 直连模式无 run 上下文：事件丢弃（handler 不依赖事件语义）。"""


def server_context() -> RunContext:
    """MCP 独立运行时的替身上下文：无 run/预算限制（MCP 调用方自担预算）。

    假设：MCP 暴露面只含 read + write（draft 工单），副作用幂等性由
    handler 层保证；带租约/fencing 的 RunStore 属编排内路径，MCP 不经过。
    """
    return RunContext(
        emit=_noop_emit,
        cancelled=lambda: False,
        budget=Budget(max_steps=10**9, max_tool_calls=10**9, max_tokens=10**9),
        ledger=InMemoryLedger(),
        retriever=StaticRetriever(),
        llm=lambda: (_ for _ in ()).throw(RuntimeError("MCP 工具面不消费 LLM")),
    )


def _wrap_spec(spec: ToolSpec) -> Any:
    """把 ToolSpec 包成带真实签名的异步函数（MCPServer 从签名推断入参 Schema）。

    数值边界（ge/le）不进 MCP Schema 广播，但 wrapper 内 model_validate 仍强制。
    """
    params = []
    for field_name, field in spec.input_schema.model_fields.items():
        if field.is_required():
            params.append(Parameter(field_name, Parameter.KEYWORD_ONLY, annotation=field.annotation))
        else:
            default = field.get_default(call_default_factory=True)
            params.append(
                Parameter(field_name, Parameter.KEYWORD_ONLY, annotation=field.annotation, default=default)
            )

    async def call(**kwargs: Any) -> dict:
        validated = spec.input_schema.model_validate(kwargs)
        out = await spec.handler(validated)
        return out.model_dump()

    call.__signature__ = Signature(params)
    call.__name__ = spec.name
    call.__doc__ = spec.description
    return call


def build_mcp_server(
    ctx: RunContext,
    *,
    fixture_root=None,
    registry: ToolRegistry | None = None,
    name: str = SERVER_NAME,
) -> MCPServer:
    """从注册表生成 MCPServer；dangerous 工具不暴露（ADR-0009 第 3 条）。"""
    reg = registry if registry is not None else build_registry(ctx, fixture_root=fixture_root)
    server = MCPServer(name, instructions=_INSTRUCTIONS)
    for tool_name in reg.names():
        spec = reg.require(tool_name)
        if spec.effect == "dangerous":
            continue
        server.add_tool(_wrap_spec(spec), name=spec.name, description=spec.description, structured_output=False)
    return server


def list_tools_output() -> str:
    """`--list-tools` 的文本输出（与实际暴露面一致，不含 dangerous）。"""
    ctx = server_context()
    reg = build_registry(ctx)
    lines = []
    for tool_name in reg.names():
        spec = reg.require(tool_name)
        if spec.effect == "dangerous":
            continue
        lines.append(f"{spec.name} [{spec.effect}] {spec.description}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="agent-hub 巡检 MCP Server")
    parser.add_argument("--list-tools", action="store_true", help="列出暴露的工具后退出")
    parser.add_argument("--serve", action="store_true", help="以 stdio 传输启动服务")
    args = parser.parse_args(argv)

    if args.list_tools:
        print(list_tools_output())
        return 0
    if args.serve:
        ctx = server_context()
        build_mcp_server(ctx).run(transport="stdio")
        return 0
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
