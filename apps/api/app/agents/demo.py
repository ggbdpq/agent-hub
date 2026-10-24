"""巡检多智能体演示（B 收尾验收）：`python -m app.agents.demo --fake-llm`。

打印完整事件序列（RunStore 持久化后）+ 最终报告 JSON：
create_run → claim（租约/fencing token）→ InspectionAgentRunner 全流程
（规划 → Send 扇出巡检 → 工单 → critical 审批 interrupt → 自动审批 → 报告）
→ finish（终态 CAS）。

--fake-llm：contracts/fakes.FakeLLM 脚本回放（离线确定性，开发与测试口径）；
不带参数：走 app.agents.models 的 OpenAI 兼容真实模型（需 OPENAI_API_KEY）。
"""

import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from app.agents.models import env_chat_model_factory
from app.agents.runner import InspectionAgentRunner, build_context_from_claim
from app.contracts.agent import Budget, InspectionRequest
from app.contracts.events import RunEvent
from app.runtime.store import SqliteLedger, SqliteRunStore

REQUEST = InspectionRequest(
    site="SITE-A",
    device_ids=["INV-001", "INV-005", "INV-002"],
    window="2026-10-23 00:00~23:00Z",
    goal="例行巡检：遥测比对与异常判读",
)


def _demo_retriever():
    """真实检索（I3 接线）：C 的语料切分 + 确定性 Hashing 嵌入 + BM25/向量 RRF 融合。"""
    from app.knowledge.embedding import HashingEmbedder
    from app.knowledge.retriever import HybridRetriever
    from app.knowledge.splitting import split_corpus
    from app.tools.devices import DEFAULT_FIXTURE_ROOT

    return HybridRetriever(
        split_corpus(DEFAULT_FIXTURE_ROOT / "corpus"), embedder=HashingEmbedder(dim=512)
    )


def _scripts(evidence_chunk_id: str) -> list:
    """FakeLLM 脚本：planner 1 + inspector 3（判读→检索→结论）+ reporter 起草 ×2（审批恢复重跑）。

    结论引用的 chunk_id 来自接线后的真实检索（先查一次语料取回），保证证据链可回溯。
    """
    from langchain_core.messages import AIMessage

    def insp(device_id: str) -> list:
        return [
            AIMessage(content=json.dumps({"tool": "detect_anomaly", "args": {"device_id": device_id}})),
            AIMessage(content=json.dumps({"tool": "search_procedures", "args": {"query": "异常判据 柜温"}})),
            AIMessage(
                content=json.dumps(
                    {
                        "final": {
                            "summary": f"{device_id}：结合确定性判读与规程条款给出结论",
                            "evidence_chunk_ids": [evidence_chunk_id],
                            "metrics": {},
                        }
                    },
                    ensure_ascii=False,
                )
            ),
        ]

    return [
        json.dumps(
            {
                "steps": [
                    {"device_id": "INV-001", "checks": ["遥测比对", "柜温趋势"]},
                    {"device_id": "INV-005", "checks": ["柜温趋势", "散热检查"]},
                    {"device_id": "INV-002", "checks": ["遥测比对"]},
                ]
            },
            ensure_ascii=False,
        ),
        insp("INV-001"),
        insp("INV-005"),
        insp("INV-002"),
        json.dumps(
            {"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热与柜体密封，按告警阈值规程处置"}]},
            ensure_ascii=False,
        ),
        json.dumps(
            {"orders": [{"device_id": "INV-005", "title": "柜温超限现场检查", "action": "检查散热与柜体密封，按告警阈值规程处置"}]},
            ensure_ascii=False,
        ),
    ]


def _print_events(events: list[RunEvent]) -> None:
    print(f"\n—— 事件序列（共 {len(events)} 条，seq 由 RunStore 分配）——")
    for e in events:
        payload = json.dumps(e.payload, ensure_ascii=False)
        if len(payload) > 90:
            payload = payload[:90] + "…"
        print(f"  #{e.seq:<3} [{e.type}] node={e.node or '-'} {payload}")


def _sequence_factory(responses: list):
    """逐次推进的本地工厂：每次 ctx.llm() 消耗一条脚本；列表项 = 多轮脚本。

    与 tests/tools/b_testkit.sequence_llm_factory 同语义（测试工具不入生产包，
    demo 自带这份小实现）。
    """
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage

    queue = list(responses)

    def build() -> GenericFakeChatModel:
        item = queue.pop(0) if queue else ""
        if isinstance(item, list):
            msgs = [m if isinstance(m, AIMessage) else AIMessage(content=m) for m in item]
        else:
            msgs = [item if isinstance(item, AIMessage) else AIMessage(content=item)]
        return GenericFakeChatModel(messages=iter(msgs))

    return build


async def main_async(use_fake_llm: bool) -> int:
    db_path = Path(tempfile.gettempdir()) / "agent-hub-demo-runs.sqlite"
    if db_path.exists():
        db_path.unlink()  # 演示用固定幂等键，每次全新库避免历史 run 干扰重复演示
    store = SqliteRunStore(str(db_path))
    ledger = SqliteLedger(str(db_path))
    retriever = _demo_retriever()
    hits = await retriever.search("柜温 告警阈值", k=1)
    evidence_chunk_id = hits[0].chunk_id if hits else ""
    print(f"检索接线自检：语料命中 chunk_id = {evidence_chunk_id or '（无命中）'}")

    if use_fake_llm:
        llm_factory = _sequence_factory(_scripts(evidence_chunk_id))
    else:
        llm_factory = env_chat_model_factory()  # 未配置 OPENAI_API_KEY 时显式报错

    record = store.create_run("inspection", REQUEST.model_dump(), idempotency_key="demo-inspection-1")
    claim = store.claim(worker_id="demo", lease_s=600)
    assert claim is not None
    print(f"run_id = {claim.run_id}（thread_id 同值）attempt={claim.attempt} fencing_token={claim.fencing_token}")

    ctx = build_context_from_claim(
        store,
        claim,
        ledger=ledger,
        retriever=retriever,
        llm_factory=llm_factory,
        budget=Budget(max_steps=32, max_tool_calls=64, max_tokens=200_000),
    )

    def demo_approver(payload: dict) -> dict:
        print(f"\n[人工审批点] {payload.get('question')}")
        print(f"  待复核设备: {payload.get('devices')}")
        decision = {"approved": True, "approval_id": "AP-DEMO-1", "note": "演示自动审批：同意派无人机"}
        print(f"  决定: {decision['note']}（approval_id={decision['approval_id']}）")
        return decision

    runner = InspectionAgentRunner(
        approver=demo_approver,
        checkpointer=InMemorySaver(),
        inspector_skills=["telemetry-triage", "procedure-lookup"],  # B10：Skill 加载能力
    )
    # 预算由 runner 从 ctx.budget 组装（BudgetTracker 在 runner 内创建）

    report = await runner.run(claim.run_id, REQUEST, ctx)

    finished = store.finish(
        claim.run_id, claim.fencing_token, report.status, result=report.model_dump()
    )
    print(f"\nfinish({report.status}) -> {finished}")

    _print_events(store.events_after(claim.run_id, 0))

    print("\n—— 巡检报告 ——")
    print(json.dumps(report.model_dump(), ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="agent-hub 巡检多智能体演示")
    parser.add_argument("--fake-llm", action="store_true", help="使用 FakeLLM 脚本回放（离线确定性）")
    args = parser.parse_args()
    return asyncio.run(main_async(use_fake_llm=args.fake_llm))


if __name__ == "__main__":
    raise SystemExit(main())
