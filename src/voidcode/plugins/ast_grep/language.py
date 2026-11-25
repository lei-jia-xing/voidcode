"""Language resolution and path-surface filtering for ast_grep results."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from voidcode.capabilities.files._common import (
    DEFAULT_IGNORE_PATTERNS,
    full_match_path,
    has_hidden_component,
    is_default_ignored,
)
from voidcode.capabilities.tool_errors import raise_tool_diagnostic
from voidcode.sdk.services import PluginServices

from .process import _OUTLINE_CAPTURE_BYTES, _parse_outline_language, _raise_on_process_failure, _run_ast_grep


def _infer_language(
    *,
    display_path: str,
    workspace: Path,
    services: PluginServices,
    timeout_seconds: int,
    stage: str,
) -> str:
    completed = _run_ast_grep(
        cmd=["ast-grep", "outline", "--json=stream", "--", display_path],
        workspace=workspace,
        services=services,
        timeout_seconds=timeout_seconds,
        query=True,
        max_output_bytes=_OUTLINE_CAPTURE_BYTES,
    )
    if completed.cancelled:
        raise_tool_diagnostic(
            message=f"ast-grep language inference for {stage} was cancelled.",
            error_kind="tool_cancelled",
            reason="process_capture_failed",
            retry_guidance="Retry the operation if it was cancelled unintentionally.",
            details={"stage": stage},
        )
    if completed.timed_out:
        raise_tool_diagnostic(
            message=f"ast-grep language inference for {stage} timed out after {timeout_seconds}s.",
            error_kind="tool_execution_error",
            reason="search_io_error" if stage != "replace" else "edit_failed",
            retry_guidance="Pass lang explicitly and retry.",
            details={"stage": stage, "timeout_seconds": timeout_seconds},
        )
    if completed.truncated:
        raise_tool_diagnostic(
            message=f"ast-grep language inference for {stage} exceeded the capture limit; pass lang explicitly.",
            error_kind="tool_environment_error",
            reason="process_capture_failed",
            retry_guidance="Pass lang explicitly and retry.",
            details={"stage": stage},
        )
    if completed.exit_code != 0:
        _raise_on_process_failure(completed=completed, fallback_message="ast-grep outline failed", stage=stage)
    language = _parse_outline_language(completed.stdout)
    if language is None:
        raise_tool_diagnostic(
            message=f"ast-grep could not infer a language for {display_path}; pass lang explicitly.",
            error_kind="tool_input_mismatch",
            reason="invalid_arguments",
            retry_guidance="Retry with lang set to the target language (for example `python` or `typescript`).",
            details={"path": display_path, "stage": stage},
        )
    return language


def _resolved_language(
    args_lang: str | None,
    *,
    candidate: Path,
    display_path: str,
    workspace: Path,
    services: PluginServices,
    timeout_seconds: int,
    stage: str,
) -> str:
    if args_lang:
        return args_lang
    if candidate.is_dir():
        raise_tool_diagnostic(
            message=f"ast_grep {stage} requires lang when path is a directory: {display_path}",
            error_kind="tool_input_mismatch",
            reason="invalid_arguments",
            retry_guidance="Pass lang explicitly; ast-grep cannot disambiguate a structural pattern across languages.",
            details={"path": display_path, "stage": stage},
        )
    return _infer_language(
        display_path=display_path,
        workspace=workspace,
        services=services,
        timeout_seconds=timeout_seconds,
        stage=stage,
    )


def _ignore_flags(*, include_hidden: bool, respect_gitignore: bool) -> list[str]:
    flags: list[str] = []
    if include_hidden:
        flags.extend(["--no-ignore", "hidden"])
    if not respect_gitignore:
        flags.extend(["--no-ignore", "vcs"])
    flags.extend(["--no-ignore", "exclude", "--no-ignore", "global", "--no-ignore", "parent"])
    return flags


def _directory_globs() -> list[str]:
    return [f"!**/{name}/**" for name in sorted(DEFAULT_IGNORE_PATTERNS)]


def _file_matches_filters(path: Path, *, workspace: Path, include: list[str] | None, exclude: list[str] | None) -> bool:
    workspace_root = workspace.resolve()
    relative = path.relative_to(workspace_root).as_posix() if path.is_relative_to(workspace_root) else path.as_posix()
    if include and not any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in include):
        return False
    return not (exclude and any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in exclude))


def _match_resolved_path(match: dict[str, Any], *, workspace: Path) -> Path | None:
    raw_path = match.get("file")
    if not isinstance(raw_path, str) or not raw_path:
        return None
    raw = Path(raw_path)
    return (raw if raw.is_absolute() else workspace.resolve() / raw).resolve()


def _surface_relative_path(match: dict[str, Any], *, workspace: Path, search_root: Path) -> str | None:
    resolved = _match_resolved_path(match, workspace=workspace)
    if resolved is None:
        return None
    workspace_root = workspace.resolve()
    if search_root.is_relative_to(workspace_root):
        return resolved.relative_to(workspace_root).as_posix() if resolved.is_relative_to(workspace_root) else resolved.as_posix()
    if search_root.is_dir():
        try:
            return resolved.relative_to(search_root.resolve()).as_posix()
        except ValueError:
            return resolved.as_posix()
    return resolved.as_posix()


def _below_search_root(match: dict[str, Any], *, workspace: Path, search_root: Path) -> str | None:
    resolved = _match_resolved_path(match, workspace=workspace)
    if resolved is None:
        return None
    try:
        return resolved.relative_to(search_root.resolve()).as_posix()
    except ValueError:
        return resolved.as_posix()


def _filter_matches(
    matches: list[dict[str, Any]],
    *,
    workspace: Path,
    search_root: Path,
    include: list[str] | None,
    exclude: list[str] | None,
    include_hidden: bool,
) -> list[dict[str, Any]]:
    if search_root.is_file():
        return matches if _file_matches_filters(search_root, workspace=workspace, include=include, exclude=exclude) else []
    selected: list[dict[str, Any]] = []
    for match in matches:
        relative = _surface_relative_path(match, workspace=workspace, search_root=search_root)
        below_root = _below_search_root(match, workspace=workspace, search_root=search_root)
        if relative is None or below_root is None:
            continue
        if is_default_ignored(Path(below_root), is_directory=False):
            continue
        relative_path = Path(relative)
        if not include_hidden and has_hidden_component(relative_path):
            continue
        if include and not any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in include):
            continue
        if exclude and any(full_match_path(relative, pattern, basename_any_depth=True) for pattern in exclude):
            continue
        selected.append(match)
    return selected


def _native_jsonl(matches: list[dict[str, Any]]) -> str:
    if not matches:
        return ""
    return "\n".join(json.dumps(match, ensure_ascii=False) for match in matches) + "\n"


__all__ = [
    "_below_search_root",
    "_directory_globs",
    "_file_matches_filters",
    "_filter_matches",
    "_ignore_flags",
    "_infer_language",
    "_match_resolved_path",
    "_native_jsonl",
    "_resolved_language",
    "_surface_relative_path",
]
