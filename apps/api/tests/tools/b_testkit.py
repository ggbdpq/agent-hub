"""B 线共享测试工具包（tests/tools 与 tests/agents 复用）。

唯一模块名 ``b_testkit``：pytest prepend 导入模式下测试目录进 sys.path，
tests/agents/conftest.py 显式插入本目录后同名导入。
"""

from pathlib import Path
from typing import Any

from app.contracts.agent import Budget, RunContext
from app.contracts.events import NewEvent
from app.contracts.fakes import InMemoryLedger, StaticRetriever
from app.contracts.runtime import ToolLedger

REPO_ROOT = Path(__file__).resolve().parents[4]
FIXTURES = REPO_ROOT / "fixtures" / "inspection"


class EventCollector:
    """收集 NewEvent 的可等待 emit 替身，供断言事件序列。"""

    def __init__(self) -> None:
        self.events: list[NewEvent] = []

    async def __call__(self, event: NewEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]

    def of_type(self, event_type: str) -> list[NewEvent]:
        return [e for e in self.events if e.type == event_type]


def no_llm() -> Any:
    raise RuntimeError("测试未提供 LLM")


def sequence_llm_factory(responses: list) -> Any:
    """逐次推进的 ChatModelFactory：每次 build() 消耗一条脚本（区别于 FakeLLM.factory 的重放）。

    用于"每次尝试调 ctx.llm() 取新实例"的组件（B3 planner 重试、B5 扇出中
    planner/inspector/reporter 的先后差异脚本）。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    queue = list(responses)
    build_count = {"n": 0}

    def build() -> GenericFakeChatModel:
        build_count["n"] += 1
        if queue:
            raw = queue.pop(0)
        else:  # 脚本耗尽：返回空内容（调用方按解析失败处理）
            raw = ""
        msg = raw if isinstance(raw, AIMessage) else AIMessage(content=raw)
        return GenericFakeChatModel(messages=iter([msg]))

    build.build_count = build_count  # type: ignore[attr-defined]
    return build


def make_ctx(
    *,
    budget: Budget | None = None,
    ledger: ToolLedger | None = None,
    retriever: StaticRetriever | None = None,
    llm: Any = None,
    cancelled: bool = False,
    collector: EventCollector | None = None,
) -> tuple[RunContext, EventCollector]:
    """构造带事件收集器的 RunContext（测试用替身组合）。"""
    col = collector or EventCollector()
    ctx = RunContext(
        emit=col,
        cancelled=lambda: cancelled,
        budget=budget or Budget(max_steps=24, max_tool_calls=40, max_tokens=100_000),
        ledger=ledger or InMemoryLedger(),
        retriever=retriever or StaticRetriever(),
        llm=llm or no_llm,
    )
    return ctx, col
