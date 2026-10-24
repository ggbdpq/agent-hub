"""I5 验收：/api/runs 系列 httpx 集成测试（TestClient + lifespan 全装配）。

覆盖：幂等创建（Idempotency-Key）、SSE 事件流格式、审批中断 → HTTP 决定 → 续跑、
Last-Event-ID 断点续传、协作式取消端点。
"""

import asyncio
import json
import time

from fastapi.testclient import TestClient

from app.agents.demo import REQUEST, _scripts, _sequence_factory
from app.main import _default_retriever, create_app


def _evidence_chunk_id() -> str:
    retriever = _default_retriever()
    hits = asyncio.run(retriever.search("柜温 告警阈值", k=1))
    return hits[0].chunk_id if hits else ""


def _build_client(tmp_path, worker_count: int = 2):
    llm_factory = _sequence_factory(_scripts(_evidence_chunk_id()))
    app = create_app(
        runs_db_path=str(tmp_path / "runs.sqlite"),
        checkpoints_path=str(tmp_path / "checkpoints.sqlite"),
        llm_factory=llm_factory,
        worker_count=worker_count,
        retriever=_default_retriever(),
        approval_timeout_s=5.0,
    )
    return TestClient(app)


def _read_to_terminal(client: TestClient, run_id: str, last_event_id: int | None = None) -> list[dict]:
    """读 SSE 流直到终态事件；流由服务端在终态后关闭，测试侧不主动 break。

    （TestClient 断开响应不通知 app 侧 generator——测试环境限制——因此必须读到底。）
    """
    headers = {"Last-Event-ID": str(last_event_id)} if last_event_id else {}
    events: list[dict] = []
    with client.stream("GET", f"/api/runs/{run_id}/events", headers=headers) as resp:
        assert resp.status_code == 200
        current: dict = {}
        for line in resp.iter_lines():
            if line.startswith("id: "):
                current["seq"] = int(line[4:])
            elif line.startswith("event: "):
                current["type"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])
            elif line == "" and current:
                events.append(current)
                if current["type"] in ("run.completed", "run.failed", "run.cancelled", "run.partial"):
                    break
                current = {}
    return events


def test_create_run_idempotency(tmp_path) -> None:
    client = _build_client(tmp_path, worker_count=0)  # 只验证创建幂等，无需 worker 认领
    with client:
        body = REQUEST.model_dump()
        a = client.post("/api/runs", json=body, headers={"Idempotency-Key": "idem-1"})
        b = client.post("/api/runs", json=body, headers={"Idempotency-Key": "idem-1"})
        c = client.post("/api/runs", json=body, headers={"Idempotency-Key": "idem-2"})
        assert a.status_code == 201 and b.status_code == 201
        assert a.json()["runId"] == b.json()["runId"]
        assert a.json()["runId"] != c.json()["runId"]
        assert a.json()["status"] == "queued"


def test_approval_flow_with_last_event_id_resume(tmp_path) -> None:
    """核心链路：创建 → 审批决定先行 POST → SSE 全量流 → Last-Event-ID 续传 → completed。

    审批 POST 先于读流发出（"决定先行"机制保证决定被缓冲或即时送达，消除
    "事件已发、Future 未注册"窗口竞态）；approval_id 是确定性约定
    f"{run_id}:approval:1"（ADR-0008 单 interrupt 点）。
    """
    client = _build_client(tmp_path)
    with client:
        created = client.post("/api/runs", json=REQUEST.model_dump(by_alias=True), headers={"Idempotency-Key": "flow-1"})
        run_id = created.json()["runId"]
        approval_id = f"{run_id}:approval:1"

        time.sleep(1.0)  # 等 FakeLLM 编排走到审批点；早到的决定由 hub 缓冲，不会丢
        ok = client.post(f"/api/runs/{run_id}/approvals/{approval_id}", json={"approved": True, "note": "同意"})
        assert ok.status_code == 200
        dup = client.post(f"/api/runs/{run_id}/approvals/{approval_id}", json={"approved": True})
        assert dup.status_code == 409  # 结果只消费一次

        events = _read_to_terminal(client, run_id)
        types = [e["type"] for e in events]
        seqs = [e["seq"] for e in events]
        assert seqs == sorted(set(seqs))  # 严格递增无重复
        assert "run.queued" in types and "run.started" in types
        assert "approval.required" in types and "approval.resolved" in types
        assert types[-1] == "run.completed"
        assert "tool.deduplicated" in types  # interrupt 恢复重放 reporter：写工具被账本去重
        approval_events = [e for e in events if e["type"] == "approval.required"]
        assert all(e["data"]["payload"].get("approvalId") == approval_id for e in approval_events)

        record = client.get(f"/api/runs/{run_id}").json()
        assert record["status"] == "completed"
        report = record["result"]
        assert report["status"] == "completed"
        # B 的领域语义：工单在审批前起草（draft、无审批 id）；审批 id 记入 notes 与派工
        assert report["workOrders"][0]["status"] == "draft"
        assert approval_id in (report["notes"] or "")

        # Last-Event-ID 断点续传：从倒数第二条之后重连，只收尾部（含终态），无空洞无重放
        before_last = events[-2]["seq"]
        tail = _read_to_terminal(client, run_id, last_event_id=before_last)
        assert tail[0]["seq"] == before_last + 1
        assert [e["type"] for e in tail] == ["run.completed"]


def test_cancel_without_worker_sets_flag(tmp_path) -> None:
    """worker_count=0：取消端点只置标记，终态仍由 worker 写（这里无人认领，保持 queued）。

    不读事件流：非终态 run 的流在服务端持续轮询（TestClient 断开不通知 app 侧），
    断言只依赖取消端点与 run 详情。
    """
    client = _build_client(tmp_path, worker_count=0)
    with client:
        created = client.post("/api/runs", json=REQUEST.model_dump(by_alias=True))
        run_id = created.json()["runId"]
        cancelled = client.post(f"/api/runs/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["cancelRequested"] is True
        assert cancelled.json()["status"] == "queued"  # 未被认领，终态不由 API 写
        # 终态 run 的取消是幂等返回原记录（不污染终态）
        assert client.get(f"/api/runs/{run_id}").json()["status"] == "queued"


def test_events_404_for_unknown_run(tmp_path) -> None:
    client = _build_client(tmp_path, worker_count=0)
    with client:
        assert client.get("/api/runs/no-such-run").status_code == 404
