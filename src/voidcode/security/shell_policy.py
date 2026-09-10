from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

_NON_INTERACTIVE_PACKAGE_MANAGER_ENV = {
    "CI": "1",
    "NPM_CONFIG_YES": "true",
    "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
}

_PROJECT_PACKAGE_MANAGERS_WITH_PROMPTS = frozenset({"bun", "npm", "pnpm", "yarn"})
_SHELL_CONTROL_OPERATORS = frozenset({"&", "&&", ";", "|", "|&", "||", "(", ")", "{", "}"})


@dataclass(frozen=True, slots=True)
class ShellExecutionPolicy:
    workspace_root: Path
    timeout_seconds: int
    runtime_timeout_selected: bool


def non_interactive_shell_env(command: str) -> dict[str, str]:
    for segment in _command_segments(command):
        normalized = tuple(_normalize_command_token(token) for token in _shell_tokens(segment))
        if any(candidate in _PROJECT_PACKAGE_MANAGERS_WITH_PROMPTS for candidate in _command_candidates(normalized)):
            return dict(_NON_INTERACTIVE_PACKAGE_MANAGER_ENV)
    return {}


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


def _shell_command_groups(command: str) -> tuple[list[list[str]], list[str]]:
    groups: list[list[str]] = []
    separators: list[str] = []
    current: list[str] = []
    for token in _shell_tokens(command):
        if token in _SHELL_CONTROL_OPERATORS:
            if current:
                groups.append(current)
                current = []
            if groups:
                separators.append(token)
        else:
            current.append(token)
    if current:
        groups.append(current)
    if len(separators) > max(0, len(groups) - 1):
        separators = separators[: max(0, len(groups) - 1)]
    return groups, separators


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(_normalize_shell_newlines(command), posix=False, punctuation_chars=";&|()<>")
        lexer.whitespace = " \t\r"
        # Keep URL punctuation and shell variable syntax inside one token;
        # punctuation_chars still emits actual control operators separately.
        lexer.wordchars += ":/@%+,.?=[]{}$~-/\\"
        return list(lexer)
    except ValueError:
        return command.split()


def _normalize_shell_newlines(command: str) -> str:
    output: list[str] = []
    quote: str | None = None
    escaped = False
    for character in command:
        if escaped:
            output.append(character)
            escaped = False
            continue
        if character == "\\" and quote != "'":
            output.append(character)
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            output.append(character)
            continue
        output.append(" ; " if character == "\n" and quote is None else character)
    return "".join(output)


def _command_segments(command: str) -> tuple[str, ...]:
    groups, _separators = _shell_command_groups(command)
    return tuple(" ".join(group).strip() for group in groups if group)


def _command_candidates(tokens: tuple[str, ...]) -> tuple[str, ...]:
    if not tokens:
        return ()
    candidates = [tokens[0]]
    if len(tokens) >= 2:
        candidates.append(f"{tokens[0]} {tokens[1]}")
    if len(tokens) >= 3 and tokens[1] == "-m":
        candidates.append(f"{tokens[0]} -m {tokens[2]}")
    return tuple(candidate for candidate in candidates if candidate)


def _normalize_command_token(token: str) -> str:
    return token.strip().strip("\"'`").lower()


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "MAX_TIMEOUT_SECONDS", "ShellExecutionPolicy", "non_interactive_shell_env", "resolve_shell_execution_policy"]
