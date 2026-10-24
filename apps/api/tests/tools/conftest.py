"""B 线 tests/tools 夹具：包装 b_testkit 为 pytest fixture（tests/agents/conftest.py 同模式复用）。"""

import pytest

from b_testkit import EventCollector, make_ctx  # noqa: F401  (re-export 供测试导入)


@pytest.fixture
def collector() -> EventCollector:
    return EventCollector()
