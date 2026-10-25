"""B2：预算追踪器与 Token 估算——预算由代码强制，不靠提示词自觉（ADR-0009）。

Budget（契约）只有上限字段；运行期计数在 BudgetTracker 实例上，
由 runner/循环的持有者创建并贯穿整个 run（跨节点共享同一份计数）。
allow_* 返回布尔而不抛异常：调用方（执行器/ReAct 循环）决定拒绝或降级，
保证"超限"是可解释结果而非崩溃。
"""

import hashlib
import json
import time
from dataclasses import dataclass, field

from langchain_core.messages import BaseMessage
from pydantic import BaseModel

from app.contracts.agent import Budget


def estimate_tokens(text: str) -> int:
    """估算 Token 数。假设：CJK 字符约 0.7 token/字，其余按 4 字符 1 token。

    仅用于无 usage_metadata 时的近似预算控制（FakeLLM 与部分兼容网关不回用量），
    不是计费口径。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if ord(ch) > 0x2E80)
    other = len(text) - cjk
    return max(1, int(cjk * 0.7 + other / 4))


def usage_from_message(message: BaseMessage) -> int:
    """从模型响应取总 token；无 usage_metadata 时按内容估算（假设，见 estimate_tokens）。"""
    usage = getattr(message, "usage_metadata", None)
    if usage and usage.get("total_tokens"):
        return int(usage["total_tokens"])
    content = message.content
    if isinstance(content, str):
        return estimate_tokens(content)
    return estimate_tokens(json.dumps(content, ensure_ascii=False))


def args_hash(args: BaseModel) -> str:
    """工具入参指纹（账本键的组成部分）：规范化 JSON 的 sha256 前 16 位。"""
    payload = json.dumps(args.model_dump(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


@dataclass
class BudgetTracker:
    """run 级预算计数：步数 / 工具次数 / Token / 截止时间。"""

    budget: Budget
    steps_used: int = 0
    tool_calls_used: int = 0
    tokens_used: int = 0
    _t0: float = field(default_factory=time.monotonic, repr=False)

    def consume_step(self) -> bool:
        """消耗一步；False = 步数预算耗尽。"""
        self.steps_used += 1
        return self.steps_used <= self.budget.max_steps

    def steps_left(self) -> int:
        """剩余步数；0 = 最后一步（留给结论），可为负（耗尽后循环退出的过渡态）。"""
        return self.budget.max_steps - self.steps_used

    def allow_tool_call(self) -> tuple[bool, str | None]:
        """检查并消耗一次工具调用；(False, reason) = 拒绝。"""
        if self.tool_calls_used >= self.budget.max_tool_calls:
            return False, "budget_tool_calls"
        left = self.deadline_left()
        if left is not None and left <= 0:
            return False, "deadline_exceeded"
        self.tool_calls_used += 1
        return True, None

    def consume_tokens(self, n: int) -> bool:
        """记录 Token 用量；False = 超出上限（调用方应停止而非继续）。"""
        self.tokens_used += n
        return self.tokens_used <= self.budget.max_tokens

    def deadline_left(self) -> float | None:
        """剩余秒数；无 deadline 返回 None。"""
        if self.budget.deadline is None:
            return None
        return self.budget.deadline.timestamp() - time.time()

    def elapsed_s(self) -> float:
        return time.monotonic() - self._t0

    def usage(self) -> dict:
        return {
            "steps": self.steps_used,
            "tool_calls": self.tool_calls_used,
            "tokens": self.tokens_used,
        }
