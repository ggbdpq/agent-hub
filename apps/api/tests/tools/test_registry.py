"""B1 ToolSpec 注册表与六工具：注册校验、未注册/越权 → tool.denied 事件而非异常崩溃。

计划 §7.2-B1 / ADR-0009：六工具 read×4 + write×1 + dangerous×1；
detect_anomaly 为确定性规则（数值判断交给代码，LLM 只做解释）。
"""

import pytest
from pydantic import BaseModel, ValidationError as PydanticValidationError

from app.contracts.tools import ToolSpec
from app.tools.inspection_tools import build_registry
from app.tools.registry import ToolRegistry, ToolRegistryError, check_call
from b_testkit import FIXTURES, make_ctx

EXPECTED = {
    "list_devices": "read",
    "get_telemetry": "read",
    "detect_anomaly": "read",
    "search_procedures": "read",
    "create_work_order": "write",
    "dispatch_drone": "dangerous",
}


def _make_registry(**kwargs):
    ctx, collector = make_ctx(**kwargs)
    registry = build_registry(ctx, fixture_root=FIXTURES)
    return registry, ctx, collector


def test_six_tools_registered_with_effects() -> None:
    registry, _, _ = _make_registry()
    assert set(registry.names()) == set(EXPECTED)
    for name, effect in EXPECTED.items():
        assert registry.get(name).effect == effect, name


def _spec(name: str, input_schema, output_schema, effect, nodes, handler) -> ToolSpec:
    return ToolSpec(
        name=name,
        description="",
        input_schema=input_schema,
        output_schema=output_schema,
        effect=effect,
        allowed_nodes=nodes,
        handler=handler,
    )


class _M(BaseModel):
    pass


async def _h(_: BaseModel) -> BaseModel:
    return _M()


def test_registration_rejects_invalid_spec() -> None:
    registry = ToolRegistry()

    # 非 BaseModel 入参/出参 Schema：ToolSpec 构造期（pydantic）与注册期双重拦截
    with pytest.raises((ToolRegistryError, PydanticValidationError)):
        registry.register(_spec("t1", dict, _M, "read", {"n"}, _h))  # type: ignore[arg-type]
    with pytest.raises((ToolRegistryError, PydanticValidationError)):
        registry.register(_spec("t1b", _M, dict, "read", {"n"}, _h))  # type: ignore[arg-type]
    # 空 allowed_nodes：权限没有边界 = 没有权限模型
    with pytest.raises(ToolRegistryError):
        registry.register(_spec("t2", _M, _M, "read", set(), _h))
    # 合法规格先注册成功（对照）
    registry.register(_spec("t2b", _M, _M, "read", {"n"}, _h))
    # timeout_s<=0 在注册期被拒
    with pytest.raises(ToolRegistryError):
        registry.register(
            ToolSpec(
                name="t2c",
                description="",
                input_schema=_M,
                output_schema=_M,
                effect="read",
                allowed_nodes={"n"},
                timeout_s=0,
                handler=_h,
            )
        )
    # 重复注册
    registry.register(_spec("t3", _M, _M, "read", {"n"}, _h))
    with pytest.raises(ToolRegistryError):
        registry.register(_spec("t3", _M, _M, "read", {"n"}, _h))


async def test_unregistered_tool_denied_with_event() -> None:
    """未注册工具：check_call 返回 denial 并发 tool.denied 事件，不抛异常。"""
    registry, ctx, collector = _make_registry()
    result = await check_call(registry, ctx, node="inspector", name="no_such_tool")
    assert result.denied is True
    assert result.reason == "not_registered"
    events = collector.of_type("tool.denied")
    assert len(events) == 1
    assert events[0].payload["tool"] == "no_such_tool"


async def test_node_not_allowed_denied_with_event() -> None:
    """越权：inspector 调 write 工具 → denial + tool.denied 事件（不是异常）。"""
    registry, ctx, collector = _make_registry()
    result = await check_call(registry, ctx, node="inspector", name="create_work_order")
    assert result.denied is True
    assert result.reason == "node_not_allowed"
    event = collector.of_type("tool.denied")[0]
    assert event.payload["tool"] == "create_work_order"
    assert event.node == "inspector"


