"""C7 评测集构建（一次性脚本，提交以记录来源）：evals/data/agent_cases.json。

**期望异常设备与等级来自 evals.derive_expectations.detect（语料判据代码化）对遥测的
推导结果**，本脚本只组织用例结构，不手写任何期望异常；构建后由
tests/evals/test_agent_cases.py 逐条复核（数据与推导漂移即红）。

再构建：``python -m evals.build_agent_cases``
"""

import json
from pathlib import Path

from evals.datasets import fixtures_root
from evals.derive_expectations import detect_device

DATA_FILE = Path(__file__).resolve().parent / "data" / "agent_cases.json"

WINDOW = "2026-10-23 00:00~23:00Z"
BASE_TOOLS = ["get_telemetry", "detect_anomaly"]
BATCH_TOOLS = ["list_devices", "get_telemetry", "detect_anomaly"]
FULL_TOOLS = ["list_devices", "get_telemetry", "detect_anomaly", "search_procedures", "create_work_order"]
ORDER_CHECK = [["get_telemetry", "detect_anomaly"], ["detect_anomaly", "create_work_order"]]
FORBIDDEN_WRITE = ["create_work_order", "dispatch_drone"]
FORBIDDEN_DRONE = ["dispatch_drone"]

# 触发异常的主参数（从推导 evidence 的 metric 归纳，写入工单必含字段）
ANOMALY_DEVICES = ["INV-005", "WT-002", "PCS-003", "SW-002", "BS-001"]
NORMAL_DEVICES = [
    "INV-001", "INV-002", "INV-003", "INV-004", "INV-006", "INV-007", "INV-008",
    "WT-001", "WT-003", "WT-004", "PCS-001", "PCS-002", "SW-001", "SW-003", "BS-002",
]
SITE_OF: dict[str, str] = {}

_GOAL_EASY = "例行巡检：核对遥测是否越限，无异常只出结论，不开工单。"
_GOAL_MEDIUM = "巡检并处置：异常设备按规程生成工单，注明设备编号、异常参数、实测值与判定依据。"
_GOAL_READONLY = "只诊断不生成工单：输出异常结论与依据，禁止创建工单。"
_GOAL_DRONE = "巡检并对异常设备安排无人机现场复核（需审批），同时按规程生成工单。"


def _expected(device_ids: list[str], *, tools: list[str], forbidden: list[str],
              orders: bool = False, readonly: bool = False, drone: bool = False,
              status: list[str] | None = None, evidence: bool = False,
              derived: dict[str, dict] | None = None) -> dict:
    derived = derived or {d: detect_device(d) for d in device_ids}
    anomalies = [d for d in device_ids if derived[d]["anomalous"]]
    anomaly_levels = {d: derived[d]["level"] for d in anomalies}
    work_orders = []
    if orders:
        for d in anomalies:
            metrics = sorted({e["metric"] for e in derived[d]["evidence"] if not e["metric"].endswith("_pct")})
            if not metrics:  # 骤降类只有派生指标，引用原始参数名
                metrics = ["output_kw"]
            work_orders.append({"device_id": d, "must_mention": [d, *metrics]})
    exp: dict = {
        "anomaly_devices": anomalies,
        "anomaly_levels": anomaly_levels,
        "tools_expected": tools + (["dispatch_drone"] if drone else []),
        "tools_order_required": ORDER_CHECK if (orders and not readonly) else [["get_telemetry", "detect_anomaly"]],
        "forbidden_tools": forbidden,
        "work_orders": work_orders,
        "status_allowed": status or ["completed"],
        "evidence_required": evidence,
    }
    if status and status != ["completed"]:
        exp["max_write_calls_per_device"] = 1  # 故障注入：写工具不得重复执行
    return exp


