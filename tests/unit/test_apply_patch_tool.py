from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from voidcode.tools import ApplyPatchTool, ToolCall


def _init_git_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=str(path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=str(path), check=True)


def _commit_all(path: Path, message: str) -> None:
    import subprocess

    subprocess.run(["git", "add", "."], cwd=str(path), check=True, stdout=subprocess.PIPE)
    subprocess.run(
        ["git", "commit", "-m", message],
        cwd=str(path),
        check=True,
        capture_output=True,
        text=True,
    )


def test_apply_patch_updates_file_with_valid_patch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")

    old = target.read_text(encoding="utf-8").splitlines(keepends=True)
    new = ["patched-1\n", "line-2\n"]
    patch_text = "".join(
        difflib.unified_diff(old, new, fromfile="a/sample.txt", tofile="b/sample.txt")
    )

    tool = ApplyPatchTool()
    result = tool.invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert target.read_text(encoding="utf-8").startswith("patched-1")
    assert result.status == "ok"
    assert result.data["count"] >= 1


def test_apply_patch_raises_on_invalid_patch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "sample.txt").write_text("line-1\n", encoding="utf-8")

    tool = ApplyPatchTool()
    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": "not a patch"}),
            workspace=tmp_path,
        )
