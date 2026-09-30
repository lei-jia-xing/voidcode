from __future__ import annotations

from pathlib import Path

import pytest

from voidcode.tools.apply_patch import ApplyPatchTool
from voidcode.tools.contracts import ToolCall
from voidcode.tools.web_fetch import WebFetchTool


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
