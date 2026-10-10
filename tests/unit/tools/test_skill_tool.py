from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.skills.models import SkillMetadata
from voidcode.tools.contracts import ToolCall
from voidcode.tools.skill import SkillResultBody, SkillTool


def test_skill_tool_rejects_missing_name(tmp_path: Path) -> None:
    tool = SkillTool()

    with pytest.raises(ValueError):
        tool.invoke(ToolCall(tool_name="skill", arguments={}), context=ToolContext(workspace=tmp_path))


def test_skill_tool_returns_exact_body_and_discoverable_path_metadata(tmp_path: Path) -> None:
    content = "```python\n    value = 42\n```\n"
    skill = SkillMetadata(
        name="code",
        description="Python notes",
        content=content,
        directory=tmp_path,
        entry_path=tmp_path / "SKILL.md",
    )
    context = ToolContext(workspace=tmp_path, resolve_skill=lambda name: skill)
    result = SkillTool().invoke(ToolCall(tool_name="skill", arguments={"name": "code"}), context=context)

    assert isinstance(result.body, SkillResultBody)
    assert result.body.content == content
    assert result.output.text.count(content) == 1
    assert "sourcePath:" in result.output.text
    assert "baseDirectory:" in result.output.text
    assert result.body.as_payload()["skill"] == {
        "name": "code",
        "description": "Python notes",
        "sourcePath": str(skill.entry_path),
        "baseDirectory": str(skill.directory),
        "content": content,
    }
    assert tuple(SkillTool.definition.input_schema["required"]) == ("name",)
