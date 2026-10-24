"""统一报告生成（C11）：Markdown + JSON 双产出，结构全部套件复用。

必要段：``method``（方法）、``sample_size``（样本量）、``variance``（方差/波动）、
``known_biases``（已知偏差）、``disclaimer``（口径声明）——缺失即抛错，
保证任何报告都不可能漏掉"仅验证流程，不代表模型效果"类口径标注。
渲染为纯函数：固定 generated_at 下逐字节可复现（快照测试锁定）。
"""

import json
from pathlib import Path
from typing import Any


def std(values: list[float]) -> float:
    """总体标准差；空集/单点为 0。"""
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return (sum((v - mean) ** 2 for v in values) / len(values)) ** 0.5


REQUIRED_SECTIONS = ("method", "sample_size", "variance", "known_biases", "disclaimer")


def finalize(payload: dict, *, generated_at: str | None = None) -> dict:
    """补齐报告元数据并校验必要段；返回新 dict（不改入参）。generated_at 缺省取当前 UTC。"""
    from datetime import UTC, datetime

    out = dict(payload)
    out["generated_at"] = generated_at or datetime.now(UTC).isoformat()
    missing = [k for k in REQUIRED_SECTIONS if not out.get(k)]
    if missing:
        raise ValueError(f"报告缺少必要段（离线口径声明不可省略）：{missing}")
    return out


def _fmt_variance(variance: dict[str, Any]) -> list[str]:
    lines = []
    for key, value in variance.items():
        lines.append(f"- {key} = {value}" if isinstance(value, str) else f"- {key} = {value:g}")
    return lines


def render_markdown(payload: dict) -> str:
    lines: list[str] = [f"# {payload['title']}", ""]
    lines.append(f"- 生成时间：{payload['generated_at']}")
    for label, value in payload.get("meta", []):
        lines.append(f"- {label}：{value}")
    sample = payload["sample_size"]
    lines.append("- 样本量：" + "，".join(f"{k}={v}" for k, v in sample.items()))
    lines += ["", f"> **{payload['disclaimer']}**", "", "## 方法", ""]
    lines += [f"- {item}" for item in payload["method"]]
    for table in payload.get("tables", []):
        lines += ["", f"## {table['caption']}", ""]
        lines.append("| " + " | ".join(table["header"]) + " |")
        lines.append("|" + "|".join("---" for _ in table["header"]) + "|")
        for row in table["rows"]:
            lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    for section in payload.get("sections", []):
        lines += ["", f"## {section['caption']}", ""]
        lines += [f"- {item}" for item in section["bullets"]]
    lines += ["", "## 方差与波动", ""]
    lines += _fmt_variance(payload["variance"]) or ["- （本报告无波动统计）"]
    lines += ["", "## 已知偏差", ""]
    lines += [f"- {item}" for item in payload["known_biases"]]
    return "\n".join(lines) + "\n"


def write_report(payload: dict, out_dir: Path, stem: str) -> tuple[Path, Path]:
    """Markdown + JSON 双产出，返回两个文件路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"{stem}.md"
    json_path = out_dir / f"{stem}.json"
    md_path.write_text(render_markdown(payload), encoding="utf-8")
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return md_path, json_path
