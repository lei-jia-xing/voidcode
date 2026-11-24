"""Git diffs needed by the review command, scoped and treated as untrusted data."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

type GitStatusState = Literal["git_ready", "not_git_repo", "git_error"]

_GIT_TIMEOUT_SECONDS = 30
_MAX_GIT_ERROR_CHARS = 2_000
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


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
    #: Repo-relative untracked paths omitted as likely secrets; only the count is surfaced.
    skipped_untracked: tuple[str, ...] = ()


def _run_git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", "-c", "core.quotepath=false", "-C", str(root), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=_GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ValueError("git executable was not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ValueError(f"git {args[0] if args else 'command'} timed out after {_GIT_TIMEOUT_SECONDS}s") from exc
    except OSError as exc:
        raise ValueError(f"git could not start: {exc}") from exc


def _bounded_git_detail(*candidates: str) -> str | None:
    """First non-empty Git diagnostic, bounded for the snapshot payload."""
    for value in candidates:
        text = value.strip()
        if text:
            return text[:_MAX_GIT_ERROR_CHARS]
    return None


def _has_git_metadata(workspace: Path) -> bool:
    """Whether the workspace or an ancestor carries a ``.git`` entry.

    This is a structural probe, not message parsing: Git returns 128 for both
    "no repository" and a broken repository, so the marker is what separates
    them without reading the refusal text.
    """
    current = workspace
    while True:
        if (current / ".git").exists():
            return True
        parent = current.parent
        if parent == current:
            return False
        current = parent


def git_status_snapshot(workspace: Path) -> GitStatusSnapshot:
    """Resolve the repository root while preserving bounded Git diagnostics.

    Git exits 128 for every fatal ``rev-parse`` failure, so the refusal text is
    its only diagnostic. A failure below a ``.git`` marker is a broken
    repository; any other failure means this workspace has no repository.
    """
    resolved = workspace.resolve()
    try:
        result = _run_git(resolved, "rev-parse", "--show-toplevel")
    except ValueError as exc:
        return GitStatusSnapshot(state="git_error", root=None, error=str(exc))
    if result.returncode != 0:
        state: GitStatusState = "git_error" if _has_git_metadata(resolved) else "not_git_repo"
        return GitStatusSnapshot(state=state, root=None, error=_bounded_git_detail(result.stderr, result.stdout))
    return GitStatusSnapshot(state="git_ready", root=result.stdout.strip())


def _clean_ref(raw: str, *, where: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{where} must be a non-empty string")
    text = raw.strip()
    if text.startswith("-"):
        raise ValueError(f"{where} must not start with '-'")
    return text


def _is_secret_untracked(path: str) -> bool:
    name = Path(path).name.lower()
    return name.startswith(".env") or name.endswith(".pem") or name.endswith(".key") or name.startswith("credentials") or name.startswith("id_")


def _in_scope(path: str, scope: tuple[str, ...]) -> bool:
    if not scope:
        return True
    base = scope[0]
    return path == base or path.startswith(f"{base}/")


class WorkspaceReviewService:
    def __init__(self, *, workspace: Path) -> None:
        self._workspace = workspace.resolve()

    def diff_all(self, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(state="not_git_repo", diff=None)
        scope = self._scope_pathspec(root)
        diff_output, skipped = self._workspace_diff(root, scope)
        return ReviewFileDiff(state="changed" if diff_output else "clean", diff=diff_output or None, skipped_untracked=skipped)

    def diff_base_branch(self, branch: str, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(state="not_git_repo", diff=None)
        clean_branch = _clean_ref(branch, where="base branch")
        base_commit = self._resolve_commit(root, clean_branch, where="base branch")
        mb = _run_git(root, "merge-base", "HEAD", base_commit)
        if mb.returncode != 0 or not mb.stdout.strip():
            detail = mb.stderr.strip() or f"no common ancestor with branch '{clean_branch}'"
            raise ValueError(f"git merge-base failed: {detail}")
        merge_base_sha = mb.stdout.strip()
        scope = self._scope_pathspec(root)
        command = _run_git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-color", "--binary", merge_base_sha, "--", *scope)
        if command.returncode != 0:
            detail = command.stderr.strip() or "unknown git error"
            raise ValueError(f"git diff failed: {detail}")
        parts = [command.stdout]
        untracked, skipped = self._untracked_paths(root, scope)
        for path in untracked:
            diff = self._untracked_diff(path, root=root)
            if diff:
                parts.append(diff)
        diff_output = self._join_diffs(parts)
        return ReviewFileDiff(
            state="changed" if diff_output else "clean",
            diff=diff_output or None,
            merge_base=merge_base_sha,
            skipped_untracked=skipped,
        )

    def diff_commit(self, sha: str, *, git: GitStatusSnapshot) -> ReviewFileDiff:
        root = Path(git.root).resolve() if git.root is not None else self._workspace
        if git.state != "git_ready":
            return ReviewFileDiff(state="not_git_repo", diff=None)
        clean_sha = _clean_ref(sha, where="commit sha")
        commit_sha = self._resolve_commit(root, clean_sha, where="commit")
        scope = self._scope_pathspec(root)
        command = _run_git(
            root,
            "show",
            "--format=",
            "--patch",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--binary",
            commit_sha,
            "--",
            *scope,
        )
        if command.returncode != 0:
            detail = command.stderr.strip() or f"unknown commit '{clean_sha}'"
            raise ValueError(f"git show failed: {detail}")
        diff_output = command.stdout
        return ReviewFileDiff(state="changed" if diff_output.strip() else "clean", diff=diff_output or None)

    def _resolve_commit(self, root: Path, ref: str, *, where: str) -> str:
        result = _run_git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if result.returncode != 0:
            detail = result.stderr.strip()
            suffix = f": {detail}" if detail else ""
            raise ValueError(f"git rev-parse failed: unknown {where} '{ref}'{suffix}")
        resolved = result.stdout.strip()
        if _COMMIT_SHA.fullmatch(resolved) is None:
            raise ValueError(f"git rev-parse returned an unexpected object id for {where} '{ref}'")
        return resolved

    def _scope_pathspec(self, root: Path) -> tuple[str, ...]:
        try:
            relative = self._workspace.relative_to(root)
        except ValueError as exc:
            raise ValueError("workspace is outside the Git repository root") from exc
        return () if relative == Path(".") else (relative.as_posix(),)

    def _workspace_diff(self, root: Path, scope: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
        parts: list[str] = []
        for args in (("diff",), ("diff", "--cached")):
            command = _run_git(root, *args, "--no-ext-diff", "--no-textconv", "--no-color", "--binary", "--", *scope)
            if command.returncode != 0:
                detail = command.stderr.strip() or "unknown git error"
                raise ValueError(f"git diff failed: {detail}")
            parts.append(command.stdout)
        untracked, skipped = self._untracked_paths(root, scope)
        for path in untracked:
            diff = self._untracked_diff(path, root=root)
            if diff:
                parts.append(diff)
        return self._join_diffs(parts), skipped

    @staticmethod
    def _join_diffs(parts: list[str]) -> str:
        nonempty = [part.rstrip("\n") for part in parts if part]
        return "\n".join(nonempty) + ("\n" if nonempty else "")

    def _untracked_paths(self, root: Path, scope: tuple[str, ...]) -> tuple[tuple[str, ...], tuple[str, ...]]:
        result = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
        if result.returncode != 0:
            detail = result.stderr.strip() or "unknown git error"
            raise ValueError(f"git ls-files failed: {detail}")
        kept: list[str] = []
        skipped: list[str] = []
        for path in result.stdout.split("\x00"):
            if not path or not _in_scope(path, scope):
                continue
            if _is_secret_untracked(path):
                skipped.append(path)
                continue
            kept.append(path)
        return tuple(kept), tuple(skipped)

    def _untracked_diff(self, path: str, *, root: Path) -> str:
        file_path = root / path
        if not file_path.exists() and (not file_path.is_symlink()):
            raise ValueError(f"untracked path disappeared: {path}")
        command = _run_git(
            root,
            "diff",
            "--no-index",
            "--no-ext-diff",
            "--no-textconv",
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
