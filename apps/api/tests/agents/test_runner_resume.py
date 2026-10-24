"""R1 回归：审批等待期"重启"（重认领）→ runner.run 必须从检查点恢复，不得整图重跑。

生产路径：worker 重认领 run → InspectionAgentRunner.run(run_id, input, ctx) 再次进入。
修复前 runner 永远带输入 ainvoke → 整图从 START 重跑 → findings（operator.add）
在检查点上翻倍、coverage=2.0 仍 completed、planner/inspector 重复执行。
修复后：run 先 aget_state，发现 pending interrupt → 经 approver 路由决定 →
Command(resume) 恢复；findings/failures reducer 另按 device_id 去重加固。
"""

import json

from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from app.agents.runner import InspectionAgentRunner
from app.contracts.agent import InspectionRequest
from app.contracts.fakes import StaticRetriever
from app.contracts.knowledge import Chunk
from b_testkit import FIXTURES, make_ctx, sequence_llm_factory

REQUEST = InspectionRequest(site="SITE-A", device_ids=["INV-001", "INV-005", "INV-002"])

CHUNK = Chunk(
    chunk_id="doc1#异常判据#1",
    doc_id="doc1",
    title="异常判据",
    text="柜温超过 85 摄氏度立即上报",
    source_path="corpus/doc1.md",
)


def _script_for(device_id: str, level: str) -> list[AIMessage]:
    return [
        AIMessage(content=json.dumps({"tool": "detect_anomaly", "args": {"device_id": device_id}})),
        AIMessage(
            content=json.dumps(
                {"final": {"summary": f"{device_id} 判读完成", "level": level, "evidence_chunk_ids": [], "metrics": {}}},
                ensure_ascii=False,
            )
        ),
    ]


def _scripts() -> list:
    """planner 1 + inspector 3 + reporter 起草 ×2（恢复重跑 reporter 会再起草一次）。"""
    return [
        json.dumps(
            {
                "steps": [
                    {"device_id": "INV-001", "checks": ["遥测比对"]},
                    {"device_id": "INV-005", "checks": ["柜温趋势"]},
                    {"device_id": "INV-002", "checks": ["遥测比对"]},
                ]
            },
            ensure_ascii=False,
        ),
        _script_for("INV-001", "normal"),
        _script_for("INV-005", "critical"),
        _script_for("INV-002", "normal"),
        json.dumps({"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}]}, ensure_ascii=False),
        json.dumps({"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热"}]}, ensure_ascii=False),
    ]


async def test_reclaimed_run_resumes_from_checkpoint_not_rerun(tmp_path) -> None:
    """进程1 停在审批中断（approver 崩溃模拟 kill）→ 进程2 重认领后 run() 恢复完成。"""
    db = str(tmp_path / "resume.sqlite")
    run_id = "run-r1"
    config_run = run_id
    node_started: dict[str, int] = {}

    def count_started(collector) -> None:
        node_started.clear()
        for e in collector.events:
            if e.type == "node.started":
                node_started[e.node] = node_started.get(e.node, 0) + 1

    ctx, collector = make_ctx(llm=sequence_llm_factory(_scripts()), retriever=StaticRetriever([CHUNK]))
    input_ = REQUEST

    # —— 进程 1：跑到审批中断后"崩溃"（approver 抛异常模拟 kill，检查点已落） ——
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        runner = InspectionAgentRunner(
            checkpointer=saver,
            approver=lambda payload: (_ for _ in ()).throw(RuntimeError("进程被 kill（模拟）")),
        )
        try:
            await runner.run(config_run, input_, ctx)
            raise AssertionError("进程 1 应停在审批中断（approver 崩溃）")
        except RuntimeError as exc:
            assert "kill" in str(exc)
        count_started(collector)
        assert node_started == {"supervisor": 1, "planner": 1, "inspector": 3, "reporter": 1}

    # —— 进程 2：worker 重认领（新 saver 连接 = 新"进程"），runner.run 恢复 ——
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        runner2 = InspectionAgentRunner(
            checkpointer=saver,
            approver=lambda payload: {"approved": False, "approval_id": "", "note": "重启后人工复核：暂不派机"},
        )
        report = await runner2.run(config_run, input_, ctx)

    assert report.status == "completed"
    assert report.coverage == 1.0, f"coverage 翻倍: {report.coverage}"
    assert len(report.findings) == 3, f"findings 翻倍: {len(report.findings)}"
    assert {f.device_id for f in report.findings} == {"INV-001", "INV-005", "INV-002"}
    # planner / inspector 不重跑；工单只执行一次（恢复重跑 reporter 走账本去重）
    count_started(collector)
    assert node_started.get("planner") == 1, f"planner 被重跑: {node_started}"
    assert node_started.get("inspector") == 3, f"inspector 被重跑: {node_started}"
    created = [e for e in collector.of_type("tool.called") if e.payload["tool"] == "create_work_order"]
    deduped = [e for e in collector.of_type("tool.deduplicated") if e.payload["tool"] == "create_work_order"]
    assert len(created) == 1 and len(deduped) == 1
    assert "重启后人工复核" in (report.notes or "")


async def test_reclaimed_run_with_async_approver(tmp_path) -> None:
    """R-Y1 回归：进程 2 的 approver 是 async（生产 ApprovalHub 形态）也必须能恢复。

    修复前 pending 分支 `decision = self._approver(pending)` 未 await → 协程对象
    进入 Command(resume) → langgraph msgpack 序列化 TypeError → run.failed。
    """
    db = str(tmp_path / "ckpt-async.sqlite")
    run_id = "run-async-appr"

    # 进程 1：approver 崩溃模拟 kill，停在审批中断（脚本与首个回归用例相同）
    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        ctx, collector = make_ctx(llm=sequence_llm_factory(_scripts()), retriever=StaticRetriever([CHUNK]))
        runner = InspectionAgentRunner(
            checkpointer=saver,
            approver=lambda payload: (_ for _ in ()).throw(RuntimeError("kill")),
        )
        try:
            await runner.run(run_id, REQUEST, ctx)
            raise AssertionError("进程 1 应停在审批中断")
        except RuntimeError:
            pass

    # 进程 2：async approver（生产 ApprovalHub 形态）恢复
    async def async_approver(payload: dict) -> dict:
        return {"approved": False, "approvalId": "", "note": "异步审批：拒绝"}

    async with AsyncSqliteSaver.from_conn_string(db) as saver:
        ctx2, _ = make_ctx(llm=sequence_llm_factory(_scripts()), retriever=StaticRetriever([CHUNK]))
        runner2 = InspectionAgentRunner(checkpointer=saver, approver=async_approver)
        report = await runner2.run(run_id, REQUEST, ctx2)

    assert report.status == "completed", f"async approver 恢复失败: {report.notes}"
    assert report.coverage == 1.0
    assert len(report.findings) == 3, f"findings 翻倍: {len(report.findings)}"
    assert "异步审批" in (report.notes or "")
