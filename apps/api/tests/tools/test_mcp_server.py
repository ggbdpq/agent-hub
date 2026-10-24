"""B9 MCP Server：从同一 ToolSpec 注册表生成 MCP 工具（ADR-0009 单一事实源）。

计划 §7.2-B9：MCP 客户端列出工具并调用 get_telemetry 成功；
dangerous 工具（dispatch_drone）在 MCP 侧默认不暴露。
"""

import json
import sys
from pathlib import Path

from mcp.client.client import Client

# apps.mcp_inspection 位于仓库根命名空间包；server.py 自举 apps/api 路径
REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from apps.mcp_inspection.server import build_mcp_server, list_tools_output  # noqa: E402
from b_testkit import FIXTURES, make_ctx  # noqa: E402

EXPECTED_TOOLS = {
    "list_devices",
    "get_telemetry",
    "detect_anomaly",
    "search_procedures",
    "create_work_order",
}


def _server():
    ctx, _ = make_ctx()
    return build_mcp_server(ctx, fixture_root=FIXTURES)


async def test_list_tools_exposes_five_non_dangerous_tools() -> None:
    server = _server()
    async with Client(server) as client:
        result = await client.list_tools()
        names = {t.name for t in result.tools}
    assert names == EXPECTED_TOOLS
    assert "dispatch_drone" not in names  # dangerous 默认不暴露


async def test_call_get_telemetry_via_mcp_client() -> None:
    server = _server()
    async with Client(server) as client:
        result = await client.call_tool("get_telemetry", {"device_id": "INV-001"})
    payload = json.loads(result.content[0].text)
    assert payload["device_id"] == "INV-001"
    assert "cabinet_temp_c" in payload["summary"]
    assert payload["points_count"] == 24


async def test_call_detect_anomaly_and_search() -> None:
    from app.contracts.fakes import StaticRetriever
    from app.contracts.knowledge import Chunk

    chunk = Chunk(
        chunk_id="doc1#异常判据#1",
        doc_id="doc1",
        title="异常判据",
        text="柜温超限上报",
        source_path="corpus/doc1.md",
    )
    ctx, _ = make_ctx(retriever=StaticRetriever([chunk]))
    server = build_mcp_server(ctx, fixture_root=FIXTURES)
    async with Client(server) as client:
        anomaly = json.loads((await client.call_tool("detect_anomaly", {"device_id": "INV-005"})).content[0].text)
        search = json.loads(
            (await client.call_tool("search_procedures", {"query": "异常判据", "k": 3})).content[0].text
        )
    assert anomaly["level"] == "critical"
    assert [c["chunk_id"] for c in search["chunks"]] == ["doc1#异常判据#1"]


async def test_call_create_work_order_returns_draft() -> None:
    server = _server()
    async with Client(server) as client:
        result = await client.call_tool(
            "create_work_order",
            {"device_id": "INV-005", "title": "柜温超限", "action": "现场检查散热"},
        )
    payload = json.loads(result.content[0].text)
    assert payload["work_order"]["status"] == "draft"


async def test_unknown_and_dangerous_tools_rejected() -> None:
    server = _server()
    async with Client(server) as client:
        missing = await client.call_tool("dispatch_drone", {"device_id": "X"})
        missing2 = await client.call_tool("no_such_tool", {})
    assert "Unknown tool" in missing.content[0].text
    assert "Unknown tool" in missing2.content[0].text


def test_list_tools_output_contains_all_tools() -> None:
    """`python -m apps.mcp_inspection.server --list-tools` 的函数级验证。"""
    text = list_tools_output()
    for name in EXPECTED_TOOLS:
        assert name in text
    assert "dispatch_drone" not in text
