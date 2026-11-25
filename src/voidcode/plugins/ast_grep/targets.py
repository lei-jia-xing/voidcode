"""Target resolution and target file state for ast_grep."""

from __future__ import annotations

import codecs
import hashlib
from dataclasses import dataclass
from pathlib import Path

from voidcode.capabilities.files._common import (
    MAX_MUTATION_CONTENT_BYTES,
    PathEscapesWorkspaceError,
    resolve_workspace_path,
)
from voidcode.capabilities.files.mutation import literal_target, raise_symlink_target
from voidcode.capabilities.tool_errors import raise_tool_diagnostic


@dataclass(frozen=True, slots=True)
class _ReplaceTarget:
    path: Path
    display_path: str
    source_hash: str
    byte_count: int
    original_text: str | None


def _invalid_path(tool_name: str, path_text: str, exc: BaseException) -> None:
    raise_tool_diagnostic(
        message=f"{tool_name} could not resolve the target path {path_text!r}: {exc}",
        error_kind="tool_input_mismatch",
        reason="invalid_path",
        retry_guidance="Retry with a valid file or directory path.",
        details={"path": path_text},
    )


def _resolve_search_candidate(*, workspace: Path, path_text: str) -> tuple[Path, str]:
    try:
        candidate, display_path = resolve_workspace_path(workspace, path_text)
    except (OSError, RuntimeError, ValueError) as exc:
        _invalid_path("ast_grep", path_text, exc)
    if not candidate.exists():
        raise_tool_diagnostic(
            message=f"ast_grep target does not exist: {path_text}",
            error_kind="tool_input_mismatch",
            reason="target_not_found",
            retry_guidance="Retry with an existing file or directory path.",
            details={"path": path_text},
        )
    if not candidate.is_file() and not candidate.is_dir():
        raise_tool_diagnostic(
            message=f"ast_grep target is not a regular file or directory: {path_text}",
            error_kind="tool_input_mismatch",
            reason="not_a_regular_file",
            retry_guidance="Retry with a regular file or directory path.",
            details={"path": path_text},
        )
    return candidate, display_path


def _resolve_replace_candidate(*, workspace: Path, path_text: str) -> tuple[Path, str]:
    if literal_target(workspace, path_text).is_symlink():
        raise_symlink_target("ast_grep_replace", path_text)
    try:
        candidate, display_path = resolve_workspace_path(workspace, path_text, allow_outside_workspace=False)
    except PathEscapesWorkspaceError:
        raise_tool_diagnostic(
            message=f"ast_grep_replace may only modify paths inside the workspace: {path_text}",
            error_kind="tool_input_mismatch",
            reason="outside_workspace",
            retry_guidance="Retry with a workspace-relative file; read, grep, and ast_grep search may still inspect external files.",
            details={"path": path_text},
        )
    except (OSError, RuntimeError, ValueError) as exc:
        _invalid_path("ast_grep_replace", path_text, exc)
    if not candidate.is_file():
        raise_tool_diagnostic(
            message=f"ast_grep_replace only supports a single regular file inside the workspace: {path_text}",
            error_kind="tool_input_mismatch",
            reason="not_a_regular_file",
            retry_guidance="Retry with one workspace file; directory and multi-file replacement are not supported.",
            details={"path": path_text},
        )
    return candidate, display_path


def _raise_stale_edit(display_path: str, *, expected_hash: str, actual_hash: str, after_preview: bool) -> None:
    message = (
        f"ast_grep replace rejected because {display_path} changed after preview (stale edit)."
        if after_preview
        else f"ast_grep replace rejected because {display_path} changed since it was read (stale edit)."
    )
    guidance = (
        "Preview the rewrite again against the current file contents before retrying."
        if after_preview
        else "Refresh the target file's SHA-256 content hash from a current file read, then retry."
    )
    raise_tool_diagnostic(
        message=message,
        error_kind="stale_edit",
        reason="content_hash_mismatch",
        retry_guidance=guidance,
        details={"expected_hash": expected_hash, "actual_hash": actual_hash, "path": display_path},
    )


def _load_replace_target(path: Path, display_path: str) -> _ReplaceTarget:
    digest = hashlib.sha256()
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    retained: list[bytes] = []
    size = 0
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
                decoder.decode(chunk)
                if size <= MAX_MUTATION_CONTENT_BYTES:
                    retained.append(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise_tool_diagnostic(
            message=f"ast_grep replace only supports UTF-8 text files: {display_path}",
            error_kind="tool_input_mismatch",
            reason="unsupported_encoding",
            retry_guidance="Retry with a UTF-8 text file, or use a binary-aware tool.",
            details={"path": display_path},
        )
    except OSError as exc:
        raise_tool_diagnostic(
            message=f"ast_grep replace failed while reading {display_path}: {exc}",
            error_kind="tool_environment_error",
            reason="resource_read_failed",
            retry_guidance="Retry the replacement; if the failure persists, check the file's permissions.",
            details={"path": display_path},
        )
    original_text = b"".join(retained).decode("utf-8") if size <= MAX_MUTATION_CONTENT_BYTES else None
    return _ReplaceTarget(path=path, display_path=display_path, source_hash=digest.hexdigest(), byte_count=size, original_text=original_text)


def _file_digest(path: Path) -> tuple[str, int] | None:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as source:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
    except OSError:
        return None
    return digest.hexdigest(), size


__all__ = [
    "_ReplaceTarget",
    "_file_digest",
    "_invalid_path",
    "_load_replace_target",
    "_raise_stale_edit",
    "_resolve_replace_candidate",
    "_resolve_search_candidate",
]
