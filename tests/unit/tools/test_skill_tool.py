from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import ToolCall
from voidcode.tools.skill import SkillTool


def test_skill_tool_rejects_missing_name(tmp_path: Path) -> None:
    tool = SkillTool()

    with pytest.raises(ValueError):
        tool.invoke(ToolCall(tool_name="skill", arguments={}), context=ToolContext(workspace=tmp_path))
