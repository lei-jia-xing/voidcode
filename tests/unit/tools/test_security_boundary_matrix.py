from __future__ import annotations

import sys
from pathlib import Path
from typing import Protocol, cast

import pytest

from voidcode.tools import (
    ApplyPatchTool,
    EditTool,
    ReadFileTool,
    ShellExecTool,
    ToolCall,
    ToolResult,
    WebFetchTool,
    WriteFileTool,
)


class _InvokableTool(Protocol):
    def invoke(self, call: ToolCall, *, workspace: Path): ...


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        (ReadFileTool(), {"filePath": "link.txt"}),
        (WriteFileTool(), {"path": "linkdir/out.txt", "content": "x"}),
        (EditTool(), {"path": "link.txt", "oldString": "a", "newString": "b"}),
    ],
)
def test_workspace_symlink_escape_resolves_to_external_path(
    tmp_path: Path,
    tool: _InvokableTool,
    arguments: dict[str, object],
) -> None:
    outside_file = tmp_path.parent / "matrix-outside.txt"
    outside_file.write_text("a", encoding="utf-8")
    link_file = tmp_path / "link.txt"
    link_dir = tmp_path / "linkdir"
    try:
        link_file.symlink_to(outside_file)
        link_dir.symlink_to(tmp_path.parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    result = cast(
        ToolResult,
        tool.invoke(
            ToolCall(tool_name="matrix", arguments=arguments),
            workspace=tmp_path,
        ),
    )
    assert result.status == "ok"
    assert isinstance(result.data.get("path"), str)


def test_apply_patch_symlink_escape_is_rejected_by_tool(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "matrix-outside-dir"
    outside_dir.mkdir(exist_ok=True)
    link_dir = tmp_path / "linkdir"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: linkdir/escaped.txt",
            "+blocked",
            "*** End Patch",
        ]
    )
    with pytest.raises(ValueError, match="inside the workspace"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )
    assert (outside_dir / "escaped.txt").exists() is False


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/",
        "https://metadata.google.internal/computeMetadata/v1",
        "http://[::ffff:127.0.0.1]/",
        "https://user:pass@example.com/",
    ],
)
def test_web_fetch_security_boundary_blocks_dangerous_targets(url: str) -> None:
    with pytest.raises(ValueError):
        WebFetchTool().invoke(
            ToolCall(tool_name="web_fetch", arguments={"url": url, "format": "text"}),
            workspace=Path("/tmp"),
        )


def test_shell_exec_emits_consistent_security_metadata(tmp_path: Path) -> None:
    result = ShellExecTool().invoke(
        ToolCall(
            tool_name="shell_exec",
            arguments={"command": f'"{sys.executable}" -c "print(1)"'},
        ),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["cwd"] == str(tmp_path.resolve())
    assert result.data["timeout"] == 120
    assert result.data["truncated"] is False
    assert result.data["stdout_truncated"] is False
    assert result.data["stderr_truncated"] is False
    assert isinstance(result.data["output_char_count"], int)
