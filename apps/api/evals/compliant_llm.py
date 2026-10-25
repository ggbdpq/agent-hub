"""确定性合规智能体替身（X2/X3/X4 消融共用）：按系统提示区分角色，读对话内
工具结果决策——**无评测答案泄露**（level 只取 detect_anomaly 观测结果，证据只引用
search_procedures 返回过的 chunk_id，与真实智能体的信息面一致）。

角色路由（系统提示关键词）：
- planner（"巡检规划器"）：为请求清单逐台出计划（1-4 检查项）；
- inspector（"设备巡检分析员"）：简报驱动（B6 设计：遥测已在简报内，**不再调
  get_telemetry**——这正是 E1 真实模型 run 漏调 get_telemetry 的口径来源），
  每台：detect_anomaly →（异常）search_procedures → final；
- reporter（"工单起草员"）：为收到的 warning/critical 目标逐一起草工单；
- single（"巡检智能体"）：单 ReAct 循环覆盖全部设备：逐台 get_telemetry +
  detect_anomaly（单智能体无简报，遥测必须自取）→ 异常设备检索规程 → final
  （工单按 goal：含"生成工单"才创建）。

替身计量：factory 汇总各角色调用次数与输入/输出 token 估算（estimate_tokens
口径，脚本估算值，非计费口径）——供真实模型费用估算的调用数基线。
时延：``delay_s`` 模拟单次 LLM 时延（默认 0；E3 扇出消融用）。

口径：离线替身**仅验证评测流程，不代表模型效果**。
"""

import asyncio
import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel, ChatResult
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import ChatGeneration

from app.tools.budget import estimate_tokens

_ROLE_MARKERS = (
    ("planner", "巡检规划器"),
    ("inspector", "设备巡检分析员"),
    ("reporter", "工单起草员"),
    ("single", "巡检智能体"),
)


def _text(message: BaseMessage) -> str:
    content = message.content
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)


def _parse_json(content: str) -> Any:
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def _human_payloads(messages: list[BaseMessage]) -> list[Any]:
    """对话内全部 Human 消息的 JSON 解析结果（工具回执 / 简报 / 请求载荷）。"""
    return [
        parsed
        for parsed in (_parse_json(_text(m)) for m in messages if isinstance(m, HumanMessage))
        if parsed is not None
    ]


def _tool_receipts(messages: list[BaseMessage]) -> list[dict]:
    """执行器回执：{"tool","status","result"} 形态的 Human 消息。"""
    return [
        p
        for p in _human_payloads(messages)
        if isinstance(p, dict) and "tool" in p and "status" in p
    ]


def _initial_request(messages: list[BaseMessage]) -> dict:
    """首条可解析为 dict 的 Human 载荷（single 的请求 / planner 的请求）。"""
    for p in _human_payloads(messages):
        if isinstance(p, dict) and ("devices" in p or "device_ids" in p):
            return p
    return {}


def _observed_chunks(receipts: list[dict]) -> list[str]:
    chunks: list[str] = []
    for r in receipts:
        if r.get("tool") == "search_procedures" and r.get("status") == "ok":
            for c in (r.get("result") or {}).get("chunks", []):
                cid = c.get("chunk_id")
                if cid and cid not in chunks:
                    chunks.append(cid)
    return chunks


def _anomaly_summary(level: str, result: dict | None) -> str:
    if not result or not result.get("anomalies"):
        return "遥测平稳，判读无异常" if level == "normal" else f"判读 {level} 级异常"
    parts = [f"{a.get('metric', '?')}{'超阈值' if a.get('kind') == 'threshold' else '异常'}" for a in result["anomalies"]]
    return f"判读 {level} 级异常：{'；'.join(parts)}"


def _act(tool: str, **args) -> str:
    return json.dumps({"tool": tool, "args": args}, ensure_ascii=False)


# ---- 各角色策略（纯函数：messages → 回复 JSON 文本） ----


def _planner_reply(messages: list[BaseMessage]) -> str:
    request = _initial_request(messages)
    devices = request.get("devices") or request.get("device_ids") or []
    steps = [{"device_id": d, "checks": ["遥测比对", "异常判据核查"]} for d in devices]
    return json.dumps({"steps": steps}, ensure_ascii=False)


def _inspector_reply(messages: list[BaseMessage]) -> str:
    """简报驱动子任务：遥测已在简报（B6），判读必调、异常检索规程、final。"""
    receipts = _tool_receipts(messages)
    called = {r.get("tool") for r in receipts}
    detect = next(
        (r.get("result") for r in reversed(receipts)
         if r.get("tool") == "detect_anomaly" and r.get("status") == "ok"),
        None,
    )
    if detect is None and "detect_anomaly" not in called:
        return _act("detect_anomaly")
    if detect is None:  # 判读被拒/失败：给 final 让代码走确定性兜底判定
        return json.dumps({"final": {"summary": "判读工具不可用", "metrics": {}}}, ensure_ascii=False)
    level = detect.get("level", "normal")
    if level != "normal" and "search_procedures" not in called:
        return _act("search_procedures", query="异常判据与处置", k=3)
    evidence = _observed_chunks(receipts) if level != "normal" else []
    return json.dumps(
        {
            "final": {
                "summary": _anomaly_summary(level, detect),
                "level": level,
                "evidence_chunk_ids": evidence,
                "metrics": {},
            }
        },
        ensure_ascii=False,
    )


