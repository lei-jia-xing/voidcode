from __future__ import annotations

import shlex
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

type ShellCommandCategory = Literal[
    "readonly",
    "package_manager",
    "build_or_test",
    "interactive",
    "mutating",
    "destructive",
    "unknown",
]

_READONLY_COMMANDS = frozenset(
    {
        "cat",
        "dir",
        "env",
        "false",
        "git diff",
        "git log",
        "git ls-files",
        "git show",
        "git status",
        "grep",
        "ls",
        "pwd",
        "rg",
        "test",
        "true",
        "type",
        "which",
    }
)
_PACKAGE_MANAGER_COMMANDS = frozenset(
    {
        "apt",
        "apt-get",
        "brew",
        "bun",
        "cargo",
        "composer",
        "dnf",
        "gem",
        "go",
        "mise",
        "npm",
        "pnpm",
        "pip",
        "pip3",
        "poetry",
        "uv",
        "yarn",
    }
)
_BUILD_OR_TEST_COMMANDS = frozenset(
    {
        "cmake",
        "go test",
        "make",
        "ninja",
        "pytest",
        "tox",
    }
)
_INTERACTIVE_COMMANDS = frozenset(
    {
        "htop",
        "less",
        "more",
        "nano",
        "nvim",
        "ssh",
        "sudo",
        "top",
        "vim",
    }
)
_MUTATING_COMMANDS = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "git add",
        "git commit",
        "git merge",
        "git mv",
        "git rebase",
        "git restore",
        "git stash",
        "git switch",
        "git checkout",
        "install",
        "mkdir",
        "mv",
        "patch",
        "python -m pip",
        "python3 -m pip",
        "touch",
    }
)
_DESTRUCTIVE_COMMANDS = frozenset(
    {
        "dd",
        "mkfs",
        "rm",
        "rmdir",
        "shred",
    }
)
_SHELL_CONTROL_OPERATORS = frozenset({"&", "&&", ";", "|", "||"})


@dataclass(frozen=True, slots=True)
class ShellCommandSegment:
    text: str
    tokens: tuple[str, ...]
    category: ShellCommandCategory


@dataclass(frozen=True, slots=True)
class ShellCommandClassification:
    command: str
    category: ShellCommandCategory
    segments: tuple[ShellCommandSegment, ...]

    @property
    def interactive(self) -> bool:
        return self.category == "interactive"

    @property
    def denied_in_read_only(self) -> bool:
        return self.category in {"package_manager", "mutating", "destructive", "unknown"}


@dataclass(frozen=True, slots=True)
class ShellCommandPolicyDecision:
    allowed: bool
    reason: str | None = None
    injected_env_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ShellExecutionPolicy:
    workspace_root: Path
    timeout_seconds: int
    runtime_timeout_selected: bool


def classify_shell_command(command: str) -> ShellCommandClassification:
    command_text = command.strip()
    segments = tuple(
        segment
        for segment in (_classify_shell_segment(text) for text in _command_segments(command_text))
        if segment is not None
    )
    category = _highest_risk_category(segment.category for segment in segments)
    return ShellCommandClassification(command=command_text, category=category, segments=segments)


def resolve_shell_command_policy(
    command: str,
    *,
    read_only: bool = False,
    non_interactive: bool = True,
) -> ShellCommandPolicyDecision:
    classification = classify_shell_command(command)
    if non_interactive and classification.interactive:
        return ShellCommandPolicyDecision(
            allowed=False,
            reason=(
                "shell_exec cannot run interactive/TUI commands in non-interactive execution; "
                "use an interactive shell surface or choose a non-interactive command"
            ),
        )
    if read_only and classification.denied_in_read_only:
        return ShellCommandPolicyDecision(
            allowed=False,
            reason=(
                "read-only runtime policy denies shell commands classified as "
                f"{classification.category}"
            ),
        )
    return ShellCommandPolicyDecision(
        allowed=True,
        injected_env_keys=non_interactive_shell_env_keys(classification),
    )


def non_interactive_shell_env(command: str) -> dict[str, str]:
    classification = classify_shell_command(command)
    return {
        key: _NON_INTERACTIVE_PACKAGE_MANAGER_ENV[key]
        for key in non_interactive_shell_env_keys(classification)
    }


def non_interactive_shell_env_keys(
    classification: ShellCommandClassification,
) -> tuple[str, ...]:
    if classification.category != "package_manager":
        return ()
    if any(
        _package_manager_receives_non_interactive_env(segment)
        for segment in classification.segments
    ):
        return tuple(_NON_INTERACTIVE_PACKAGE_MANAGER_ENV)
    return ()


def extract_shell_path_candidates(command: str) -> tuple[str, ...]:
    tokens = _shell_tokens(command)
    candidates: list[str] = []
    for index, token in enumerate(tokens):
        value = _normalize_shell_path_token(token)
        if not value:
            continue
        if index == 0 and _looks_like_shell_executable(value):
            continue
        if _is_shell_explicit_output_path_candidate(tokens, index, value):
            candidates.append(value)
    return tuple(candidates)


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


def _classify_shell_segment(segment: str) -> ShellCommandSegment | None:
    text = segment.strip()
    if not text:
        return None
    tokens = tuple(_shell_tokens(text))
    if not tokens:
        return None
    return ShellCommandSegment(text=text, tokens=tokens, category=_segment_category(tokens))


