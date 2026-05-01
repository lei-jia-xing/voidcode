from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

from .contracts import (
    GitStatusSnapshot,
    ReviewChangedFile,
    ReviewFileDiff,
    ReviewTreeNode,
    WorkspaceReviewSnapshot,
)

_VCS_TREE_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn"})

type ReviewChangeType = Literal[
    "added",
    "modified",
    "deleted",
    "renamed",
    "untracked",
    "copied",
    "type_changed",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: str
    stderr: str


class WorkspaceReviewService:
    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace.resolve()
        self._tree_ignore_patterns: dict[Path, tuple[str, ...]] = {}

    def snapshot(self, *, git: GitStatusSnapshot) -> WorkspaceReviewSnapshot:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        changed_files = self._changed_files(root) if git.state == "git_ready" else ()
        changed_paths = {item.path for item in changed_files}
        if git.state == "git_ready":
            tree_root = root
        else:
            tree_root = self._workspace
        return WorkspaceReviewSnapshot(
            root=str(tree_root),
            git=GitStatusSnapshot(
                state=git.state,
                root=str(root) if git.root is not None else git.root,
                error=git.error,
            ),
            changed_files=changed_files,
            tree=self._tree(tree_root, tree_root=tree_root, changed_paths=changed_paths),
        )

    def diff(self, *, path: str, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        normalized_path = self._normalize_relative_path(path)
        if git.state != "git_ready":
            return ReviewFileDiff(
                root=str(root),
                path=normalized_path,
                state="not_git_repo",
                diff=None,
            )
        command = self._run_git(
            root,
            "diff",
            "--no-ext-diff",
            "--binary",
            "--",
            normalized_path,
        )
        diff_output = command.stdout
        if not diff_output:
            cached = self._run_git(
                root,
                "diff",
                "--no-ext-diff",
                "--binary",
                "--cached",
                "--",
                normalized_path,
            )
            diff_output = cached.stdout
        if not diff_output:
            untracked_path = root / normalized_path
            if untracked_path.exists() and self._is_untracked(root, normalized_path):
                diff_output = self._untracked_diff(normalized_path, root=root)
        return ReviewFileDiff(
            root=str(root),
            path=normalized_path,
            state="changed" if diff_output else "clean",
            diff=diff_output or None,
        )

    def _changed_files(self, root: Path) -> tuple[ReviewChangedFile, ...]:
        tracked = self._run_git(root, "status", "--short", "--untracked-files=all")
        if tracked.returncode != 0:
            return ()
        changed_files: list[ReviewChangedFile] = []
        for line in tracked.stdout.splitlines():
            if not line.strip():
                continue
            entry = self._parse_status_line(line)
            if entry is not None:
                changed_files.append(entry)
        changed_files.sort(key=lambda item: item.path)
        return tuple(changed_files)

    def _parse_status_line(self, line: str) -> ReviewChangedFile | None:
        status = line[:2]
        payload = line[3:] if len(line) > 3 else ""
        if not payload:
            return None
        if "->" in payload:
            old_path, new_path = [self._decode_status_path(part) for part in payload.split("->", 1)]
        else:
            old_path = None
            new_path = self._decode_status_path(payload)
        change_code = status.replace(" ", "") or "??"
        change_type = self._map_change_type(change_code)
        return ReviewChangedFile(path=new_path, change_type=change_type, old_path=old_path)

    @staticmethod
    def _decode_status_path(path: str) -> str:
        stripped = path.strip()
        if len(stripped) >= 2 and stripped[0] == '"' and stripped[-1] == '"':
            parsed = shlex.split(stripped, posix=True)
            if parsed:
                return parsed[0]
        return stripped

    @staticmethod
    def _map_change_type(code: str) -> ReviewChangeType:
        normalized = code.upper()
        if normalized == "??":
            return "untracked"
        if "R" in normalized:
            return "renamed"
        if "C" in normalized:
            return "copied"
        if "A" in normalized:
            return "added"
        if "D" in normalized:
            return "deleted"
        if "T" in normalized:
            return "type_changed"
        if "M" in normalized or "U" in normalized:
            return "modified"
        return "unknown"

    def _tree(
        self,
        root: Path,
        *,
        tree_root: Path,
        changed_paths: set[str],
    ) -> tuple[ReviewTreeNode, ...]:
        children: list[ReviewTreeNode] = []
        for entry in sorted(
            root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
        ):
            if self._should_exclude_from_tree(entry, tree_root):
                continue
            if entry.is_dir() and self._should_descend_into(entry, tree_root):
                descendants = self._tree(entry, tree_root=tree_root, changed_paths=changed_paths)
                is_changed = any(child.changed for child in descendants)
                children.append(
                    ReviewTreeNode(
                        path=self._relative_to_root(entry, tree_root),
                        name=entry.name,
                        kind="directory",
                        changed=is_changed,
                        children=descendants,
                    )
                )
            else:
                relative_path = self._relative_to_root(entry, tree_root)
                children.append(
                    ReviewTreeNode(
                        path=relative_path,
                        name=entry.name,
                        kind="file",
                        changed=relative_path in changed_paths,
                    )
                )
        return tuple(children)

    def _relative_to_root(self, path: Path, root: Path) -> str:
        return path.relative_to(root).as_posix()

    def _should_exclude_from_tree(self, path: Path, tree_root: Path) -> bool:
        if path.is_dir() and path.name in _VCS_TREE_DIRECTORY_NAMES:
            return True
        relative_path = self._relative_to_root(path, tree_root)
        return self._is_ignored_tree_path(
            tree_root,
            relative_path,
            is_directory=path.is_dir(),
        )

    def _is_ignored_tree_path(
        self,
        tree_root: Path,
        relative_path: str,
        *,
        is_directory: bool,
    ) -> bool:
        git_check = self._run_git(
            tree_root,
            "check-ignore",
            "--quiet",
            "--no-index",
            "--",
            relative_path,
        )
        if git_check.returncode == 0:
            return True
        if git_check.returncode == 1:
            return False
        return _matches_gitignore_path(
            relative_path,
            self._ignore_patterns_for_tree_root(tree_root),
            is_directory=is_directory,
        )

    def _ignore_patterns_for_tree_root(self, tree_root: Path) -> tuple[str, ...]:
        cached = self._tree_ignore_patterns.get(tree_root)
        if cached is not None:
            return cached
        gitignore = tree_root / ".gitignore"
        if gitignore.is_file():
            patterns = tuple(
                line.strip()
                for line in gitignore.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            )
        else:
            patterns = ()
        self._tree_ignore_patterns[tree_root] = patterns
        return patterns

    def _should_descend_into(self, path: Path, root: Path) -> bool:
        if not path.is_symlink():
            return True
        try:
            path.resolve().relative_to(root.resolve())
        except ValueError:
            return False
        return True

    def _normalize_relative_path(self, path: str) -> str:
        normalized = path.strip().replace("\\", "/")
        if not normalized or normalized.startswith("/"):
            raise ValueError("path must be a non-empty relative path")
        parts = Path(normalized).parts
        if any(part == ".." for part in parts):
            raise ValueError("path must stay within the workspace")
        return Path(*parts).as_posix()

    def _is_untracked(self, root: Path, path: str) -> bool:
        result = self._run_git(root, "ls-files", "--others", "--exclude-standard", "--", path)
        return result.returncode == 0 and any(
            line.strip() == path for line in result.stdout.splitlines()
        )

    def _untracked_diff(self, path: str, *, root: Path) -> str:
        diff_path = Path(path).as_posix()
        file_path = root / diff_path
        if not file_path.exists():
            return "\n".join(
                (
                    f"diff --git a/{diff_path} b/{diff_path}",
                    "new file mode 100644",
                    "--- /dev/null",
                    f"+++ b/{diff_path}",
                )
            )
        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return "\n".join(
                (
                    f"diff --git a/{diff_path} b/{diff_path}",
                    "new file mode 100644",
                    f"Binary files /dev/null and b/{diff_path} differ",
                )
            )
        added_lines = [f"+{line}" for line in content.splitlines()]
        line_count = len(added_lines)
        hunk_header = f"@@ -0,0 +1,{line_count} @@" if line_count else "@@ -0,0 +0,0 @@"
        return "\n".join(
            (
                f"diff --git a/{diff_path} b/{diff_path}",
                "new file mode 100644",
                "--- /dev/null",
                f"+++ b/{diff_path}",
                hunk_header,
                *added_lines,
            )
        )

    @staticmethod
    def _run_git(root: Path, *args: str) -> GitCommandResult:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return GitCommandResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )


def _matches_gitignore_path(
    relative_path: str,
    patterns: tuple[str, ...],
    *,
    is_directory: bool,
) -> bool:
    normalized = relative_path.strip("/")
    if not normalized:
        return False
    ignored = False
    for pattern in patterns:
        normalized_pattern = pattern.strip()
        if not normalized_pattern or normalized_pattern.startswith("#"):
            continue
        negated = normalized_pattern.startswith("!")
        if negated:
            normalized_pattern = normalized_pattern[1:]
        if _matches_gitignore_pattern(
            normalized,
            normalized_pattern,
            is_directory=is_directory,
        ):
            ignored = not negated
    return ignored


def _matches_gitignore_pattern(
    relative_path: str,
    pattern: str,
    *,
    is_directory: bool,
) -> bool:
    normalized_pattern = pattern.strip()
    if not normalized_pattern or normalized_pattern.startswith("#"):
        return False
    if normalized_pattern.startswith("!"):
        return False

    directory_only = normalized_pattern.endswith("/")
    if directory_only and not is_directory:
        return False
    anchored = normalized_pattern.startswith("/")
    normalized_pattern = normalized_pattern.strip("/")
    if not normalized_pattern:
        return False

    path_parts = Path(relative_path).parts
    if "/" not in normalized_pattern:
        if directory_only:
            return normalized_pattern in path_parts
        return any(fnmatchcase(part, normalized_pattern) for part in path_parts)

    if anchored:
        if directory_only:
            return relative_path == normalized_pattern or relative_path.startswith(
                f"{normalized_pattern}/"
            )
        return fnmatchcase(relative_path, normalized_pattern)
    if directory_only:
        return (
            relative_path == normalized_pattern
            or relative_path.endswith(f"/{normalized_pattern}")
            or f"/{normalized_pattern}/" in f"/{relative_path}/"
        )
    return fnmatchcase(relative_path, normalized_pattern) or fnmatchcase(
        relative_path,
        f"*/{normalized_pattern}",
    )
