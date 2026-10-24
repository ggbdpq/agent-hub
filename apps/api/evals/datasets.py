"""评测数据集加载（C0/C7）：标注与评测集存放在 evals/data/**，均为合成数据标注。

路径解析：默认相对仓库根（apps/api 的上两级）定位 fixtures/inspection/**，
可用环境变量 ``AGENT_HUB_ROOT`` 覆盖仓库根。
"""

import json
import os
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]  # apps/api
REPO_ROOT = Path(os.environ.get("AGENT_HUB_ROOT", API_ROOT.parent.parent))

DATA_DIR = Path(__file__).resolve().parent / "data"
ANNOTATIONS_FILE = DATA_DIR / "retrieval_annotations.json"
AGENT_CASES_FILE = DATA_DIR / "agent_cases.json"


def corpus_root() -> Path:
    return REPO_ROOT / "fixtures" / "inspection" / "corpus"


def fixtures_root() -> Path:
    return REPO_ROOT / "fixtures" / "inspection"


def load_retrieval_annotations() -> dict:
    """C0 检索评测标注：{"version", "note", "queries": [{qid, query, relevant_chunk_ids}]}。"""
    return json.loads(ANNOTATIONS_FILE.read_text(encoding="utf-8"))