def build() -> dict:
    devices = json.loads((fixtures_root() / "devices.json").read_text(encoding="utf-8"))["devices"]
    for d in devices:
        SITE_OF[d["deviceId"]] = d["site"]
    derived = {d: detect_device(d) for d in [*ANOMALY_DEVICES, *NORMAL_DEVICES]}
    cases: list[dict] = []

    def add(cid: str, difficulty: str, device_ids: list[str], goal: str, exp: dict,
            fault: dict | None = None) -> None:
        cases.append({
            "cid": cid,
            "difficulty": difficulty,
            "input": {"site": SITE_OF[device_ids[0]], "device_ids": device_ids,
                      "window": WINDOW, "goal": goal},
            "expected": exp,
            "fault": fault,
        })

    # ---- easy（20）：只读例行巡检，无异常期望 ----
    for i, dev in enumerate(NORMAL_DEVICES, start=1):
        add(f"E{i:02d}", "easy", [dev], _GOAL_EASY,
            _expected([dev], tools=BASE_TOOLS, forbidden=FORBIDDEN_WRITE, derived=derived))
    easy_batches = [
        ["INV-001", "INV-002", "INV-003"],
        ["INV-006", "INV-007", "INV-008"],
        ["WT-001", "WT-003", "WT-004"],
        ["PCS-001", "PCS-002"],
        ["SW-001", "SW-003", "BS-002"],
    ]
    for j, batch in enumerate(easy_batches, start=16):
        add(f"E{j:02d}", "easy", batch, _GOAL_EASY,
            _expected(batch, tools=BATCH_TOOLS, forbidden=FORBIDDEN_WRITE, derived=derived))

    # ---- medium（20）：含异常设备，要求规程依据与工单 ----
    singles = ANOMALY_DEVICES
    for i, dev in enumerate(singles, start=1):
        add(f"M{i:02d}", "medium", [dev], _GOAL_MEDIUM,
            _expected([dev], tools=FULL_TOOLS[1:], forbidden=FORBIDDEN_DRONE,
                      orders=True, evidence=True, derived=derived))
    mixed = [
        ["INV-005", "INV-001"],
        ["INV-005", "INV-002", "INV-003", "INV-004"],
        ["INV-005", "INV-006"],
        ["INV-005", "INV-007", "INV-008"],
        ["WT-002", "WT-001"],
        ["WT-002", "WT-003", "WT-004"],
        ["PCS-003", "PCS-001", "PCS-002"],
        ["SW-002", "SW-001"],
        ["SW-002", "SW-003", "BS-002"],
        ["BS-001", "BS-002"],
        ["BS-001", "SW-001"],
        ["INV-005", "WT-002", "PCS-003", "SW-002", "BS-001"],
    ]
    for k, batch in enumerate(mixed, start=6):
        add(f"M{k:02d}", "medium", batch, _GOAL_MEDIUM,
            _expected(batch, tools=FULL_TOOLS, forbidden=FORBIDDEN_DRONE,
                      orders=True, evidence=True, derived=derived))
    # M18-M20：跨站多异常（同站点输入在 demo 语义下允许跨站点名设备）
    cross = [
        ["INV-005", "WT-002"],
        ["PCS-003", "SW-002"],
        ["INV-005", "BS-001", "SW-002"],
    ]
    for k, batch in enumerate(cross, start=18):
        add(f"M{k:02d}", "medium", batch, _GOAL_MEDIUM,
            _expected(batch, tools=FULL_TOOLS, forbidden=FORBIDDEN_DRONE,
                      orders=True, evidence=True, derived=derived))

    # ---- hard（20）：10 条故障注入 + 10 条复杂流程 ----
    faults = [
        ("H01", "tool_timeout", [ "INV-005"], {"tool": "get_telemetry"}),
        ("H02", "tool_timeout", ["INV-001", "INV-005"], {"tool": "get_telemetry"}),
        ("H03", "tool_timeout", ["WT-002"], {"tool": "search_procedures"}),
        ("H04", "tool_timeout", ["PCS-003"], {"tool": "detect_anomaly"}),
        ("H05", "llm_invalid_output", ["INV-005"], {}),
        ("H06", "llm_invalid_output", ["SW-002", "SW-001"], {}),
        ("H07", "llm_invalid_output", ["BS-001"], {}),
        ("H08", "subtask_failure", ["INV-004", "INV-005", "INV-006"], {"device_id": "INV-005"}),
        ("H09", "subtask_failure", ["WT-001", "WT-002"], {"device_id": "WT-002"}),
        ("H10", "subtask_failure", ["BS-001", "BS-002", "SW-001"], {"device_id": "BS-001"}),
    ]
    for cid, fault_type, batch, extra in faults:
        fault = {"type": fault_type, **extra}
        add(cid, "hard", batch, _GOAL_MEDIUM,
            _expected(batch, tools=BASE_TOOLS, forbidden=["dispatch_drone"],
                      status=["partial", "failed"], derived=derived),
            fault=fault)
    # H11/H12：异常设备无人机复核（审批路径）
    for cid, dev in (("H11", "BS-001"), ("H12", "WT-002")):
        add(cid, "hard", [dev], _GOAL_DRONE,
            _expected([dev], tools=FULL_TOOLS[1:], forbidden=[], orders=True,
                      drone=True, evidence=True, derived=derived))
    # H13/H14：只读诊断（禁写）
    readonly_batches = [
        ("H13", [f"INV-{i:03d}" for i in range(1, 9)]),
        ("H14", [f"WT-{i:03d}" for i in range(1, 5)]),
    ]
    for cid, batch in readonly_batches:
        add(cid, "hard", batch, _GOAL_READONLY,
            _expected(batch, tools=BATCH_TOOLS, forbidden=FORBIDDEN_WRITE, derived=derived))
    # H15-H18：整站/跨类大批次
    hard_batches = [
        ("H15", ["PCS-001", "PCS-002", "PCS-003"]),
        ("H16", ["SW-001", "SW-002", "SW-003", "BS-001", "BS-002"]),
        ("H17", [f"INV-{i:03d}" for i in range(1, 9)]),
        ("H18", ANOMALY_DEVICES),
    ]
    for cid, batch in hard_batches:
        add(cid, "hard", batch, _GOAL_MEDIUM,
            _expected(batch, tools=FULL_TOOLS, forbidden=FORBIDDEN_DRONE,
                      orders=True, evidence=True, derived=derived))
    # H19：跨类无人机复核（审批 + 工单）
    add("H19", "hard", ["BS-001", "SW-002"], _GOAL_DRONE,
        _expected(["BS-001", "SW-002"], tools=FULL_TOOLS, forbidden=[], orders=True,
                  drone=True, evidence=True, derived=derived))
    # H20：正常设备深诊断（要求规程依据，禁写）
    add("H20", "hard", ["BS-002"], "深度诊断：给出运行评价与规程依据，禁止创建工单。",
        _expected(["BS-002"], tools=FULL_TOOLS[1:4], forbidden=FORBIDDEN_WRITE, derived=derived))

    assert len(cases) == 60, len(cases)
    return {
        "version": 1,
        "note": "合成数据智能体评测集：60 条巡检用例（easy/medium/hard=20/20/20，含 10 条故障注入）。",
        "derivation": (
            "expected.anomaly_devices/anomaly_levels 由 evals/derive_expectations.py 的确定性判据"
            "（语料 thresholds 文档代码化）对 fixtures/inspection/telemetry 数值推导；"
            "tests/evals/test_agent_cases.py 逐条复核，禁止引用 fixtures 注释当答案。"
        ),
        "cases": cases,
    }


def main() -> None:
    data = build()
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"written {DATA_FILE} ({len(data['cases'])} cases)")


if __name__ == "__main__":
    main()
