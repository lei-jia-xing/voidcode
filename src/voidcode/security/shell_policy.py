from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT_SECONDS = 120
MAX_TIMEOUT_SECONDS = 600

_SHELL_CONTROL_OPERATORS = frozenset({"&", "&&", ";", "|", "||"})


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


def extract_shell_path_candidates(command: str) -> tuple[str, ...]:
    """Return paths with a declared shell write effect.

    This intentionally models only a small, declarative set of commands.  A
    command that is not in the table contributes no targets (rather than
    turning arbitrary arguments into permission paths).  Output redirects and
    established output options remain supported independently.
    """
    candidates: list[str] = []
    for segment in _command_segments(command):
        tokens = _shell_tokens(segment)
        if not tokens:
            continue
        candidates.extend(_extract_mutator_targets(tokens))
        candidates.extend(_extract_output_path_candidates(tokens))
    return tuple(candidate for candidate in candidates if candidate)


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


@dataclass(frozen=True, slots=True)
class _ShellMutatorSpec:
    target_mode: str
    value_options: frozenset[str] = frozenset()
    target_options: frozenset[str] = frozenset()
    leading_operands: int = 0


_MUTATOR_SPECS: dict[str, _ShellMutatorSpec] = {
    "touch": _ShellMutatorSpec("all", frozenset({"-d", "--date", "-r", "--reference", "-t"})),
    "mkdir": _ShellMutatorSpec("all"),
    "rm": _ShellMutatorSpec("all"),
    "rmdir": _ShellMutatorSpec("all"),
    "cp": _ShellMutatorSpec(
        "last",
        frozenset({"--backup", "-S", "--suffix", "--context", "--reflink", "--sparse"}),
        frozenset({"--target-directory", "-t"}),
    ),
    "mv": _ShellMutatorSpec("pair"),
    "ln": _ShellMutatorSpec("last", frozenset({"--backup", "-S", "--suffix"})),
    "install": _ShellMutatorSpec(
        "last",
        frozenset({"-g", "--group", "-m", "--mode", "-o", "--owner", "-S", "--suffix", "-t"}),
    ),
    "tee": _ShellMutatorSpec("all", frozenset({"--output-error"})),
    "truncate": _ShellMutatorSpec("all", frozenset({"-r", "--reference", "-s", "--size"})),
    "chmod": _ShellMutatorSpec("all", frozenset({"--reference"}), leading_operands=1),
    "chown": _ShellMutatorSpec("all", frozenset({"--from", "--reference"}), leading_operands=1),
    "chgrp": _ShellMutatorSpec("all", frozenset({"--reference"}), leading_operands=1),
    "git add": _ShellMutatorSpec("all", frozenset({"--pathspec-from-file", "--pathspec-file-nul"})),
    "git mv": _ShellMutatorSpec("pair"),
    "git commit": _ShellMutatorSpec("all", frozenset({"-m", "--message", "-F", "--file"})),
    "git merge": _ShellMutatorSpec("all", frozenset({"--file"})),
    "git rebase": _ShellMutatorSpec("all", frozenset({"--onto", "--exec", "--strategy"})),
    "git stash": _ShellMutatorSpec("all", frozenset({"-m", "--message", "--pathspec-from-file"})),
    "git switch": _ShellMutatorSpec("all", frozenset({"-c", "-C", "--create", "--discard-changes"})),
    "git reset": _ShellMutatorSpec("all", frozenset({"--pathspec-from-file", "--pathspec-file-nul"})),
    "git tag": _ShellMutatorSpec("all", frozenset({"-m", "--message", "-F", "--file"})),
    "git restore": _ShellMutatorSpec("all", frozenset({"--source", "-s", "--staged", "--worktree", "--conflict"})),
    "git checkout": _ShellMutatorSpec("all", frozenset({"-b", "-B", "--orphan", "--conflict"})),
    "git clean": _ShellMutatorSpec("all"),
    "git clone": _ShellMutatorSpec("last", frozenset({"--template", "-b", "--branch", "--config"})),
    "git init": _ShellMutatorSpec("all", frozenset({"--template", "-b", "--separate-git-dir"})),
    "git worktree": _ShellMutatorSpec("first", frozenset({"-b", "-B", "--orphan"})),
}

