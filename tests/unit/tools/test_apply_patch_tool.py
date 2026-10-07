from __future__ import annotations

import difflib
import hashlib
import subprocess
from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.tools.apply_patch import ApplyPatchResultBody, ApplyPatchTool
from voidcode.tools.contracts import TextOutput, ToolCall


def _content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _init_git_repo(path: Path) -> None:

    subprocess.run(["git", "init"], cwd=str(path), check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True)
    subprocess.run(["git", "config", "user.name", "tester"], cwd=str(path), check=True)


def _commit_all(path: Path, message: str) -> None:

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
    patch_text = "".join(difflib.unified_diff(old, new, fromfile="a/sample.txt", tofile="b/sample.txt"))

    tool = ApplyPatchTool()
    result = tool.invoke(
        ToolCall(
            tool_name="apply_patch",
            arguments={"patch": patch_text, "expectedHashes": {"sample.txt": _content_hash(target)}},
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert target.read_text(encoding="utf-8").startswith("patched-1")
    assert result.status == "ok"
    assert isinstance(result.body, ApplyPatchResultBody)
    assert result.body.count == 1
    assert [change.as_payload() for change in result.body.changes] == [{"path": "sample.txt", "status": "M"}]
    assert isinstance(result.output, TextOutput)
    assert result.output.text == "M sample.txt"


def test_apply_patch_rejects_unified_diff_update_without_prior_read_side_effects(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")
    old = target.read_text(encoding="utf-8").splitlines(keepends=True)
    new = ["patched-1\n", "line-2\n"]
    patch_text = "".join(difflib.unified_diff(old, new, fromfile="a/sample.txt", tofile="b/sample.txt"))

    with pytest.raises(ValueError, match="requires reading the current file before modifying it"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            context=ToolContext(workspace=tmp_path, session_id="test"),
        )

    assert target.read_text(encoding="utf-8") == "line-1\nline-2\n"


def test_apply_patch_accepts_structured_add_file_patch(tmp_path: Path) -> None:
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: src/main.py",
            "+print('hello')",
            "*** Add File: README.md",
            "+# Demo",
            "*** End Patch",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    assert (tmp_path / "src/main.py").read_text(encoding="utf-8") == "print('hello')"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Demo"
    assert isinstance(result.body, ApplyPatchResultBody)
    assert [change.as_payload() for change in result.body.changes] == [
        {"path": "src/main.py", "status": "A"},
        {"path": "README.md", "status": "A"},
    ]
    assert isinstance(result.output, TextOutput)
    assert result.output.text == "A src/main.py\nA README.md"


def test_apply_patch_accepts_structured_update_delete_and_move(tmp_path: Path) -> None:
    target = tmp_path / "app.py"
    target.write_text("def greet():\n    print('hi')\n", encoding="utf-8")
    obsolete = tmp_path / "obsolete.txt"
    obsolete.write_text("remove me\n", encoding="utf-8")
    moved = tmp_path / "old.txt"
    moved.write_text("old name\n", encoding="utf-8")

    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: app.py",
            "@@ def greet():",
            "-    print('hi')",
            "+    print('hello')",
            "*** Delete File: obsolete.txt",
            "*** Update File: old.txt",
            "*** Move to: new.txt",
            "@@",
            "-old name",
            "+new name",
            "*** End Patch",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(
            tool_name="apply_patch",
            arguments={
                "patch": patch_text,
                "expectedHashes": {
                    "app.py": _content_hash(target),
                    "obsolete.txt": _content_hash(obsolete),
                    "old.txt": _content_hash(moved),
                },
            },
        ),
        context=ToolContext(workspace=tmp_path),
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "def greet():\n    print('hello')\n"
    assert not obsolete.exists()
    assert not moved.exists()
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new name\n"
    assert isinstance(result.body, ApplyPatchResultBody)
    assert [change.as_payload() for change in result.body.changes] == [
        {"path": "app.py", "status": "M"},
        {"path": "obsolete.txt", "status": "D"},
        {"path": "new.txt", "old_path": "old.txt", "status": "R"},
    ]


def test_apply_patch_raises_on_invalid_patch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "sample.txt").write_text("line-1\n", encoding="utf-8")

    tool = ApplyPatchTool()
    with pytest.raises(ValueError, match="malformed patch text"):
        tool.invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": "not a patch"}),
            context=ToolContext(workspace=tmp_path),
        )
