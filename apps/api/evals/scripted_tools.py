"""确定性工具型脚本 runner（C8 开发期参考实现）：实现 ``AgentRunner`` 契约。

按 ADR-0009 初始工具面发完整事件流（tool.called/result/denied、evidence.attached、
metric.usage），异常判定复用评测判据（evals.derive_expectations，确定性规则），
规程证据来自 **ctx 注入的真实 Retriever**。用途：离线打通评测管道与指标口径——
**不代表模型效果**。

故障注入（``set_fault``，评测框架 duck-typing 调用）：
- ``tool_timeout(tool[, device_id])``：该工具单次调用超时（tool.result ok=False）；
  get_telemetry 超时 → 对应设备子任务终止（coverage 下降 → partial）；
- ``llm_invalid_output``：首轮产生一次非法工具名，executor 记 tool.denied 后恢复，
  不影响最终 status（容错路径）；
- ``subtask_failure(device_id)``：该设备子任务失败（node.failed），其余照常 → partial。
"""

import json
from typing import Any

from app.contracts.agent import Finding, InspectionReport, InspectionRequest, RunContext, WorkOrder
from app.contracts.events import NewEvent
from evals.derive_expectations import detect_device, device_type_of, load_telemetry

_TYPE_LABEL = {
    "pv_inverter": "光伏逆变器",
    "wind_turbine": "风力发电机组",
    "storage_pcs": "储能变流器",
    "substation_bay": "变电站间隔",
    "bridge_sensor": "桥梁监测传感",
}


class _SubtaskFailure(Exception):
    """设备子任务终止信号（遥测超时降级 / 注入的子任务失败）。"""


