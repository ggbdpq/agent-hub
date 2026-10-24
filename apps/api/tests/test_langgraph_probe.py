"""U1 试验脚本（ADR-0007 依据）：Send 扇出 + interrupt 审批 + SQLite 检查点跨"进程"恢复。

计划第 12 节 U1：LangGraph API 细节以本文件实测为准（langgraph 1.2.17 /
langgraph-checkpoint-sqlite 3.1.2）。写成 pytest 用例，CI 跑 pytest 即回归本试验。

验证三个机制，对应子智能体 B5/B7/B8 的依赖：
1. supervisor 节点返回 ``Send`` 列表 → inspector 并行扇出，结果经 reducer 合并；
2. reporter 内 ``interrupt()`` 暂停等人工审批，``Command(resume=...)`` 注入审批结果；
3. 关闭连接再重开 SqliteSaver（模拟进程重启）后，从检查点恢复：扇出结果不丢，
   已完成的 inspector **不重复执行**。
"""

import operator
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

DEVICES = ["DE-001", "DE-002", "DE-003"]

# inspector 实际执行的设备序号（跨两次"进程"累计），用于断言续跑不重复执行
_CALLED: list[str] = []


class ProbeState(TypedDict):
    devices: list[str]
    findings: Annotated[list[dict], operator.add]
    report: dict | None


def _build_graph(saver):
    graph = StateGraph(ProbeState)

    def supervisor(state: ProbeState) -> dict:
        return {}  # 扇出经条件边发出（langgraph 1.x：节点返回值不接受 Send 列表）

    def inspector(task: dict) -> dict:
        _CALLED.append(task["device_id"])
        return {"findings": [{"device_id": task["device_id"], "level": "ok"}]}

    def reporter(state: ProbeState) -> dict:
        # 人工审批中断（B7）：恢复后 interrupt() 的返回值即 Command(resume=...) 传入值
        decision = interrupt({"question": "dispatch_drone?", "pending": state["findings"]})
        return {"report": {"approval": decision, "coverage": len(state["findings"])}}

    graph.add_node("supervisor", supervisor)
    graph.add_node("inspector", inspector)
    graph.add_node("reporter", reporter)
    graph.add_edge(START, "supervisor")
    # 并行扇出：每台设备一个 inspector 子任务（B5 的核心机制）
    graph.add_conditional_edges(
        "supervisor",
        lambda state: [Send("inspector", {"device_id": d}) for d in state["devices"]],
        ["inspector"],
    )
    graph.add_edge("inspector", "reporter")  # 扇出全部汇合后进 reporter
    graph.add_edge("reporter", END)
    return graph.compile(checkpointer=saver)


def test_sync_send_interrupt_sqlite_checkpoint(tmp_path) -> None:
    db = str(tmp_path / "probe.sqlite")
    config = {"configurable": {"thread_id": "t-sync"}}
    _CALLED.clear()

    # 第一次"进程"：跑到审批中断
    with SqliteSaver.from_conn_string(db) as saver:
        result = _build_graph(saver).invoke(
            {"devices": DEVICES, "findings": [], "report": None}, config
        )
        assert result["report"] is None
        assert len(result["findings"]) == 3
        interrupted = result["__interrupt__"][0].value
        assert interrupted["question"] == "dispatch_drone?"

    # 第二次"进程"：重开同一 db 文件，注入审批结果
    with SqliteSaver.from_conn_string(db) as saver:
        resumed = _build_graph(saver).invoke(Command(resume="approved"), config)

    assert resumed["report"] == {"approval": "approved", "coverage": 3}
    assert sorted(_CALLED) == DEVICES  # 恢复后 inspector 不重跑


async def test_async_send_interrupt_sqlite_checkpoint(tmp_path) -> None:
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    db = str(tmp_path / "probe-async.sqlite")
    config = {"configurable": {"thread_id": "t-async"}}
    _CALLED.clear()

    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        result = await _build_graph(saver).ainvoke(
            {"devices": DEVICES, "findings": [], "report": None}, config
        )
        assert result["report"] is None
        assert len(result["findings"]) == 3

    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        resumed = await _build_graph(saver).ainvoke(Command(resume="approved"), config)

    assert resumed["report"] == {"approval": "approved", "coverage": 3}
    assert sorted(_CALLED) == DEVICES