def test_for_node_inspector_is_read_only_and_device_scoped() -> None:
    """R3：inspector 只读本设备数据——list_devices（站点级）不在其权限内。"""
    registry, _, _ = _make_registry()
    inspector_tools = registry.for_node("inspector")
    assert {s.name for s in inspector_tools} == {
        "get_telemetry",
        "detect_anomaly",
        "search_procedures",
    }
    assert all(s.effect == "read" for s in inspector_tools)


async def test_inspector_cannot_call_list_devices() -> None:
    """B6 硬规则的权限面：inspector 调 list_devices → denial + tool.denied 事件。"""
    registry, ctx, collector = _make_registry()
    result = await check_call(registry, ctx, node="inspector", name="list_devices")
    assert result.denied is True
    assert result.reason == "node_not_allowed"
    assert collector.of_type("tool.denied")[0].payload["tool"] == "list_devices"
    # 其余仍可用的节点不受影响
    ok = await check_call(registry, ctx, node="supervisor", name="list_devices")
    assert ok.denied is False


async def test_detect_anomaly_deterministic() -> None:
    """确定性规则：INV-005 柜温漂移超阈值 → critical；INV-001 → normal。"""
    registry, _, _ = _make_registry()
    spec = registry.get("detect_anomaly")

    bad = await spec.handler(spec.input_schema(device_id="INV-005"))
    assert bad.level == "critical"
    assert any(a.kind == "threshold" and a.metric == "cabinet_temp_c" for a in bad.anomalies)

    ok = await spec.handler(spec.input_schema(device_id="INV-001"))
    assert ok.level == "normal"
    assert ok.anomalies == []


async def test_detect_anomaly_covers_all_injected_patterns() -> None:
    """其余 4 台注入异常设备均被判 critical（期望从数据推导，非引用生成器注释）。"""
    registry, _, _ = _make_registry()
    spec = registry.get("detect_anomaly")
    levels = {}
    for device_id in ("WT-002", "PCS-003", "SW-002", "BS-001"):
        result = await spec.handler(spec.input_schema(device_id=device_id))
        levels[device_id] = result.level
    assert levels == {
        "WT-002": "critical",
        "PCS-003": "critical",
        "SW-002": "critical",
        "BS-001": "critical",
    }


async def test_search_procedures_uses_retriever() -> None:
    from app.contracts.fakes import StaticRetriever
    from app.contracts.knowledge import Chunk

    chunk = Chunk(
        chunk_id="doc1#异常判据#1",
        doc_id="doc1",
        title="异常判据",
        text="柜温超过 85 摄氏度立即上报",
        source_path="corpus/doc1.md",
    )
    registry, _, _ = _make_registry(retriever=StaticRetriever([chunk]))
    spec = registry.get("search_procedures")
    out = await spec.handler(spec.input_schema(query="异常判据", k=3))
    assert [c.chunk_id for c in out.chunks] == ["doc1#异常判据#1"]


async def test_create_work_order_returns_draft() -> None:
    registry, _, _ = _make_registry()
    spec = registry.get("create_work_order")
    out = await spec.handler(
        spec.input_schema(device_id="INV-005", title="柜温漂移", action="现场检查散热")
    )
    assert out.work_order.device_id == "INV-005"
    assert out.work_order.status == "draft"


async def test_dispatch_drone_simulated_only() -> None:
    registry, _, _ = _make_registry()
    spec = registry.get("dispatch_drone")
    out = await spec.handler(
        spec.input_schema(device_id="INV-005", reason="柜温超限", approval_id="AP-1")
    )
    assert out.status == "simulated"
    assert out.approval_id == "AP-1"


def test_spec_overrides_support_fault_injection() -> None:
    """C7 故障注入需要替换单个工具（慢/异常 handler），注册表构建须支持覆盖。"""

    async def slow_handler(_: BaseModel) -> BaseModel:
        import asyncio

        await asyncio.sleep(10)
        return _M()

    base_ctx, _ = make_ctx()
    base = build_registry(base_ctx, fixture_root=FIXTURES)
    slow_spec = base.get("get_telemetry").model_copy(update={"handler": slow_handler})
    ctx, _ = make_ctx()
    overridden = build_registry(ctx, fixture_root=FIXTURES, spec_overrides={"get_telemetry": slow_spec})
    assert overridden.get("get_telemetry").handler is slow_handler
    # 未覆盖的工具保持同名同权限（handler 是独立闭包实例，不比较相等）
    assert overridden.names() == base.names()
    assert overridden.get("list_devices").allowed_nodes == base.get("list_devices").allowed_nodes
