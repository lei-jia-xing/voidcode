from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class ShellExecutionPolicy:
    workspace_root: Path
    timeout_seconds: int
    runtime_timeout_selected: bool


def resolve_shell_execution_policy(
    *,
    workspace: Path,
    timeout_argument: object,
    runtime_timeout_seconds: int | None,
) -> ShellExecutionPolicy:
    workspace_root = workspace.resolve()
    if not workspace_root.exists() or not workspace_root.is_dir():
        raise ValueError("shell_exec workspace must be an existing directory")

    if isinstance(timeout_argument, (int, float)) and timeout_argument > 0:
        local_timeout_seconds = min(int(timeout_argument), MAX_TIMEOUT_SECONDS)
    else:
        local_timeout_seconds = DEFAULT_TIMEOUT_SECONDS

    timeout_seconds = local_timeout_seconds
    runtime_timeout_selected = False
    if runtime_timeout_seconds is not None and runtime_timeout_seconds < timeout_seconds:
        timeout_seconds = runtime_timeout_seconds
        runtime_timeout_selected = True

    return ShellExecutionPolicy(
        workspace_root=workspace_root,
        timeout_seconds=timeout_seconds,
        runtime_timeout_selected=runtime_timeout_selected,
    )


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "ShellExecutionPolicy",
    "resolve_shell_execution_policy",
]
