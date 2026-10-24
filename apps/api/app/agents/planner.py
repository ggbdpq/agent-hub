"""B3：规划 Worker（Plan-and-Execute）。

输出结构化 ``Plan(steps[{device_id, checks[]}])``；LLM 输出经 pydantic 校验，
失败最多重试 2 次，仍失败退回"全量默认检查计划"（确定性兜底，可解释）。
计划只决定"查什么"，不决定"怎么判"——数值判断归 detect_anomaly（ADR-0009）。
"""

import json
import re
from dataclasses import dataclass, field

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field, ValidationError

from app.contracts.agent import InspectionRequest, RunContext
from app.tools.budget import BudgetTracker, usage_from_message
from app.tools.devices import load_devices

MAX_RETRIES = 2

_SYSTEM_PROMPT = """你是巡检规划器。为给定设备制定巡检计划：每台设备给出 1-4 个检查项。
只输出严格 JSON（不要多余文字、不要 markdown 代码块）：
{"steps": [{"device_id": "设备编号", "checks": ["检查项", ...]}]}
device_id 必须从给定清单中选择；检查项从设备类型出发（遥测比对/异常判据/规程检索等）。"""

_JSON_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class PlanStep(BaseModel):
    device_id: str
    checks: list[str] = Field(min_length=1)


class Plan(BaseModel):
    steps: list[PlanStep]
    note: str | None = None


@dataclass
class PlanResult:
    plan: Plan
    fallback: bool = False
    attempts: int = 0
    errors: list[str] = field(default_factory=list)


# 设备类型 → 默认检查项（确定性兜底用；与语料《日常巡检规程》一致）
DEFAULT_CHECKS: dict[str, list[str]] = {
    "pv_inverter": ["遥测比对", "柜温趋势", "散热检查"],
    "wind_turbine": ["遥测比对", "振动趋势", "机舱温度"],
    "storage_pcs": ["遥测比对", "输出功率趋势", "电芯温度"],
    "substation_bay": ["遥测比对", "油温趋势", "负载率"],
    "bridge_sensor": ["遥测比对", "位移趋势", "温度"],
}
_FALLBACK_CHECKS = ["遥测比对", "异常判据核查"]


def default_plan(request: InspectionRequest) -> Plan:
    """确定性兜底计划：每台设备按类型给默认检查项（类型未知给通用检查）。"""
    types = {d.device_id: d.type for d in load_devices()}
    steps = [
        PlanStep(device_id=device_id, checks=DEFAULT_CHECKS.get(types.get(device_id, ""), _FALLBACK_CHECKS))
        for device_id in request.device_ids
    ]
    return Plan(steps=steps, note="默认全量检查计划（规划失败兜底）")


async def make_plan(
    request: InspectionRequest,
    ctx: RunContext,
    tracker: BudgetTracker,
    *,
    max_retries: int = MAX_RETRIES,
) -> PlanResult:
    """LLM 规划 → 校验 → 重试 → 兜底。全程经 ctx.llm 获取模型（不直接建模型）。"""
    errors: list[str] = []
    attempts = 0
    user_payload = json.dumps(
        {"site": request.site, "devices": request.device_ids, "goal": request.goal or "日常巡检"},
        ensure_ascii=False,
    )
    for attempt in range(max_retries + 1):
        if ctx.cancelled():
            errors.append("取消请求：退回默认计划")
            break
        attempts += 1
        model = ctx.llm()
        message = await model.ainvoke([SystemMessage(_SYSTEM_PROMPT), HumanMessage(user_payload)])
        if not tracker.consume_tokens(usage_from_message(message)):
            errors.append(f"第 {attempt + 1} 次尝试后 token 预算耗尽，退回默认计划")
            break
        try:
            plan = _parse_plan(message.content, request)
            return PlanResult(plan=plan, fallback=False, attempts=attempts, errors=errors)
        except (ValueError, ValidationError) as exc:
            errors.append(f"第 {attempt + 1} 次输出不可用: {exc}")
    # 兜底计划自身带 note 说明（默认全量检查计划），errors 只记失败原因
    return PlanResult(plan=default_plan(request), fallback=True, attempts=attempts, errors=errors)


def _parse_plan(content: str, request: InspectionRequest) -> Plan:
    """剥离代码围栏 → json.loads → pydantic 校验 → 剔除未知设备/去重；空计划抛 ValueError。"""
    text = content.strip()
    fenced = _JSON_FENCE.search(text)
    if fenced:
        text = fenced.group(1)
    data = json.loads(text)
    plan = Plan.model_validate(data)
    allowed = set(request.device_ids)
    seen: set[str] = set()
    steps = []
    for step in plan.steps:
        if step.device_id in allowed and step.device_id not in seen:
            steps.append(step)
            seen.add(step.device_id)
    if not steps:
        raise ValueError("计划无有效步骤（空或全部 device_id 未知）")
    return Plan(steps=steps, note=plan.note)
