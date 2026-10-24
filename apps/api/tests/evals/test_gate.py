"""C10：质量门——离线基线指标低于阈值时退出码为 1（真正让 CI 失败）。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from evals.gate import DEFAULT_THRESHOLDS, check_thresholds, main  # noqa: E402


def test_default_thresholds_structure() -> None:
    assert "retrieval" in DEFAULT_THRESHOLDS and "agent" in DEFAULT_THRESHOLDS
    hybrid = DEFAULT_THRESHOLDS["retrieval"]["hybrid-bigram"]
    assert hybrid["recall_at_5_min"] > 0 and hybrid["mrr_min"] > 0
    assert DEFAULT_THRESHOLDS["agent"]["success_rate_min"] > 0
    assert DEFAULT_THRESHOLDS["agent"]["forbidden_calls_max"] == 0


def test_check_thresholds_pass_and_fail() -> None:
    retrieval = {"hybrid-bigram": {"recall_at_5": 0.9, "recall_at_3": 0.8, "mrr": 0.7}}
    agent = {"success_rate": 0.9, "anomaly_recall": 0.85, "forbidden_calls_total": 0,
             "write_cap_violations_total": 0, "crashed_total": 0}
    failures = check_thresholds(DEFAULT_THRESHOLDS, retrieval, agent)
    assert failures == [], f"基线水平指标应通过：{failures}"

    retrieval_bad = {"hybrid-bigram": {"recall_at_5": 0.5, "recall_at_3": 0.4, "mrr": 0.2}}
    failures = check_thresholds(DEFAULT_THRESHOLDS, retrieval_bad, agent)
    assert any("recall_at_5" in f for f in failures), "低于阈值必须产生失败项"


def test_agent_forbidden_calls_fail() -> None:
    agent = {"success_rate": 0.9, "anomaly_recall": 0.85, "forbidden_calls_total": 2,
             "write_cap_violations_total": 0, "crashed_total": 0}
    failures = check_thresholds(DEFAULT_THRESHOLDS, {"hybrid-bigram": {"recall_at_5": 0.9, "recall_at_3": 0.8, "mrr": 0.7}}, agent)
    assert any("forbidden_calls" in f for f in failures)


def test_gate_main_exit_codes(tmp_path: Path) -> None:
    assert main([]) == 0, "当前离线基线必须通过质量门"

    from evals import gate

    saved = gate.load_baseline
    passing_agent = {
        "success_rate": 1.0,
        "anomaly_recall": 0.9,
        "forbidden_calls_total": 0,
        "write_cap_violations_total": 0,
        "crashed_total": 0,
    }

    def broken_baseline(suite: str):
        if suite == "retrieval":
            return {"hybrid-bigram": {"recall_at_5": 0.1, "recall_at_3": 0.1, "mrr": 0.1}}
        return passing_agent

    try:
        gate.load_baseline = broken_baseline  # type: ignore[assignment]
        assert main([]) == 1, "低于阈值必须退出码 1"
    finally:
        gate.load_baseline = saved  # type: ignore[assignment]
