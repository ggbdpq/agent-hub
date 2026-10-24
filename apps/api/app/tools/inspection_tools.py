"""B1：巡检六工具（ADR-0009 初始工具面），从 ToolSpec 注册表构建。

handler 只收已校验入参（契约签名）；需要运行时能力（Retriever）的由本模块
用闭包绑定 ctx。权限（allowed_nodes）在此声明，执行器（B2）强制。
"""

import uuid

from app.contracts.agent import RunContext, WorkOrder
from app.contracts.knowledge import Chunk
from app.tools.anomaly import AnomalyResult, detect
from app.tools.devices import DEFAULT_FIXTURE_ROOT, DeviceRecord, load_devices, load_telemetry, telemetry_summary
from app.tools.registry import ToolRegistry
from pydantic import BaseModel, Field


# -- 入参 / 出参 Schema --

class ListDevicesArgs(BaseModel):
    site: str | None = None


class ListDevicesOut(BaseModel):
    devices: list[DeviceRecord]
    total: int


class DeviceIdArgs(BaseModel):
    device_id: str


class GetTelemetryOut(BaseModel):
    device_id: str
    window: str
    points_count: int
    summary: dict[str, dict[str, float]]


class SearchProceduresArgs(BaseModel):
    query: str
    k: int = Field(default=3, ge=1, le=10)


class SearchProceduresOut(BaseModel):
    chunks: list[Chunk]


class CreateWorkOrderArgs(BaseModel):
    device_id: str
    title: str
    action: str


class CreateWorkOrderOut(BaseModel):
    work_order: WorkOrder


class DispatchDroneArgs(BaseModel):
    device_id: str
    reason: str
    approval_id: str  # dangerous 工具必须带审批凭证；缺省由执行器拒绝


class DispatchDroneOut(BaseModel):
    dispatch_id: str
    status: str  # 固定 "simulated"
    approval_id: str


# -- 节点权限表（ADR-0009：越权由执行器拒绝，不靠提示词） --

_READ_NODES = {"supervisor", "planner", "inspector", "reporter", "single"}
_ANALYSIS_NODES = {"supervisor", "inspector", "single"}
_WRITE_NODES = {"supervisor", "reporter", "single"}


def build_registry(
    ctx: RunContext,
    *,
    fixture_root=None,
    spec_overrides: dict[str, object] | None = None,
) -> ToolRegistry:
    """构建六工具注册表；spec_overrides 支持故障注入与测试替身（C7 消费用）。"""
    root = fixture_root if fixture_root is not None else DEFAULT_FIXTURE_ROOT
    devices_cache: list[DeviceRecord] | None = None

    def _devices() -> list[DeviceRecord]:
        nonlocal devices_cache
        if devices_cache is None:
            devices_cache = load_devices(root)
        return devices_cache

    async def list_devices(args: ListDevicesArgs) -> ListDevicesOut:
        devices = [d for d in _devices() if args.site is None or d.site == args.site]
        return ListDevicesOut(devices=devices, total=len(devices))

    async def get_telemetry(args: DeviceIdArgs) -> GetTelemetryOut:
        series = load_telemetry(args.device_id, root)
        return GetTelemetryOut(
            device_id=series.device_id,
            window=series.window,
            points_count=len(series.points),
            summary=telemetry_summary(series),
        )

    async def detect_anomaly(args: DeviceIdArgs) -> AnomalyResult:
        return detect(args.device_id, root)

    async def search_procedures(args: SearchProceduresArgs) -> SearchProceduresOut:
        chunks = await ctx.retriever.search(args.query, k=args.k)
        return SearchProceduresOut(chunks=chunks)

    async def create_work_order(args: CreateWorkOrderArgs) -> CreateWorkOrderOut:
        work_order = WorkOrder(device_id=args.device_id, title=args.title, action=args.action)
        return CreateWorkOrderOut(work_order=work_order)

    async def dispatch_drone(args: DispatchDroneArgs) -> DispatchDroneOut:
        # 仅模拟：不接真实设备（ADR-0009）；真实派工需审批凭证
        return DispatchDroneOut(
            dispatch_id=f"DR-{uuid.uuid4().hex[:8]}",
            status="simulated",
            approval_id=args.approval_id,
        )

    specs = [
        _spec(
            name="list_devices",
            description="列出站点设备清单，可按站点过滤",
            input_schema=ListDevicesArgs,
            output_schema=ListDevicesOut,
            effect="read",
            allowed_nodes=_READ_NODES,
            handler=list_devices,
        ),
        _spec(
            name="get_telemetry",
            description="读取设备 24h 遥测的紧凑统计摘要（每指标 min/max/mean/last）",
            input_schema=DeviceIdArgs,
            output_schema=GetTelemetryOut,
            effect="read",
            allowed_nodes=_ANALYSIS_NODES,
            handler=get_telemetry,
        ),
        _spec(
            name="detect_anomaly",
            description="确定性异常判读：阈值越限/趋势漂移/阶跃突变/功率骤降，返回 level 与规则明细",
            input_schema=DeviceIdArgs,
            output_schema=AnomalyResult,
            effect="read",
            allowed_nodes=_ANALYSIS_NODES,
            handler=detect_anomaly,
        ),
        _spec(
            name="search_procedures",
            description="在巡检规程知识库中检索相关条款，返回 chunk_id 供结论引用",
            input_schema=SearchProceduresArgs,
            output_schema=SearchProceduresOut,
            effect="read",
            allowed_nodes=_ANALYSIS_NODES,
            handler=search_procedures,
        ),
        _spec(
            name="create_work_order",
            description="创建检修工单（write，幂等账本去重）",
            input_schema=CreateWorkOrderArgs,
            output_schema=CreateWorkOrderOut,
            effect="write",
            allowed_nodes=_WRITE_NODES,
            handler=create_work_order,
            retry="ledger",
            timeout_s=15.0,
        ),
        _spec(
            name="dispatch_drone",
            description="派无人机现场复核（dangerous，必须携带人工审批 approval_id；仅模拟）",
            input_schema=DispatchDroneArgs,
            output_schema=DispatchDroneOut,
            effect="dangerous",
            allowed_nodes=_WRITE_NODES,
            handler=dispatch_drone,
            retry="never",
            timeout_s=15.0,
        ),
    ]

    by_name = {spec.name: spec for spec in specs}
    by_name.update(spec_overrides or {})  # type: ignore[assignment]
    registry = ToolRegistry()
    for spec in by_name.values():
        registry.register(spec)
    return registry


def _spec(**kwargs):
    from app.contracts.tools import ToolSpec

    return ToolSpec(**kwargs)
