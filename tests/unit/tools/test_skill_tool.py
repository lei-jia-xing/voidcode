from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.skills.models import SkillMetadata
from voidcode.tools import SkillTool, ToolCall


def test_skill_tool_returns_skill_body_and_metadata(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    entry = skill_dir / "SKILL.md"
    entry.write_text("# Demo\nUse it.\n", encoding="utf-8")
    skill = SkillMetadata(
        name="demo",
        description="Demo skill",
        directory=skill_dir,
        entry_path=entry,
        content="# Demo\nUse it.",
    )
    tool = SkillTool(list_skills=lambda: (skill,), resolve_skill=lambda name: skill)

    result = tool.invoke(
        ToolCall(tool_name="skill", arguments={"name": "demo"}), workspace=tmp_path
    )

    assert result.status == "ok"
    assert result.content is not None
    assert "## Skill: demo" in result.content
    assert result.data["skill"] == {
        "name": "demo",
        "description": "Demo skill",
        "source_path": str(entry),
        "directory": str(skill_dir),
        "content": "# Demo\nUse it.",
    }


def test_skill_tool_definition_includes_nested_skill_locations(tmp_path: Path) -> None:
    skill_dir = tmp_path / ".voidcode" / "skills" / "python" / "demo"
    skill_dir.mkdir(parents=True)
    entry = skill_dir / "SKILL.md"
    entry.write_text("# Demo\nUse it.\n", encoding="utf-8")
    skill = SkillMetadata(
        name="demo",
        description="Demo skill",
        directory=skill_dir,
        entry_path=entry,
        content="# Demo\nUse it.",
    )

    tool = SkillTool(list_skills=lambda: (skill,), resolve_skill=lambda name: skill)

    assert entry.as_uri() in tool.definition.description


def test_skill_tool_rejects_missing_name(tmp_path: Path) -> None:
    tool = SkillTool(
        list_skills=lambda: (), resolve_skill=lambda name: (_ for _ in ()).throw(ValueError(name))
    )

    with pytest.raises(ValueError, match="non-empty string name"):
        tool.invoke(ToolCall(tool_name="skill", arguments={}), workspace=tmp_path)
