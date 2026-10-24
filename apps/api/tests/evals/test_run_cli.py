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
    assert payload["annotations"] == 60
    assert len(payload["variants"]) == 4
    assert "不代表模型效果" in payload["disclaimer"]
    md = md_files[0].read_text(encoding="utf-8")
    assert "## 分词方案结论（U4）" in md
    assert "不代表模型效果" in md
    assert "| 变体 | Recall@1 | Recall@3 | Recall@5 | MRR | P50 (ms) | P95 (ms) | 空结果率 |" in md
