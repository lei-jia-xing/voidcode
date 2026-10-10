from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.contracts import ToolCall
from voidcode.tools.invoke_tool import InvokeTool


def test_invoke_tool_rejects_unknown_wrapper_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Validation error"):
        InvokeTool().invoke(
            ToolCall(tool_name="invoke_tool", arguments={"name": "read", "unexpected": True}),
            context=ToolContext(workspace=tmp_path),
        )