def _reporter_reply(messages: list[BaseMessage]) -> str:
    payloads = _human_payloads(messages)
    targets = next((p for p in payloads if isinstance(p, list)), [])
    orders = [
        {
            "device_id": t.get("device_id", ""),
            # 设备编号 + 级别 + 判读摘要（摘要含异常指标名）——goal 要求工单注明
            # 设备编号、异常参数与判定依据，信息均来自上游 finding（无答案泄露）
            "title": f"{t.get('device_id', '')} {t.get('level', 'warning')}：{(t.get('summary') or '')[:40]}",
            "action": "按异常判据规程现场检查处置并复核（异常指标见标题）",
        }
        for t in targets
        if isinstance(t, dict) and t.get("device_id")
    ]
    return json.dumps({"orders": orders}, ensure_ascii=False)


def _single_reply(messages: list[BaseMessage]) -> str:
    """单 ReAct 合规策略：无简报 → 逐台自取遥测+判读 → 异常检索 → final。"""
    request = _initial_request(messages)
    devices: list[str] = request.get("devices") or request.get("device_ids") or []
    goal = str(request.get("goal") or "")
    receipts = _tool_receipts(messages)
    telemetry = {
        (r.get("result") or {}).get("device_id")
        for r in receipts
        if r.get("tool") == "get_telemetry" and r.get("status") == "ok"
    }
    detects = {
        (r.get("result") or {}).get("device_id"): r.get("result")
        for r in receipts
        if r.get("tool") == "detect_anomaly" and r.get("status") == "ok"
    }
    searches = sum(1 for r in receipts if r.get("tool") == "search_procedures")
    for device in devices:
        if device not in telemetry:
            return _act("get_telemetry", device_id=device)
    for device in devices:
        if device not in detects:
            return _act("detect_anomaly", device_id=device)
    anomalies = [d for d in devices if (detects.get(d) or {}).get("level", "normal") != "normal"]
    if anomalies and searches < len(anomalies):
        return _act("search_procedures", query="异常判据与处置", k=3)
    chunks = _observed_chunks(receipts)
    findings = [
        {
            "device_id": d,
            "level": (detects.get(d) or {}).get("level", "normal"),
            "summary": _anomaly_summary((detects.get(d) or {}).get("level", "normal"), detects.get(d)),
            "evidence_chunk_ids": chunks if d in anomalies else [],
            "metrics": {},
        }
        for d in devices
    ]
    readonly = "不开工单" in goal or "禁止创建工单" in goal
    orders = (
        []
        if readonly
        else [
            {
                "device_id": d,
                "title": f"{d} {(detects.get(d) or {}).get('level', 'warning')}：{_anomaly_summary((detects.get(d) or {}).get('level', 'normal'), detects.get(d))[:60]}",
                "action": "按异常判据规程现场检查处置并复核（异常指标见标题）",
            }
            for d in anomalies
        ]
    )
    return json.dumps(
        {"final": {"findings": findings, "orders": orders, "notes": "合规替身：单循环完成全部设备"}},
        ensure_ascii=False,
    )


_ROLES = {
    "planner": _planner_reply,
    "inspector": _inspector_reply,
    "reporter": _reporter_reply,
    "single": _single_reply,
}


class CompliantScriptedLLM(BaseChatModel):
    """按角色路由的确定性合规替身模型；``delay_s`` 模拟单次 LLM 时延。"""

    delay_s: float = 0.0

    @property
    def _llm_type(self) -> str:
        return "compliant-scripted"

    def _reply(self, messages: list[BaseMessage]) -> str:
        system = _text(messages[0]) if messages else ""
        for role, marker in _ROLE_MARKERS:
            if marker in system:
                return _ROLES[role](messages)
        raise RuntimeError(f"CompliantScriptedLLM：未知角色（系统提示片段：{system[:40]}…）")

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # pragma: no cover - 仅异步路径
        raise NotImplementedError("CompliantScriptedLLM 仅支持 ainvoke（评测全异步）")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        reply = self._reply(list(messages))
        self._factory.record(messages, reply)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=reply))])

    def bind_factory(self, factory: "CompliantLLMFactory") -> "CompliantScriptedLLM":
        object.__setattr__(self, "_factory", factory)
        return self


class CompliantLLMFactory:
    """ChatModelFactory 替身：每次 build() 返回新模型实例；汇总调用与 token 估算。"""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self.delay_s = delay_s
        self.calls_by_role: dict[str, int] = {}
        self.input_tokens_est = 0
        self.output_tokens_est = 0

    @property
    def calls(self) -> int:
        return sum(self.calls_by_role.values())

    def build(self) -> CompliantScriptedLLM:
        return CompliantScriptedLLM(delay_s=self.delay_s).bind_factory(self)

    __call__ = build

    def record(self, messages: list[BaseMessage], reply: str) -> None:
        system = _text(messages[0]) if messages else ""
        role = next((r for r, marker in _ROLE_MARKERS if marker in system), "unknown")
        self.calls_by_role[role] = self.calls_by_role.get(role, 0) + 1
        self.input_tokens_est += sum(estimate_tokens(_text(m)) for m in messages)
        self.output_tokens_est += estimate_tokens(reply)

    def snapshot(self) -> dict:
        return {
            "calls": self.calls,
            "calls_by_role": dict(self.calls_by_role),
            "input_tokens_est": self.input_tokens_est,
            "output_tokens_est": self.output_tokens_est,
        }
