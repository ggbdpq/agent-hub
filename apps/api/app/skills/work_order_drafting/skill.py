"""work-order-drafting Skill（B10）：工单起草能力封装。"""

from pathlib import Path

from app.skills.base import Skill, load_skill_file


def load() -> Skill:
    return load_skill_file(Path(__file__).parent / "SKILL.md")
