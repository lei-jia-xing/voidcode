from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600


@dataclass(frozen=True, slots=True)
class ShellExecutionPolicy:
    workspace_root: Path
    timeout_seconds: int
    runtime_timeout_selected: bool
    non_interactive_blocked: bool = False
    non_interactive_reason: str | None = None
    retry_guidance: str | None = None


def resolve_shell_execution_policy(
    *,
    workspace: Path,
    command_text: str,
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

    blocked, reason, retry_guidance = classify_non_interactive_command(command_text)
    return ShellExecutionPolicy(
        workspace_root=workspace_root,
        timeout_seconds=timeout_seconds,
        runtime_timeout_selected=runtime_timeout_selected,
        non_interactive_blocked=blocked,
        non_interactive_reason=reason,
        retry_guidance=retry_guidance,
    )


def classify_non_interactive_command(command_text: str) -> tuple[bool, str | None, str | None]:
    try:
        tokens = shlex.split(command_text, posix=True)
    except ValueError:
        tokens = command_text.split()
    if not tokens:
        return False, None, None
    executable = tokens[0]
    if executable in {"vim", "vi", "nano", "less", "more", "man", "top", "htop", "watch"}:
        return (
            True,
            "interactive_command",
            "Use a non-interactive command or a file-oriented tool instead of "
            "launching an interactive TUI.",
        )
    if executable in {"python", "python3"} and "-i" in tokens[1:]:
        return (
            True,
            "interactive_command",
            "Avoid interactive Python shells; run a non-interactive script or use a "
            "file-oriented tool.",
        )
    if executable in {"bash", "sh", "zsh"} and all(token != "-c" for token in tokens[1:]):
        return (
            True,
            "interactive_command",
            "Provide a non-interactive shell command with -c, or use shell_exec for a "
            "bounded one-shot command.",
        )
    return False, None, None


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "ShellExecutionPolicy",
    "classify_non_interactive_command",
    "resolve_shell_execution_policy",
]
