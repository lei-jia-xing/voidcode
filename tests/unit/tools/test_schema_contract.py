"""Contract checks for builtin agent-facing tool schemas."""

from __future__ import annotations

import pytest

from voidcode.tools.apply_patch import ApplyPatchTool
from voidcode.tools.ast_grep import AstGrepTool
from voidcode.tools.edit import EditTool
from voidcode.tools.grep import GrepTool
from voidcode.tools.multi_edit import MultiEditTool
from voidcode.tools.read_file import ReadFileTool
from voidcode.tools.shell_exec import ShellExecTool
from voidcode.tools.web_fetch import WebFetchTool
from voidcode.tools.web_search import WebSearchTool
from voidcode.tools.write_file import WriteFileTool


@pytest.mark.parametrize(
    ("tool", "required"),
    [
        (ReadFileTool, ["path"]),
        (WriteFileTool, ["path", "content"]),
        (EditTool, ["path", "oldString", "newString"]),
        (MultiEditTool, ["path", "edits"]),
        (GrepTool, ["pattern", "path"]),
        (ApplyPatchTool, ["patch"]),
        (AstGrepTool, ["mode", "pattern", "path"]),
        (ShellExecTool, ["command"]),
        (WebSearchTool, ["query"]),
        (WebFetchTool, ["url"]),
    ],
)
def test_builtin_schema_declares_required_fields(tool: type[object], required: list[str]) -> None:
    schema = tool.definition.input_schema
    assert schema["required"] == required
    for field in required:
        assert isinstance(schema[field], dict)
        assert str(schema[field].get("description", "")).strip()


def test_read_file_schema_uses_only_path() -> None:
    schema = ReadFileTool.definition.input_schema
    assert "path" in schema
    assert "path" not in schema
    assert ReadFileTool.definition.path_argument_keys == ("path",)


def test_ast_grep_schema_constrains_mode() -> None:
    modes = AstGrepTool.definition.input_schema["mode"]
    assert modes["enum"] == ["search", "preview", "replace"]
