"""评测 CLI 端到端：python -m evals.run --suite retrieval 产出报告文件。"""

import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

import json  # noqa: E402

from evals.run import main  # noqa: E402


def test_retrieval_suite_writes_reports(tmp_path: Path) -> None:
    code = main(["--suite", "retrieval", "--out-dir", str(tmp_path)])
    assert code == 0
    md_files = list(tmp_path.glob("retrieval-*.md"))
    json_files = list(tmp_path.glob("retrieval-*.json"))
    assert md_files and json_files
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert payload["suite"] == "retrieval"
    assert payload["sample_size"]["queries"] == 60
    names = [v["name"] for v in payload["variants"]]
    assert {"hybrid-bigram", "hybrid-unigram", "bm25-only-bigram", "vector-only-bigram"} <= set(names)
    assert len(payload["variants"]) in (4, 6), f"jieba 可装时应为 6 变体，实际 {names}"
    if len(payload["variants"]) == 6:
        assert {"hybrid-jieba", "bm25-only-jieba"} <= set(names)
        assert "U4 关闭" in md_files[0].read_text(encoding="utf-8") or "建议主线程批准" in md_files[0].read_text(encoding="utf-8")
    assert all("recall_at_5_std" in v["aggregates"] for v in payload["variants"])
    assert "不代表模型效果" in payload["disclaimer"]
    md = md_files[0].read_text(encoding="utf-8")
    assert "## 分词方案结论（U4" in md
    assert "不代表模型效果" in md
    assert "| 变体 | Recall@1 | Recall@3 | Recall@5 | Recall@5±σ | MRR | P50 (ms) | P95 (ms) | 空结果率 |" in md
    assert "## 方差与波动" in md
    assert "## 已知偏差" in md


def test_agent_suite_writes_reports_with_full_event_trace(tmp_path: Path) -> None:
    from evals.run import main as run_main

    code = run_main(["--suite", "agent", "--runner", "scripted-tools", "--out-dir", str(tmp_path)])
    assert code == 0
    json_files = list(tmp_path.glob("agent-scripted-tools-*.json"))
    md_files = list(tmp_path.glob("agent-scripted-tools-*.md"))
    assert json_files and md_files
    payload = json.loads(json_files[0].read_text(encoding="utf-8"))
    assert payload["suite"] == "agent"
    assert len(payload["cases"]) == 60
    first = payload["cases"][0]
    assert first["events"], "每条用例必须保存完整事件序列"
    assert first["expected"] and first["input"], "记录必须带期望与输入供复盘"
    agg = payload["aggregates"]
    assert agg["success_rate"] >= 0.9
    assert agg["anomaly_recall"] is not None
    md = md_files[0].read_text(encoding="utf-8")
    assert "不代表模型效果" in md
    assert "## 已知偏差" in md
