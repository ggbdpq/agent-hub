"""X3：E3 并行 vs 串行扇出（离线替身层，不改生产默认）。时延为注入的模拟 LLM 时延。"""

import sys
import time
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from evals.compliant_llm import CompliantLLMFactory  # noqa: E402


def _ctx_factory(delay_s: float = 0.0):
    from app.contracts.fakes import InMemoryLedger, StaticRetriever
    from app.contracts.agent import Budget, RunContext

    events: list = []

    async def emit(event) -> None:
        events.append(event)

    factory = CompliantLLMFactory(delay_s=delay_s)
    ctx = RunContext(
        emit=emit,
        cancelled=lambda: False,
        budget=Budget(),
        ledger=InMemoryLedger(),
        retriever=StaticRetriever(),
        llm=factory.build,
    )
    return ctx, factory, events


def _request(device_ids: list[str]):
    from app.contracts.agent import InspectionRequest

    return InspectionRequest(site="SITE-A", device_ids=device_ids, goal="巡检并处置：异常设备按规程生成工单")


DEVICES_3 = ["INV-001", "INV-005", "WT-001"]


async def _run_mode(mode: str, devices: list[str], delay_s: float = 0.0):
    from evals.datasets import fixtures_root
    from evals.e3_fanout import FanoutSimRunner

    ctx, factory, _ = _ctx_factory(delay_s)
    runner = FanoutSimRunner(fanout_mode=mode, fixture_root=fixtures_root())
    start = time.perf_counter()
    report = await runner.run("e3-test", _request(devices), ctx)
    elapsed = time.perf_counter() - start
    return report, factory, elapsed


async def test_parallel_and_serial_produce_equivalent_outcomes() -> None:
    par, _, _ = await _run_mode("parallel", DEVICES_3)
    ser, _, _ = await _run_mode("serial", DEVICES_3)
    assert par.status == ser.status == "completed"
    assert {f.device_id for f in par.findings} == {f.device_id for f in ser.findings} == set(DEVICES_3)
    assert {f.device_id for f in par.findings if f.level != "normal"} == {
        f.device_id for f in ser.findings if f.level != "normal"
    }  # 判读结论一致（扇出方式不是结论变量）


async def test_serial_slower_than_parallel_with_injected_llm_delay() -> None:
    _, _, t_par = await _run_mode("parallel", DEVICES_3, delay_s=0.05)
    _, _, t_ser = await _run_mode("serial", DEVICES_3, delay_s=0.05)
    # 每台 2-3 次 LLM 调用 × 50ms：串行 ≈ Σ，并行 ≈ max —— 3 台时串行至少慢 ~2×
    assert t_ser > t_par * 1.5, f"serial={t_ser:.3f}s parallel={t_par:.3f}s"


def test_device_bucket_mapping() -> None:
    from evals.e3_fanout import device_bucket

    assert device_bucket(1) == "1 台"
    assert device_bucket(2) == "2-3 台"
    assert device_bucket(3) == "2-3 台"
    assert device_bucket(4) == "4-5 台"
    assert device_bucket(5) == "4-5 台"
    assert device_bucket(8) == "6+ 台"
