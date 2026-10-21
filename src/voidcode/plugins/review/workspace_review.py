"""Workspace git surface for the review plugin.

The pure git/filesystem logic is copied from the legacy ``voidcode.runtime.review`` module: the
``WorkspaceReviewService`` methods, the gitignore matchers, the untracked-file handling and every
error string are the legacy ones. Only the legacy contract dataclasses moved in here (the plugin
tree cannot import ``voidcode.runtime``), and ``git_status_snapshot`` is the legacy inspection
coordinator's ``_git_status_snapshot`` producer, so ``state``/``root``/``error`` keep the legacy
values (``git_ready`` / ``not_git_repo`` / ``git_error``).

No core, plugin-host or product code is involved: this module only runs ``git`` and reads the
workspace, which is why the review command can refuse or accept a target before any child exists.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import Literal

_VCS_TREE_DIRECTORY_NAMES = frozenset({".git", ".hg", ".svn"})

type GitStatusState = Literal["git_ready", "not_git_repo", "git_error"]

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

#: git status flag -> review change type; first match in order wins.
_GIT_STATUS_CHANGE_TYPES: tuple[tuple[str, ReviewChangeType], ...] = (
    ("R", "renamed"),
    ("C", "copied"),
    ("A", "added"),
    ("D", "deleted"),
    ("T", "type_changed"),
    ("M", "modified"),
    ("U", "modified"),
)


@dataclass(frozen=True, slots=True)
class GitStatusSnapshot:
    state: GitStatusState
    root: str | None = None
    branch: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewChangedFile:
    path: str
    change_type: ReviewChangeType
    old_path: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewTreeNode:
    path: str
    name: str
    kind: Literal["file", "directory"]
    changed: bool
    children: tuple[ReviewTreeNode, ...] = ()


@dataclass(frozen=True, slots=True)
class ReviewFileDiff:
    root: str
    path: str
    state: Literal["changed", "clean", "not_git_repo"]
    diff: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewCommitLogEntry:
    sha: str
    timestamp: int
    subject: str


@dataclass(frozen=True, slots=True)
class WorkspaceReviewSnapshot:
    root: str
    git: GitStatusSnapshot
    changed_files: tuple[ReviewChangedFile, ...] = ()
    tree: tuple[ReviewTreeNode, ...] = ()


@dataclass(frozen=True, slots=True)
class GitCommandResult:
    returncode: int
    stdout: str
    stderr: str


def git_status_snapshot(workspace: Path) -> GitStatusSnapshot:
    """Resolve the workspace's git state, exactly as the legacy inspection coordinator did."""
    resolved = workspace.resolve()
    result = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )
    stdout = result.stdout
    stderr = result.stderr
    if result.returncode == 0:
        branch_result = subprocess.run(
            ["git", "-C", str(resolved), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        return GitStatusSnapshot(
            state="git_ready",
            root=stdout.strip() or str(resolved),
            branch=branch_result.stdout.strip() or None if branch_result.returncode == 0 else None,
        )
    if "not a git repository" in stderr.lower():
        return GitStatusSnapshot(state="not_git_repo", root=None, branch=None, error=stderr or None)
    return GitStatusSnapshot(state="git_error", root=None, error=stderr or stdout.strip() or None, branch=None)


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
            tree=self._tree(
                tree_root,
                tree_root=tree_root,
                changed_paths=changed_paths,
                use_git_ignore=git.state == "git_ready",
            ),
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
        diff_output = self._diff_for_paths(root, (normalized_path,))
        return ReviewFileDiff(
            root=str(root),
            path=normalized_path,
            state="changed" if diff_output else "clean",
            diff=diff_output or None,
        )

    def diff_all(self, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(root=str(root), path=".", state="not_git_repo", diff=None)
        diff_output = self._diff_for_paths(root, ())
        return ReviewFileDiff(
            root=str(root),
            path=".",
            state="changed" if diff_output else "clean",
            diff=diff_output or None,
        )

    def diff_base_branch(self, branch: str, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(root=str(root), path=branch, state="not_git_repo", diff=None)
        if not branch or not isinstance(branch, str) or not branch.strip():
            raise ValueError("branch must be a non-empty string")
        clean_branch = branch.strip()
        rev = self._run_git(root, "rev-parse", "--verify", clean_branch)
        if rev.returncode != 0:
            raise ValueError(f"git rev-parse failed: unknown base branch '{clean_branch}'")
        preferred_ref = clean_branch
        upstream_check = self._run_git(root, "rev-parse", "--abbrev-ref", f"{clean_branch}@{{upstream}}")
        if upstream_check.returncode == 0 and upstream_check.stdout.strip():
            upstream = upstream_check.stdout.strip()
            ahead = self._run_git(root, "rev-list", "--count", f"{clean_branch}..{upstream}")
            if ahead.returncode == 0 and ahead.stdout.strip().isdigit() and int(ahead.stdout.strip()) > 0:
                preferred_ref = upstream
        mb = self._run_git(root, "merge-base", "HEAD", preferred_ref)
        if mb.returncode != 0 or not mb.stdout.strip():
            mb = self._run_git(root, "merge-base", "HEAD", clean_branch)
        if mb.returncode != 0 or not mb.stdout.strip():
            detail = mb.stderr.strip() or f"no common ancestor with branch '{clean_branch}'"
            raise ValueError(f"git merge-base failed: {detail}")
        merge_base_sha = mb.stdout.strip()
        command = self._run_git(
            root,
            "diff",
            "--no-ext-diff",
            "--no-color",
            "--binary",
            merge_base_sha,
        )
        if command.returncode != 0:
            detail = command.stderr.strip() or "unknown git error"
            raise ValueError(f"git diff failed: {detail}")
        parts = [command.stdout]
        for path in self._untracked_paths(root):
            untracked = self._untracked_diff(path, root=root)
            if untracked:
                parts.append(untracked)
        diff_output = self._join_diffs(parts)
        return ReviewFileDiff(
            root=str(root),
            path=clean_branch,
            state="changed" if diff_output else "clean",
            diff=diff_output or None,
        )

    def merge_base_sha(self, branch: str, *, git: GitStatusSnapshot) -> str | None:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return None
        rev = self._run_git(root, "rev-parse", "--verify", branch)
        if rev.returncode != 0:
            return None
        preferred_ref = branch
        upstream_check = self._run_git(root, "rev-parse", "--abbrev-ref", f"{branch}@{{upstream}}")
        if upstream_check.returncode == 0 and upstream_check.stdout.strip():
            upstream = upstream_check.stdout.strip()
            ahead = self._run_git(root, "rev-list", "--count", f"{branch}..{upstream}")
            if ahead.returncode == 0 and ahead.stdout.strip().isdigit() and int(ahead.stdout.strip()) > 0:
                preferred_ref = upstream
        mb = self._run_git(root, "merge-base", "HEAD", preferred_ref)
        if mb.returncode == 0 and mb.stdout.strip():
            return mb.stdout.strip()
        mb = self._run_git(root, "merge-base", "HEAD", branch)
        return mb.stdout.strip() if mb.returncode == 0 and mb.stdout.strip() else None

    def diff_commit(self, sha: str, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(root=str(root), path=sha, state="not_git_repo", diff=None)
        if not sha or not isinstance(sha, str) or not sha.strip():
            raise ValueError("commit sha must be a non-empty string")
        clean_sha = sha.strip()
        command = self._run_git(
            root,
            "show",
            "--no-ext-diff",
            "--no-color",
            "--binary",
            clean_sha,
        )
        if command.returncode != 0:
            detail = command.stderr.strip() or f"unknown commit '{clean_sha}'"
            raise ValueError(f"git show failed: {detail}")
        diff_output = command.stdout
        has_diff = any(line.startswith(("diff --git ", "--- ", "+++ ")) for line in diff_output.splitlines())
        return ReviewFileDiff(
            root=str(root),
            path=clean_sha,
            state="changed" if has_diff else "clean",
            diff=diff_output if has_diff else None,
        )

    def list_branches(self, *, git: GitStatusSnapshot) -> tuple[str, ...]:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ()
        out = self._run_git(root, "for-each-ref", "--format=%(refname:short)", "refs/heads")
        if out.returncode != 0:
            return ()
        branches = [line.strip() for line in out.stdout.splitlines() if line.strip()]
        branches.sort()
        default_branch = None
        sym = self._run_git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        if sym.returncode == 0 and sym.stdout.strip():
            candidate = sym.stdout.strip().removeprefix("origin/")
            if candidate in branches:
                default_branch = candidate
        if default_branch is None:
            for candidate in ("main", "master"):
                if candidate in branches:
                    default_branch = candidate
                    break
        if default_branch and default_branch in branches:
            branches.remove(default_branch)
            branches.insert(0, default_branch)
        return tuple(branches)

    def current_branch(self, *, git: GitStatusSnapshot) -> str | None:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return None
        out = self._run_git(root, "branch", "--show-current")
        if out.returncode != 0:
            return None
        cleaned = out.stdout.strip()
        return cleaned or None

    def recent_commits(self, *, git: GitStatusSnapshot, limit: int = 100) -> tuple[ReviewCommitLogEntry, ...]:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ()
        args = ["log"]
        if limit > 0:
            args.extend(["-n", str(limit)])
        args.append("--pretty=format:%H\x1f%ct\x1f%s")
        out = self._run_git(root, *args)
        if out.returncode != 0:
            return ()
        entries: list[ReviewCommitLogEntry] = []
        for line in out.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) >= 3:
                sha = parts[0].strip()
                try:
                    ts = int(parts[1].strip())
                except ValueError:
                    ts = 0
                subject = parts[2].strip()
                if sha:
                    entries.append(ReviewCommitLogEntry(sha=sha, timestamp=ts, subject=subject))
        return tuple(entries)

    def _diff_for_paths(self, root: Path, paths: tuple[str, ...]) -> str:
        parts: list[str] = []
        for args in (("diff",), ("diff", "--cached")):
            command = self._run_git(
                root,
                *args,
                "--no-ext-diff",
                "--no-color",
                "--binary",
                "--",
                *paths,
            )
            if command.returncode != 0:
                detail = command.stderr.strip() or "unknown git error"
                raise ValueError(f"git diff failed: {detail}")
            parts.append(command.stdout)
        if not paths:
            paths = self._untracked_paths(root)
        else:
            paths = tuple(path for path in paths if self._is_untracked(root, path))
        for path in paths:
            untracked = self._untracked_diff(path, root=root)
            if untracked:
                parts.append(untracked)
        return self._join_diffs(parts)

    @staticmethod
    def _join_diffs(parts: list[str]) -> str:
        nonempty = [part.rstrip("\n") for part in parts if part]
        return "\n".join(nonempty) + ("\n" if nonempty else "")

    def _untracked_paths(self, root: Path) -> tuple[str, ...]:
        result = self._run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
        if result.returncode != 0:
            detail = result.stderr.strip() or "unknown git error"
            raise ValueError(f"git ls-files failed: {detail}")
        return tuple(path for path in result.stdout.split("\0") if path)

    def _changed_files(self, root: Path) -> tuple[ReviewChangedFile, ...]:
        tracked = self._run_git(root, "status", "--short", "--untracked-files=all", "-z")
        if tracked.returncode != 0:
            detail = tracked.stderr.strip() or "unknown git error"
            raise ValueError(f"git status failed: {detail}")
        changed_files: list[ReviewChangedFile] = []
        records = [record for record in tracked.stdout.split("\0") if record]
        index = 0
        while index < len(records):
            record = records[index]
            status = record[:2]
            payload = record[3:] if len(record) > 2 and record[2] == " " else record[2:]
            if not payload:
                index += 1
                continue
            if status[0] in "RC" or status[1] in "RC":
                old_path = records[index + 1] if index + 1 < len(records) else None
                index += 1
            else:
                old_path = None
            change_code = status.replace(" ", "") or "??"
            changed_files.append(
                ReviewChangedFile(
                    path=payload,
                    change_type=self._map_change_type(change_code),
                    old_path=old_path,
                )
            )
            index += 1
        changed_files.sort(key=lambda item: item.path)
        return tuple(changed_files)

    @staticmethod
    def _map_change_type(code: str) -> ReviewChangeType:
        normalized = code.upper()
        if normalized == "??":
            return "untracked"
        for flag, change_type in _GIT_STATUS_CHANGE_TYPES:
            if flag in normalized:
                return change_type
        return "unknown"

    def _tree(
        self,
        root: Path,
        *,
        tree_root: Path,
        changed_paths: set[str],
        use_git_ignore: bool,
    ) -> tuple[ReviewTreeNode, ...]:
        children: list[ReviewTreeNode] = []
        for entry in sorted(root.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())):
            if self._should_exclude_from_tree(entry, tree_root, use_git_ignore=use_git_ignore):
                continue
            if entry.is_dir() and self._should_descend_into(entry, tree_root):
                descendants = self._tree(
                    entry,
                    tree_root=tree_root,
                    changed_paths=changed_paths,
                    use_git_ignore=use_git_ignore,
                )
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

    def _should_exclude_from_tree(
        self,
        path: Path,
        tree_root: Path,
        *,
        use_git_ignore: bool,
    ) -> bool:
        if path.is_dir() and path.name in _VCS_TREE_DIRECTORY_NAMES:
            return True
        relative_path = self._relative_to_root(path, tree_root)
        return self._is_ignored_tree_path(
            tree_root,
            relative_path,
            is_directory=path.is_dir(),
            use_git_ignore=use_git_ignore,
        )

    def _is_ignored_tree_path(
        self,
        tree_root: Path,
        relative_path: str,
        *,
        is_directory: bool,
        use_git_ignore: bool,
    ) -> bool:
        if use_git_ignore:
            git_check = self._run_git(
                tree_root,
                "check-ignore",
                "--quiet",
                "--",
                relative_path,
            )
            if git_check.returncode == 0:
                if is_directory and self._has_tracked_tree_path(tree_root, relative_path):
                    return False
                return True
            if git_check.returncode == 1:
                return False
        return _matches_gitignore_path(
            relative_path,
            self._ignore_patterns_for_tree_root(tree_root),
            is_directory=is_directory,
        )

    def _has_tracked_tree_path(self, tree_root: Path, relative_path: str) -> bool:
        tracked = self._run_git(tree_root, "ls-files", "--", f"{relative_path}/")
        return tracked.returncode == 0 and bool(tracked.stdout.strip())

    def _ignore_patterns_for_tree_root(self, tree_root: Path) -> tuple[str, ...]:
        cached = self._tree_ignore_patterns.get(tree_root)
        if cached is not None:
            return cached
        gitignore = tree_root / ".gitignore"
        if gitignore.is_file():
            patterns = tuple(
                line.strip() for line in gitignore.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")
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
        candidate = Path(path)
        if not path or candidate.is_absolute():
            raise ValueError("path must be a non-empty relative path")
        if any(part == ".." for part in candidate.parts):
            raise ValueError("path must stay within the workspace")
        return candidate.as_posix()

    def _is_untracked(self, root: Path, path: str) -> bool:
        result = self._run_git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", path)
        if result.returncode != 0:
            detail = result.stderr.strip() or "unknown git error"
            raise ValueError(f"git ls-files failed: {detail}")
        return path in result.stdout.split("\0")

    def _untracked_diff(self, path: str, *, root: Path) -> str:
        file_path = root / path
        if not file_path.exists() and not file_path.is_symlink():
            raise ValueError(f"untracked path disappeared: {path}")
        command = self._run_git(
            root,
            "diff",
            "--no-index",
            "--no-ext-diff",
            "--no-color",
            "--binary",
            "--",
            os.devnull,
            path,
        )
        if command.returncode not in (0, 1):
            detail = command.stderr.strip() or "unknown git error"
            raise ValueError(f"git diff failed: {detail}")
        return command.stdout

    @staticmethod
    def _run_git(root: Path, *args: str) -> GitCommandResult:
        result = subprocess.run(
            ["git", "-c", "kernel.Path=false", "-C", str(root), *args],
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
            return relative_path == normalized_pattern or relative_path.startswith(f"{normalized_pattern}/")
        return _matches_path_segments(relative_path, normalized_pattern)
    if directory_only:
        return (
            relative_path == normalized_pattern
            or relative_path.endswith(f"/{normalized_pattern}")
            or f"/{normalized_pattern}/" in f"/{relative_path}/"
        )
    return _matches_path_segments(relative_path, normalized_pattern)


def _matches_path_segments(relative_path: str, pattern: str) -> bool:
    path_parts = Path(relative_path).parts
    pattern_parts = Path(pattern).parts
    if len(path_parts) != len(pattern_parts):
        return False
    return all(fnmatchcase(path_part, pattern_part) for path_part, pattern_part in zip(path_parts, pattern_parts, strict=True))