_SHELL_COMMAND_WRAPPERS = frozenset({"command", "exec", "env", "nohup", "sudo", "doas", "builtin"})
_WRAPPER_VALUE_OPTIONS = frozenset({"-u", "--user", "-g", "--group", "-C", "--chdir"})
_GIT_VALUE_OPTIONS = frozenset({"-C", "--git-dir", "--work-tree", "--namespace"})


def _extract_mutator_targets(tokens: list[str]) -> tuple[str, ...]:
    located = _locate_declared_mutator(tokens)
    if located is None:
        return ()
    start, spec = located
    operands, option_targets = _parse_mutator_operands(tokens, start, spec)
    if option_targets:
        return tuple(option_targets)
    if spec.target_mode == "last":
        return (operands[-1],) if len(operands) >= 2 else ()
    if spec.target_mode == "first":
        return (operands[0],) if operands else ()
    if spec.target_mode == "pair":
        return tuple(operands) if len(operands) >= 2 else ()
    return tuple(operands[spec.leading_operands :])


def _locate_declared_mutator(tokens: list[str]) -> tuple[int, _ShellMutatorSpec] | None:
    index = 0
    while index < len(tokens):
        normalized = _normalize_command_token(tokens[index])
        if not normalized or ("=" in normalized and not normalized.startswith("-")):
            index += 1
            continue
        command = normalized.rsplit("/", 1)[-1]
        if command in _SHELL_COMMAND_WRAPPERS:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                option, inline = _option_parts(tokens[index])
                index += 1
                if inline is None and option in _WRAPPER_VALUE_OPTIONS:
                    index += 1
            continue
        if command == "git":
            index += 1
            while index < len(tokens):
                option, inline = _option_parts(tokens[index])
                if tokens[index].startswith("-"):
                    index += 1
                    if inline is None and option in _GIT_VALUE_OPTIONS:
                        index += 1
                    continue
                break
            if index >= len(tokens):
                return None
            subcommand = _normalize_command_token(tokens[index])
            spec = _MUTATOR_SPECS.get(f"git {subcommand}")
            return (index + 1, spec) if spec is not None else None
        spec = _MUTATOR_SPECS.get(command)
        return (index + 1, spec) if spec is not None else None
    return None


def _parse_mutator_operands(tokens: list[str], start: int, spec: _ShellMutatorSpec) -> tuple[list[str], list[str]]:
    operands: list[str] = []
    option_targets: list[str] = []
    index = start
    options_done = False
    while index < len(tokens):
        token = _normalize_shell_target_token(tokens[index])
        if not token:
            index += 1
            continue
        if _has_shell_output_redirection(token):
            if token in {">", ">>", "1>", "1>>", "2>", "2>>"} and index + 1 < len(tokens):
                index += 1
            index += 1
            continue
        if not options_done and token == "--":
            options_done = True
            index += 1
            continue
        if not options_done and token.startswith("-") and token != "-":
            option, inline_value = _option_parts(token)
            if option in spec.target_options:
                value = inline_value
                if value is None and index + 1 < len(tokens):
                    index += 1
                    value = _normalize_shell_target_token(tokens[index])
                if value:
                    option_targets.append(value)
            elif inline_value is None and option in spec.value_options:
                index += 1
            index += 1
            continue
        operands.append(token)
        index += 1
    return operands, option_targets


def _normalize_shell_target_token(token: str) -> str:
    return token.strip().strip("\"'`")


def _option_parts(token: str) -> tuple[str, str | None]:
    stripped = token.strip().strip("\"'`")
    if not stripped.startswith("-"):
        return (stripped, None)
    if "=" in stripped:
        option, value = stripped.split("=", 1)
        return option, value
    if stripped.startswith("-") and not stripped.startswith("--") and len(stripped) > 2:
        for option in ("-t", "-o", "-g", "-m", "-S", "-s", "-r", "-C"):
            if stripped.startswith(option):
                return option, stripped[len(option) :]
    return stripped, None


def _extract_output_path_candidates(tokens: list[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    for index, token in enumerate(tokens):
        value = _normalize_shell_path_token(token)
        if value and _is_shell_explicit_output_path_candidate(tokens, index, value):
            candidates.append(value)

    return tuple(candidates)


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
    "ShellExecutionPolicy",
    "extract_shell_path_candidates",
    "non_interactive_shell_env",
    "resolve_shell_execution_policy",
]
