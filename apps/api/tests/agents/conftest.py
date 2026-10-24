"""B 线 tests/agents 夹具：显式把 tests/tools 加入 sys.path 以复用 b_testkit。"""

import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))
