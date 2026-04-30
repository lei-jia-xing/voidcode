from __future__ import annotations

import difflib
import sys
import textwrap
from pathlib import Path

import pytest

from voidcode.hook.config import RuntimeFormatterPresetConfig, RuntimeHooksConfig
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
    assert result.data["count"] == 1
    assert result.data["changes"] == [{"path": "sample.txt", "status": "M"}]
    assert result.content == "M sample.txt"


def test_apply_patch_allows_external_target_file(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "outside-apply-patch"
    outside_dir.mkdir(exist_ok=True)
    target = outside_dir / "sample.txt"
    target.write_text("line-1\nline-2\n", encoding="utf-8")

    patch_text = "\n".join(
        [
            "*** Begin Patch",
            f"*** Update File: {target}",
            "@@",
            "-line-1",
            "+patched-1",
            "*** End Patch",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8").startswith("patched-1")


def test_apply_patch_reports_file_addition_from_unified_diff(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)

    patch_text = "\n".join(
        [
            "diff --git a/new.txt b/new.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/new.txt",
            "@@ -0,0 +1 @@",
            "+hello",
            "",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"
    assert result.data["changes"] == [{"path": "new.txt", "status": "A"}]
    assert result.content == "A new.txt"


def test_apply_patch_treats_unified_diff_marker_literals_as_unified_diff(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "sample.txt"
    target.write_text(
        "before\n*** Begin Patch\n*** End Patch\nold\n",
        encoding="utf-8",
    )
    _commit_all(tmp_path, "baseline")
    old = target.read_text(encoding="utf-8").splitlines(keepends=True)
    new = ["before\n", "*** Begin Patch\n", "*** End Patch\n", "new\n"]
    patch_text = "".join(
        difflib.unified_diff(old, new, fromfile="a/sample.txt", tofile="b/sample.txt")
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "before\n*** Begin Patch\n*** End Patch\nnew\n"
    assert result.content == "M sample.txt"


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
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert (tmp_path / "src/main.py").read_text(encoding="utf-8") == "print('hello')"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# Demo"
    assert result.data["changes"] == [
        {"path": "src/main.py", "status": "A"},
        {"path": "README.md", "status": "A"},
    ]
    assert result.content == "A src/main.py\nA README.md"


def test_apply_patch_rejects_malformed_structured_add_file_line(
    tmp_path: Path,
) -> None:
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: README.md",
            "+# Demo",
            "missing plus prefix",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError, match=r"Add File content lines must start with '\+'"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )

    assert not (tmp_path / "README.md").exists()


def test_apply_patch_rejects_structured_add_file_when_destination_exists(
    tmp_path: Path,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("original\n", encoding="utf-8")
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: README.md",
            "+# Replacement",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError, match="Add File destination already exists"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )

    assert target.read_text(encoding="utf-8") == "original\n"


def test_apply_patch_rejects_structured_add_file_before_partial_writes(
    tmp_path: Path,
) -> None:
    target = tmp_path / "README.md"
    target.write_text("original\n", encoding="utf-8")
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: new.txt",
            "+new file",
            "*** Add File: README.md",
            "+# Replacement",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError, match="Add File destination already exists"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )

    assert not (tmp_path / "new.txt").exists()
    assert target.read_text(encoding="utf-8") == "original\n"


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
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "def greet():\n    print('hello')\n"
    assert not obsolete.exists()
    assert not moved.exists()
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "new name\n"
    assert result.data["changes"] == [
        {"path": "app.py", "status": "M"},
        {"path": "obsolete.txt", "status": "D"},
        {"path": "new.txt", "old_path": "old.txt", "status": "R"},
    ]


def test_apply_patch_repeated_structured_updates_use_staged_content(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("alpha\nbeta\n", encoding="utf-8")
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: app.py",
            "@@",
            "-alpha",
            "+alpha-one",
            "*** Update File: app.py",
            "@@",
            "-beta",
            "+beta-two",
            "*** End Patch",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "alpha-one\nbeta-two\n"


def test_apply_patch_rejects_structured_same_path_move_without_deleting_file(
    tmp_path: Path,
) -> None:
    target = tmp_path / "app.py"
    target.write_text("print('before')\n", encoding="utf-8")
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: ./app.py",
            "*** Move to: app.py",
            "@@",
            "-print('before')",
            "+print('after')",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError, match="Move destination must differ from source"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )

    assert target.read_text(encoding="utf-8") == "print('before')\n"


def test_apply_patch_rejects_structured_move_when_destination_exists(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.txt"
    destination = tmp_path / "new.txt"
    source.write_text("old\n", encoding="utf-8")
    destination.write_text("existing\n", encoding="utf-8")
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: old.txt",
            "*** Move to: new.txt",
            "@@",
            "-old",
            "+moved",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError, match="Move destination already exists"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )

    assert source.read_text(encoding="utf-8") == "old\n"
    assert destination.read_text(encoding="utf-8") == "existing\n"


def test_apply_patch_rejects_structured_move_before_partial_writes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "old.txt"
    destination = tmp_path / "new.txt"
    source.write_text("old\n", encoding="utf-8")
    destination.write_text("existing\n", encoding="utf-8")
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: created.txt",
            "+created",
            "*** Update File: old.txt",
            "*** Move to: new.txt",
            "@@",
            "-old",
            "+moved",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError, match="Move destination already exists"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )

    assert not (tmp_path / "created.txt").exists()
    assert source.read_text(encoding="utf-8") == "old\n"
    assert destination.read_text(encoding="utf-8") == "existing\n"


def test_apply_patch_structured_insert_only_update_uses_matched_context(
    tmp_path: Path,
) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("before\nanchor\nafter\n", encoding="utf-8")

    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Update File: sample.txt",
            "@@ anchor",
            "+inserted",
            "*** End Patch",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "before\nanchor\ninserted\nafter\n"
    assert result.data["changes"] == [{"path": "sample.txt", "status": "M"}]


def test_apply_patch_reports_context_for_corrupt_unified_diff(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    patch_text = "\n".join(
        [
            "diff --git a/one.txt b/one.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/one.txt",
            "@@ -0,0 +2 @@",
            "diff --git a/two.txt b/two.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/two.txt",
            "@@ -0,0 +1 @@",
            "+second",
            "",
        ]
    )

    with pytest.raises(ValueError) as exc_info:
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )

    error = str(exc_info.value)
    assert "corrupt patch" in error
    assert "Patch context near line" in error
    assert "structured *** Begin Patch / *** Add File envelope" in error


def test_apply_patch_runs_formatter_for_structured_changed_files(tmp_path: Path) -> None:
    formatter_script = tmp_path / "formatter.py"
    formatter_script.write_text(
        textwrap.dedent(
            """
            import pathlib
            import sys

            pathlib.Path(sys.argv[-1]).write_text("print( 'formatted' )\\n", encoding="utf-8")
            """
        ),
        encoding="utf-8",
    )
    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: main.py",
            "+print('raw')",
            "*** End Patch",
        ]
    )
    tool = ApplyPatchTool(
        hooks_config=RuntimeHooksConfig(
            formatter_presets={
                "python": RuntimeFormatterPresetConfig(
                    command=(sys.executable, str(formatter_script)),
                    extensions=(".py",),
                )
            }
        )
    )

    result = tool.invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert (tmp_path / "main.py").read_text(encoding="utf-8") == "print( 'formatted' )\n"
    assert result.data["formatters"] == [
        {
            "status": "formatted",
            "language": "python",
            "cwd": str(tmp_path),
            "command": [sys.executable, str(formatter_script), str(tmp_path / "main.py")],
            "attempted_commands": [
                [sys.executable, str(formatter_script), str(tmp_path / "main.py")]
            ],
            "path": "main.py",
        }
    ]


def test_apply_patch_raises_on_invalid_patch(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    (tmp_path / "sample.txt").write_text("line-1\n", encoding="utf-8")

    tool = ApplyPatchTool()
    with pytest.raises(ValueError):
        tool.invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": "not a patch"}),
            workspace=tmp_path,
        )


def test_apply_patch_reports_only_patch_touched_paths_in_dirty_worktree(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "sample.txt"
    untouched_dirty = tmp_path / "dirty.txt"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    untouched_dirty.write_text("before\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")

    untouched_dirty.write_text("after\n", encoding="utf-8")

    old = target.read_text(encoding="utf-8").splitlines(keepends=True)
    new = ["patched-1\n", "line-2\n"]
    patch_text = "".join(
        difflib.unified_diff(old, new, fromfile="a/sample.txt", tofile="b/sample.txt")
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["count"] == 1
    assert result.data["changes"] == [{"path": "sample.txt", "status": "M"}]
    assert result.content == "M sample.txt"


def test_apply_patch_reports_pure_rename_from_patch_metadata(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    old_path = tmp_path / "old.txt"
    old_path.write_text("hello\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")

    patch_text = "\n".join(
        [
            "diff --git a/old.txt b/new.txt",
            "similarity index 100%",
            "rename from old.txt",
            "rename to new.txt",
            "",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert not old_path.exists()
    assert (tmp_path / "new.txt").read_text(encoding="utf-8") == "hello\n"
    assert result.data["count"] == 1
    assert result.data["changes"] == [{"path": "new.txt", "old_path": "old.txt", "status": "R"}]
    assert result.content == "M old.txt -> new.txt"


def test_apply_patch_reports_mode_only_change_from_patch_metadata(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "script.sh"
    target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")

    patch_text = "\n".join(
        [
            "diff --git a/script.sh b/script.sh",
            "old mode 100644",
            "new mode 100755",
            "",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["count"] == 1
    assert result.data["changes"] == [{"path": "script.sh", "status": "M"}]
    assert result.content == "M script.sh"


def test_apply_patch_reports_mode_only_change_for_path_with_spaces(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "space name.sh"
    target.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")

    patch_text = "\n".join(
        [
            "diff --git a/space name.sh b/space name.sh",
            "old mode 100644",
            "new mode 100755",
            "",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert result.data["count"] == 1
    assert result.data["changes"] == [{"path": "space name.sh", "status": "M"}]
    assert result.content == "M space name.sh"


def test_apply_patch_ignores_broken_unidiff_paths_from_quoted_diff_header(tmp_path: Path) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "space name.txt"
    target.write_text("old\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")

    patch_text = "\n".join(
        [
            'diff --git "a/space name.txt" "b/space name.txt"',
            "--- a/space name.txt",
            "+++ b/space name.txt",
            "@@ -1 +1 @@",
            "-old",
            "+new",
            "",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "new\n"
    assert result.data["count"] == 1
    assert result.data["changes"] == [{"path": "space name.txt", "status": "M"}]
    assert result.content == "M space name.txt"


def test_apply_patch_does_not_treat_mixed_mode_and_content_patch_as_mode_only(
    tmp_path: Path,
) -> None:
    _init_git_repo(tmp_path)
    target = tmp_path / "file.txt"
    target.write_text("line-1\n", encoding="utf-8")
    _commit_all(tmp_path, "baseline")

    patch_text = "\n".join(
        [
            "diff --git a/file.txt b/file.txt",
            "old mode 100644",
            "new mode 100755",
            "--- a/file.txt",
            "+++ b/file.txt",
            "@@ -1 +1 @@",
            "-line-1",
            "+line-2",
            "",
        ]
    )

    result = ApplyPatchTool().invoke(
        ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
        workspace=tmp_path,
    )

    assert result.status == "ok"
    assert target.read_text(encoding="utf-8") == "line-2\n"
    assert result.data["count"] == 1
    assert result.data["changes"] == [{"path": "file.txt", "status": "M"}]
    assert result.content == "M file.txt"


def test_apply_patch_raises_helpful_error_when_git_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def _raise_file_not_found(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise FileNotFoundError()

    monkeypatch.setattr(
        "voidcode.tools.apply_patch.subprocess.run",
        _raise_file_not_found,
    )

    patch_text = "\n".join(
        [
            "diff --git a/file.txt b/file.txt",
            "old mode 100644",
            "new mode 100755",
            "",
        ]
    )

    with pytest.raises(ValueError, match="git is required for apply_patch"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )


def test_apply_patch_rejects_structured_symlink_escape(tmp_path: Path) -> None:
    outside_dir = tmp_path.parent / "outside_patch_escape"
    outside_dir.mkdir(exist_ok=True)
    link_dir = tmp_path / "linkdir"
    try:
        link_dir.symlink_to(outside_dir, target_is_directory=True)
    except OSError:
        pytest.skip("symlink is not available on this platform")

    patch_text = "\n".join(
        [
            "*** Begin Patch",
            "*** Add File: linkdir/escape.txt",
            "+escaped",
            "*** End Patch",
        ]
    )

    with pytest.raises(ValueError, match="inside the workspace"):
        ApplyPatchTool().invoke(
            ToolCall(tool_name="apply_patch", arguments={"patch": patch_text}),
            workspace=tmp_path,
        )