_NON_INTERACTIVE_PACKAGE_MANAGER_ENV = {
    "CI": "1",
    "NPM_CONFIG_YES": "true",
    "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
}

_PROJECT_PACKAGE_MANAGERS_WITH_PROMPTS = frozenset(
    {
        "bun",
        "npm",
        "pnpm",
        "yarn",
    }
)


def _package_manager_receives_non_interactive_env(segment: ShellCommandSegment) -> bool:
    normalized = tuple(_normalize_command_token(token) for token in segment.tokens)
    command_candidates = _command_candidates(normalized)
    return any(
        candidate in _PROJECT_PACKAGE_MANAGERS_WITH_PROMPTS for candidate in command_candidates
    )


def _segment_category(tokens: tuple[str, ...]) -> ShellCommandCategory:
    normalized = tuple(_normalize_command_token(token) for token in tokens)
    command_candidates = _command_candidates(normalized)
    if any(candidate in _INTERACTIVE_COMMANDS for candidate in command_candidates):
        return "interactive"
    if any(candidate in _DESTRUCTIVE_COMMANDS for candidate in command_candidates):
        return "destructive"
    if _has_shell_mutation_tokens(tokens):
        return "mutating"
    if any(candidate in _MUTATING_COMMANDS for candidate in command_candidates):
        return "mutating"
    if any(candidate in _PACKAGE_MANAGER_COMMANDS for candidate in command_candidates):
        return "package_manager"
    if any(candidate in _BUILD_OR_TEST_COMMANDS for candidate in command_candidates):
        return "build_or_test"
    if any(candidate in _READONLY_COMMANDS for candidate in command_candidates):
        return "readonly"
    return "unknown"


def _highest_risk_category(categories: Iterable[ShellCommandCategory]) -> ShellCommandCategory:
    ranking: dict[ShellCommandCategory, int] = {
        "readonly": 0,
        "unknown": 1,
        "build_or_test": 2,
        "package_manager": 3,
        "mutating": 4,
        "destructive": 5,
        "interactive": 6,
    }
    selected: ShellCommandCategory | None = None
    for category in categories:
        if selected is None or ranking[category] > ranking[selected]:
            selected = category
    return selected or "unknown"


def _command_segments(command: str) -> tuple[str, ...]:
    segments: list[str] = []
    current: list[str] = []
    for token in _shell_tokens(command):
        if token in _SHELL_CONTROL_OPERATORS:
            text = " ".join(current).strip()
            if text:
                segments.append(text)
            current = []
        else:
            current.append(token)
    text = " ".join(current).strip()
    if text:
        segments.append(text)
    return tuple(segments)


def _shell_tokens(command: str) -> list[str]:
    try:
        lexer = shlex.shlex(command, posix=False, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return command.split()


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


def _has_shell_mutation_tokens(tokens: tuple[str, ...]) -> bool:
    return any(_has_shell_output_redirection(token) for token in tokens)


def _is_shell_explicit_output_path_candidate(tokens: list[str], index: int, value: str) -> bool:
    if not _looks_like_shell_path_candidate(value):
        return False
    token = tokens[index].strip()
    if _has_shell_output_redirection(token):
        return True
    option, has_inline_value = _shell_option_name(token)
    output_options = {"--output", "--output-document", "--out", "--outfile"}
    if option in output_options:
        return True
    previous = tokens[index - 1].strip() if index > 0 else ""
    if _has_shell_output_redirection(previous):
        return True
    if previous in output_options or previous == "-o":
        return True
    if has_inline_value:
        return False
    return False


def _has_shell_output_redirection(token: str) -> bool:
    stripped = token.strip().lstrip("0123456789")
    return stripped.startswith(">")


def _shell_option_name(token: str) -> tuple[str | None, bool]:
    stripped = token.strip().strip("\"'`")
    if not stripped.startswith("-"):
        return None, False
    if "=" in stripped:
        option, _value = stripped.split("=", 1)
        return option, True
    return stripped, False


def _normalize_shell_path_token(token: str) -> str:
    value = token.strip().strip("\"'`")
    redirection_index = 0
    while redirection_index < len(value) and value[redirection_index].isdigit():
        redirection_index += 1
    if redirection_index < len(value) and value[redirection_index] in ("<", ">"):
        value = value[redirection_index:]
    value = value.lstrip("<>")
    if "=" in value:
        _, assignment_value = value.split("=", 1)
        assignment_value = assignment_value.strip().strip("\"'`")
        if _looks_like_shell_path_candidate(assignment_value):
            return assignment_value
    return value


def _looks_like_shell_path_candidate(value: str) -> bool:
    normalized = value
    while normalized.startswith("./") or normalized.startswith(".\\"):
        normalized = normalized[2:]
    if normalized.startswith(("~/", "../", "..\\", "/")):
        return True
    return len(normalized) >= 3 and normalized[1] == ":" and normalized[2] in ("\\", "/")


def _looks_like_shell_executable(value: str) -> bool:
    if value.startswith(("/", "~/")):
        return True
    return len(value) >= 3 and value[1] == ":" and value[2] in ("\\", "/")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "ShellCommandCategory",
    "ShellCommandClassification",
    "ShellCommandPolicyDecision",
    "ShellCommandSegment",
    "ShellExecutionPolicy",
    "classify_shell_command",
    "extract_shell_path_candidates",
    "non_interactive_shell_env",
    "non_interactive_shell_env_keys",
    "resolve_shell_command_policy",
    "resolve_shell_execution_policy",
]
