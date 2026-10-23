"""Git diffs needed by the review command, including untracked files."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type GitStatusState = Literal["git_ready", "not_git_repo", "git_error"]


@dataclass(frozen=True, slots=True)
class GitStatusSnapshot:
    state: GitStatusState
    root: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ReviewFileDiff:
    state: Literal["changed", "clean", "not_git_repo"]
    diff: str | None = None
    merge_base: str | None = None


def git_status_snapshot(workspace: Path) -> GitStatusSnapshot:
    """Resolve the repository root while preserving Git diagnostics."""
    resolved = workspace.resolve()
    result = subprocess.run(
        ["git", "-C", str(resolved), "rev-parse", "--show-toplevel"], capture_output=True, text=True, errors="replace", timeout=30, check=False
    )
    stdout = result.stdout
    stderr = result.stderr
    if result.returncode == 0:
        return GitStatusSnapshot(state="git_ready", root=stdout.strip())
    if "not a git repository" in stderr.lower():
        return GitStatusSnapshot(state="not_git_repo", root=None, error=stderr or None)
    return GitStatusSnapshot(state="git_error", root=None, error=stderr or stdout.strip() or None)


class WorkspaceReviewService:
    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace.resolve()

    def diff_all(self, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(state="not_git_repo", diff=None)
        diff_output = self._workspace_diff(root)
        return ReviewFileDiff(state="changed" if diff_output else "clean", diff=diff_output or None)

    def diff_base_branch(self, branch: str, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(state="not_git_repo", diff=None)
        if not branch or not isinstance(branch, str) or (not branch.strip()):
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
            if ahead.returncode == 0 and ahead.stdout.strip().isdigit() and (int(ahead.stdout.strip()) > 0):
                preferred_ref = upstream
        mb = self._run_git(root, "merge-base", "HEAD", preferred_ref)
        if mb.returncode != 0 or not mb.stdout.strip():
            mb = self._run_git(root, "merge-base", "HEAD", clean_branch)
        if mb.returncode != 0 or not mb.stdout.strip():
            detail = mb.stderr.strip() or f"no common ancestor with branch '{clean_branch}'"
            raise ValueError(f"git merge-base failed: {detail}")
        merge_base_sha = mb.stdout.strip()
        command = self._run_git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--binary", merge_base_sha)
        if command.returncode != 0:
            detail = command.stderr.strip() or "unknown git error"
            raise ValueError(f"git diff failed: {detail}")
        parts = [command.stdout]
        for path in self._untracked_paths(root):
            untracked = self._untracked_diff(path, root=root)
            if untracked:
                parts.append(untracked)
        diff_output = self._join_diffs(parts)
        return ReviewFileDiff(state="changed" if diff_output else "clean", diff=diff_output or None, merge_base=merge_base_sha)

    def diff_commit(self, sha: str, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(state="not_git_repo", diff=None)
        if not sha or not isinstance(sha, str) or (not sha.strip()):
            raise ValueError("commit sha must be a non-empty string")
        clean_sha = sha.strip()
        command = self._run_git(root, "show", "--no-ext-diff", "--no-textconv", "--no-color", "--binary", clean_sha)
        if command.returncode != 0:
            detail = command.stderr.strip() or f"unknown commit '{clean_sha}'"
            raise ValueError(f"git show failed: {detail}")
        diff_output = command.stdout
        has_diff = any(line.startswith(("diff --git ", "--- ", "+++ ")) for line in diff_output.splitlines())
        return ReviewFileDiff(state="changed" if has_diff else "clean", diff=diff_output if has_diff else None)

    def _workspace_diff(self, root: Path) -> str:
        parts: list[str] = []
        for args in (("diff",), ("diff", "--cached")):
            command = self._run_git(root, *args, "--no-ext-diff", "--no-textconv", "--no-color", "--binary", "--")
            if command.returncode != 0:
                detail = command.stderr.strip() or "unknown git error"
                raise ValueError(f"git diff failed: {detail}")
            parts.append(command.stdout)
        for path in self._untracked_paths(root):
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
        return tuple(path for path in result.stdout.split("\x00") if path)

    def _untracked_diff(self, path: str, *, root: Path) -> str:
        file_path = root / path
        if not file_path.exists() and (not file_path.is_symlink()):
            raise ValueError(f"untracked path disappeared: {path}")
        command = self._run_git(root, "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--no-color", "--binary", "--", os.devnull, path)
        if command.returncode not in (0, 1):
            detail = command.stderr.strip() or "unknown git error"
            raise ValueError(f"git diff failed: {detail}")
        return command.stdout

    @staticmethod
    def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(root), *args], capture_output=True, text=True, errors="replace", timeout=30, check=False
        )