class ScriptedToolAgentRunner:
    """鸭子类型实现 AgentRunner 契约（与 fakes.ScriptedAgentRunner 同风格）。"""
    def __init__(self) -> None:
        self._fault: dict | None = None
        self._timeout_fired = False
        self._invalid_fired = False
        self._degraded = False

    def set_fault(self, fault: dict | None) -> None:
        self._fault = fault
        self._timeout_fired = False
        self._invalid_fired = False
        self._degraded = False

    async def run(self, run_id: str, input: InspectionRequest, ctx: RunContext) -> InspectionReport:
        readonly = "不生成工单" in (input.goal or "")  # goal 契约：只诊断，禁写
        drone = "无人机" in (input.goal or "")  # goal 契约：异常设备需无人机复核（dangerous，需审批）
        await ctx.emit(NewEvent(type="node.started", node="planner", payload={"devices": len(input.device_ids)}))
        if len(input.device_ids) > 1:
            await self._call_tool(ctx, "list_devices", {"count": len(input.device_ids)})
        findings: list[Finding] = []
        work_orders: list[WorkOrder] = []
        failed_subtasks = 0
        for device_id in input.device_ids:
            try:
                finding, order = await self._inspect_device(ctx, device_id, readonly=readonly, drone=drone)
            except _SubtaskFailure:
                failed_subtasks += 1
                await ctx.emit(
                    NewEvent(type="node.failed", node="inspector", payload={"device_id": device_id, "error": "subtask_failure"})
                )
                continue
            if finding is not None:
                findings.append(finding)
            if order is not None:
                work_orders.append(order)
        coverage = (len(input.device_ids) - failed_subtasks) / len(input.device_ids)
        status = "completed" if failed_subtasks == 0 and not self._degraded else "partial"
        await ctx.emit(
            NewEvent(
                type="metric.usage",
                payload={"input_tokens": 120 * len(input.device_ids), "output_tokens": 60 * len(input.device_ids)},
            )
        )
        await ctx.emit(NewEvent(type="node.completed", node="reporter", payload={"status": status}))
        return InspectionReport(
            findings=findings,
            work_orders=work_orders,
            status=status,
            coverage=round(coverage, 3),
            notes="scripted-tools 离线参考输出：仅验证评测流程，不代表模型效果",
        )

    # ---- 单设备子任务 ----

    async def _inspect_device(self, ctx: RunContext, device_id: str, *, readonly: bool = False, drone: bool = False):
        fault = self._fault or {}
        if fault.get("type") == "subtask_failure" and fault.get("device_id") == device_id:
            self._degraded = True
            raise _SubtaskFailure()
        telemetry = await self._call_tool(ctx, "get_telemetry", {"device_id": device_id})
        if telemetry is None:  # 遥测超时：设备无法判定，子任务终止
            self._degraded = True
            raise _SubtaskFailure()
        level_info = await self._call_tool(ctx, "detect_anomaly", {"device_id": device_id})
        if level_info is None:  # 判据不可用：不能假装"正常"，降级并跳过该设备的结论
            self._degraded = True
            return None, None
        if not level_info["anomalous"]:
            return Finding(device_id=device_id, level="normal", summary="遥测平稳，未见异常"), None
        # 异常设备：检索规程依据（真实 Retriever）→ 工单
        query = f"{_TYPE_LABEL[device_type_of(device_id)]} 异常判据与告警阈值"
        chunks = await self._call_tool(ctx, "search_procedures", {"query": query, "k": 3})
        if chunks is None:  # 规程依据缺失：工单缺判定依据（上报要求），整体降级
            self._degraded = True
            chunks = []
        evidence_ids = [c["chunk_id"] for c in chunks]
        await ctx.emit(NewEvent(type="evidence.attached", node="inspector", payload={"device_id": device_id, "chunk_ids": evidence_ids}))
        metrics = sorted({e["metric"] for e in level_info["evidence"] if not e["metric"].endswith("_pct")}) or ["output_kw"]
        title = f"{device_id} {'/'.join(metrics)} 异常工单"
        action = (
            f"按规程处置：{device_id} 触发 {level_info['level']} 级异常（{', '.join(metrics)}），"
            f"判定依据证据 {', '.join(evidence_ids) or '无'}"
        )
        if readonly:
            # goal 契约：只诊断。给出结论与依据，不开工单、不派无人机。
            return (
                Finding(
                    device_id=device_id,
                    level="critical" if level_info["level"] == "critical" else "warning",
                    summary=f"{device_id} 异常：{', '.join(metrics)}",
                    evidence_chunk_ids=evidence_ids,
                    metrics={m: level_info["level"] for m in metrics},
                ),
                None,
            )
        await self._call_tool(ctx, "create_work_order", {"device_id": device_id, "title": title, "action": action})
        if drone:
            # dangerous 工具：审批路径（offline 脚本自审自批，事件流完整可复盘）
            await ctx.emit(NewEvent(type="approval.required", node="inspector", payload={"tool": "dispatch_drone", "device_id": device_id}))
            await ctx.emit(NewEvent(type="approval.resolved", node="inspector", payload={"tool": "dispatch_drone", "device_id": device_id, "approved": True}))
            await self._call_tool(ctx, "dispatch_drone", {"device_id": device_id, "purpose": "现场复核"})
        finding = Finding(
            device_id=device_id,
            level="critical" if level_info["level"] == "critical" else "warning",
            summary=f"{device_id} 异常：{', '.join(metrics)}",
            evidence_chunk_ids=evidence_ids,
            metrics={m: level_info["level"] for m in metrics},
        )
        return finding, WorkOrder(device_id=device_id, title=title, action=action, status="draft")

    # ---- 工具调用与故障注入点 ----

    async def _call_tool(self, ctx: RunContext, tool: str, args: dict) -> Any:
        fault = self._fault or {}
        if fault.get("type") == "llm_invalid_output" and not self._invalid_fired:
            self._invalid_fired = True  # 一次非法工具名：executor 拒绝后流程恢复
            await ctx.emit(NewEvent(type="tool.called", node="inspector", payload={"tool": "restart_reactor", "args": {}}))
            await ctx.emit(NewEvent(type="tool.denied", node="inspector", payload={"tool": "restart_reactor", "reason": "unknown_tool"}))
        if (
            fault.get("type") == "tool_timeout"
            and fault.get("tool") == tool
            and not self._timeout_fired
            and (fault.get("device_id") is None or fault.get("device_id") == args.get("device_id"))
        ):
            self._timeout_fired = True
            await ctx.emit(NewEvent(type="tool.called", node="inspector", payload={"tool": tool, "args": args}))
            await ctx.emit(NewEvent(type="tool.result", node="inspector", payload={"tool": tool, "ok": False, "error": "timeout"}))
            return None
        await ctx.emit(NewEvent(type="tool.called", node="inspector", payload={"tool": tool, "args": args}))
        result = await self._execute(ctx, tool, args)
        await ctx.emit(NewEvent(type="tool.result", node="inspector", payload={"tool": tool, "ok": True, "result": _jsonable(result)}))
        return result

    async def _execute(self, ctx: RunContext, tool: str, args: dict) -> Any:
        if tool == "get_telemetry":
            return load_telemetry(args["device_id"])
        if tool == "detect_anomaly":
            return detect_device(args["device_id"])
        if tool == "list_devices":
            return {"note": "名单由巡检输入给定"}
        if tool == "search_procedures":
            return [c.model_dump(mode="json") for c in await ctx.retriever.search(args["query"], args.get("k", 3))]
        if tool == "create_work_order":
            return {"work_order_id": f"WO-{args.get('device_id', 'X')}"}
        if tool == "dispatch_drone":
            return {"drone_id": "UAV-01", "status": "dispatched(simulated)"}
        raise ValueError(f"未知工具：{tool}")


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)
