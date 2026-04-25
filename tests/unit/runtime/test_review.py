from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from voidcode.runtime.contracts import GitStatusSnapshot, ReviewChangedFile, ReviewTreeNode
from voidcode.runtime.review import WorkspaceReviewService


def test_review_snapshot_keeps_out_of_root_symlinked_file_paths_relative(tmp_path: Path) -> None:
    outside_file = tmp_path.parent / "outside-file.txt"
    outside_file.write_text("outside\n", encoding="utf-8")
    symlink_path = tmp_path / "external-file.txt"
    symlink_path.symlink_to(outside_file)

    snapshot = WorkspaceReviewService(workspace=tmp_path).snapshot(
        git=GitStatusSnapshot(state="not_git_repo")
    )

    assert snapshot.root == str(tmp_path.resolve())
    assert snapshot.tree == (
        ReviewTreeNode(
            path="external-file.txt",
            name="external-file.txt",
            kind="file",
            changed=False,
        ),
    )


def test_review_snapshot_does_not_descend_into_out_of_root_symlinked_directory(
    tmp_path: Path,
) -> None:
    outside_dir = tmp_path.parent / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "nested.txt").write_text("nested\n", encoding="utf-8")
    symlink_path = tmp_path / "external-dir"
    symlink_path.symlink_to(outside_dir, target_is_directory=True)

    snapshot = WorkspaceReviewService(workspace=tmp_path).snapshot(
        git=GitStatusSnapshot(state="not_git_repo")
    )

    assert len(snapshot.tree) == 1
    node = snapshot.tree[0]
    assert node.path == "external-dir"
    assert node.name == "external-dir"
    assert node.kind == "file"
    assert node.changed is False
    assert node.children == ()


def test_review_snapshot_excludes_generated_and_internal_directories(tmp_path: Path) -> None:
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "python").write_text("python\n", encoding="utf-8")
    (tmp_path / ".sisyphus" / "plans").mkdir(parents=True)
    (tmp_path / ".sisyphus" / "plans" / "plan.md").write_text("plan\n", encoding="utf-8")
    (tmp_path / ".opencode" / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / ".opencode" / "node_modules" / "pkg" / "index.js").write_text(
        "export {};\n", encoding="utf-8"
    )
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    snapshot = WorkspaceReviewService(workspace=tmp_path).snapshot(
        git=GitStatusSnapshot(state="not_git_repo")
    )

    assert snapshot.tree == (
        ReviewTreeNode(
            path=".github",
            name=".github",
            kind="directory",
            changed=False,
            children=(
                ReviewTreeNode(
                    path=".github/workflows",
                    name="workflows",
                    kind="directory",
                    changed=False,
                    children=(
                        ReviewTreeNode(
                            path=".github/workflows/ci.yml",
                            name="ci.yml",
                            kind="file",
                            changed=False,
                        ),
                    ),
                ),
            ),
        ),
        ReviewTreeNode(
            path=".opencode",
            name=".opencode",
            kind="directory",
            changed=False,
            children=(),
        ),
        ReviewTreeNode(
            path="src",
            name="src",
            kind="directory",
            changed=False,
            children=(
                ReviewTreeNode(
                    path="src/app.py",
                    name="app.py",
                    kind="file",
                    changed=False,
                ),
            ),
        ),
    )


def test_review_snapshot_keeps_changed_files_when_tree_excludes_internal_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / ".venv" / "bin" / "activate").write_text("activate\n", encoding="utf-8")

    changed_files = (
        ReviewChangedFile(path=".venv/bin/activate", change_type="modified"),
        ReviewChangedFile(path="src/app.py", change_type="modified"),
    )

    service = WorkspaceReviewService(workspace=tmp_path)
    with patch.object(service, "_changed_files", return_value=changed_files):
        result = service.snapshot(git=GitStatusSnapshot(state="git_ready", root=str(tmp_path)))

    assert result.changed_files == changed_files
    assert result.tree == (
        ReviewTreeNode(
            path="src",
            name="src",
            kind="directory",
            changed=True,
            children=(
                ReviewTreeNode(
                    path="src/app.py",
                    name="app.py",
                    kind="file",
                    changed=True,
                ),
            ),
        ),
    )


def test_review_diff_reads_untracked_file_from_nested_workspace_under_git_root(
    tmp_path: Path,
) -> None:
    git_root = tmp_path / "repo"
    workspace = git_root / "subdir"
    workspace.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=git_root, check=True, capture_output=True)
    (workspace / "new.txt").write_text("hello\nworld\n", encoding="utf-8")

    service = WorkspaceReviewService(workspace=workspace)

    result = service.diff(
        path="subdir/new.txt",
        git=GitStatusSnapshot(state="git_ready", root=str(git_root)),
    )

    assert result.state == "changed"
    assert result.path == "subdir/new.txt"
    assert result.diff == "\n".join(
        (
            "diff --git a/subdir/new.txt b/subdir/new.txt",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/subdir/new.txt",
            "@@ -0,0 +1,2 @@",
            "+hello",
            "+world",
        )
    )


def test_review_changed_files_decodes_quoted_untracked_path(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "a b.txt").write_text("hello\n", encoding="utf-8")

    result = WorkspaceReviewService(workspace=tmp_path).snapshot(
        git=GitStatusSnapshot(state="git_ready", root=str(tmp_path))
    )

    assert result.changed_files == (
        ReviewChangedFile(path="a b.txt", change_type="untracked", old_path=None),
    )


def test_review_changed_files_decodes_quoted_rename_paths(tmp_path: Path) -> None:
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    old_path = tmp_path / "old name.txt"
    old_path.write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "old name.txt"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "-m",
            "init",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    old_path.rename(tmp_path / "new name.txt")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, capture_output=True)

    result = WorkspaceReviewService(workspace=tmp_path).snapshot(
        git=GitStatusSnapshot(state="git_ready", root=str(tmp_path))
    )

    assert result.changed_files == (
        ReviewChangedFile(
            path="new name.txt",
            change_type="renamed",
            old_path="old name.txt",
        ),
    )
