"""B10：Skill 基础设施——解析 SKILL.md（frontmatter + 系统提示片段）。

SKILL.md 格式（无 YAML 依赖，逐行解析）：
```
---
name: telemetry-triage
description: ...
allowed_tools: get_telemetry, detect_anomaly
output_schema: app.tools.anomaly.AnomalyResult
---
（正文 = 系统提示片段，原样注入 Worker 系统提示）
```
"""

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SKILLS_DIR = Path(__file__).resolve().parent
_FRONTMATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    """一个能力封装：声明工具白名单 + 输出 Schema + 系统提示片段。"""

    name: str
    description: str
    allowed_tools: tuple[str, ...]
    output_schema: str  # 点分路径（评测期解析，加载期不 import——避免循环依赖）
    system_prompt: str
    path: Path


def parse_skill_file(path: Path, text: str | None = None) -> Skill:
    """解析 SKILL.md：frontmatter 键值 + 正文片段。缺失必填键显式报错。"""
    text = text if text is not None else path.read_text(encoding="utf-8")
    match = _FRONTMATTER.match(text.strip())
    if not match:
        raise ValueError(f"{path}: 缺少 frontmatter（--- ... ---）")
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip()
    for required in ("name", "allowed_tools", "output_schema"):
        if not meta.get(required):
            raise ValueError(f"{path}: frontmatter 缺少必填键 {required}")
    return Skill(
        name=meta["name"],
        description=meta.get("description", ""),
        allowed_tools=tuple(t.strip() for t in meta["allowed_tools"].split(",") if t.strip()),
        output_schema=meta["output_schema"],
        system_prompt=match.group(2).strip(),
        path=path,
    )


def load_skill_file(path: Path) -> Skill:
    return parse_skill_file(path)


def available_skills() -> dict[str, Skill]:
    """扫描 SKILLS_DIR 下的 */SKILL.md（缓存不必要——目录固定且小）。"""
    skills: dict[str, Skill] = {}
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        skill = parse_skill_file(path)
        skills[skill.name] = skill
    return skills


def get_skill(name: str) -> Skill:
    skills = available_skills()
    if name not in skills:
        raise KeyError(f"未知 Skill: {name}（可用: {sorted(skills)}）")
    return skills[name]


def resolve_allowed_tools(skills: list[Skill] | None) -> set[str] | None:
    """Skill 白名单并集；无 Skill 返回 None（不收窄）。"""
    if not skills:
        return None
    allowed: set[str] = set()
    for skill in skills:
        allowed.update(skill.allowed_tools)
    return allowed


def skill_prompt_fragments(skills: list[Skill] | None) -> str:
    """拼接系统提示片段；无 Skill 返回空串。"""
    if not skills:
        return ""
    return "\n\n".join(skill.system_prompt for skill in skills)


def resolve_output_schema(dotted: str) -> Any:
    """按点分路径惰性解析输出 Schema 类（评测期用）。"""
    module_path, _, class_name = dotted.rpartition(".")
    import importlib

    module = importlib.import_module(module_path)
    return getattr(module, class_name)
