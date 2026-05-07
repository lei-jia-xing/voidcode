from __future__ import annotations

from pathlib import Path
from typing import cast

from ..security.path_policy import resolve_workspace_path
from ._repair import raise_tool_diagnostic
from .contracts import ToolResult
from .runtime_context import current_runtime_tool_context


def read_paths_for_tool_results(
    *,
    tool_results: tuple[ToolResult, ...],
    workspace: Path,
) -> frozenset[str]:
    resolved_paths: set[str] = set()
    for result in tool_results:
        if result.tool_name != "read_file" or result.status != "ok":
            continue
        candidate = _resolve_internal_workspace_path(
            workspace=workspace,
            raw_path=_read_result_path(result),
        )
        if candidate is not None:
            resolved_paths.add(candidate.as_posix())
    return frozenset(resolved_paths)


def enforce_read_before_write(
    *,
    tool_name: str,
    workspace: Path,
    raw_path: str,
    candidate: Path,
    display_path: str,
    is_external: bool,
) -> None:
    if is_external or not candidate.exists() or not candidate.is_file():
        return
    context = current_runtime_tool_context()
    if context is None:
        return
    if candidate.resolve().as_posix() in context.read_paths:
        return
    raise_tool_diagnostic(
        message=(
            f"{tool_name} requires reading the current file before modifying it: {display_path}"
        ),
        error_kind="tool_input_mismatch",
        reason="write_without_read",
        retry_guidance=(
            "Use read_file on the target path first, review the current content, "
            "then retry the change."
        ),
        details={"path": display_path, "raw_path": raw_path},
    )


def _read_result_path(result: ToolResult) -> str | None:
    raw_arguments = result.data.get("arguments")
    if isinstance(raw_arguments, dict):
        arguments = cast(dict[str, object], raw_arguments)
        raw_file_path = arguments.get("filePath")
        if isinstance(raw_file_path, str) and raw_file_path.strip():
            return raw_file_path
        raw_path = arguments.get("path")
        if isinstance(raw_path, str) and raw_path.strip():
            return raw_path
    raw_path = result.data.get("path")
    if isinstance(raw_path, str) and raw_path.strip():
        return raw_path
    return None


def _resolve_internal_workspace_path(*, workspace: Path, raw_path: str | None) -> Path | None:
    if raw_path is None or not raw_path.strip():
        return None
    resolution = resolve_workspace_path(
        workspace=workspace,
        raw_path=raw_path,
        allow_outside_workspace=True,
    )
    if resolution.is_external:
        return None
    return resolution.candidate.resolve()


__all__ = ["enforce_read_before_write", "read_paths_for_tool_results"]
