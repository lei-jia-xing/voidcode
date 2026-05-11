from __future__ import annotations

import pytest

from voidcode.security.shell_policy import (
    classify_shell_command,
    extract_shell_path_candidates,
    non_interactive_shell_env,
    resolve_shell_command_policy,
)


@pytest.mark.parametrize(
    ("command", "category"),
    [
        ("rg shell_policy src", "readonly"),
        ("uv run pytest tests/unit -q", "package_manager"),
        ("pytest tests/unit -q", "build_or_test"),
        ("vim README.md", "interactive"),
        ("printf alpha > out.txt", "mutating"),
        ("rm -rf build", "destructive"),
        ("custom-tool --flag", "unknown"),
    ],
)
def test_classify_shell_command_categories(command: str, category: str) -> None:
    assert classify_shell_command(command).category == category


def test_classify_shell_command_uses_highest_risk_segment() -> None:
    classification = classify_shell_command("pwd && rm -rf build")

    assert classification.category == "destructive"
    assert [segment.category for segment in classification.segments] == ["readonly", "destructive"]


@pytest.mark.parametrize(
    ("command", "category", "segment_categories"),
    [
        ("pwd&&rm -rf build", "destructive", ["readonly", "destructive"]),
        ("pwd;rm -rf build", "destructive", ["readonly", "destructive"]),
        ("rg x|rm -rf build", "destructive", ["readonly", "destructive"]),
        ("echo hi>out.txt", "mutating", ["mutating"]),
    ],
)
def test_classify_shell_command_handles_compact_operators(
    command: str,
    category: str,
    segment_categories: list[str],
) -> None:
    classification = classify_shell_command(command)

    assert classification.category == category
    assert [segment.category for segment in classification.segments] == segment_categories


@pytest.mark.parametrize("command", ["vim README.md", "nvim README.md", "less README.md"])
def test_resolve_shell_command_policy_denies_interactive_non_interactive_commands(
    command: str,
) -> None:
    decision = resolve_shell_command_policy(command, non_interactive=True)

    assert decision.allowed is False
    assert decision.reason is not None
    assert "interactive/TUI commands" in decision.reason
    assert "interactive shell surface" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "npm install",
        "touch generated.txt",
        "rm generated.txt",
        "pwd&&rm -rf build",
        "pwd;rm -rf build",
        "rg x|rm -rf build",
        "echo hi>out.txt",
    ],
)
def test_resolve_shell_command_policy_denies_risky_commands_in_read_only(command: str) -> None:
    decision = resolve_shell_command_policy(command, read_only=True)

    assert decision.allowed is False
    assert decision.reason is not None
    assert "read-only runtime policy denies shell commands" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        'python -c \'from pathlib import Path; Path("generated.txt").write_text("bad")\'',
        'bash -lc "rm generated.txt"',
    ],
)
def test_resolve_shell_command_policy_denies_unknown_commands_in_read_only(
    command: str,
) -> None:
    decision = resolve_shell_command_policy(command, read_only=True)

    assert classify_shell_command(command).category == "unknown"
    assert decision.allowed is False
    assert decision.reason is not None
    assert "read-only runtime policy denies shell commands classified as unknown" in decision.reason


@pytest.mark.parametrize(
    "command",
    [
        "pwd",
        "ls",
        "rg shell_policy src",
        "git status",
    ],
)
def test_resolve_shell_command_policy_allows_readonly_commands_in_read_only(
    command: str,
) -> None:
    assert resolve_shell_command_policy(command, read_only=True).allowed is True


@pytest.mark.parametrize("command", ["npm install", "pnpm install", "yarn install", "bun install"])
def test_resolve_shell_command_policy_records_injected_env_key_names(
    command: str,
) -> None:
    decision = resolve_shell_command_policy(command)

    assert decision.allowed is True
    assert decision.injected_env_keys == (
        "CI",
        "NPM_CONFIG_YES",
        "YARN_ENABLE_IMMUTABLE_INSTALLS",
    )
    assert set(non_interactive_shell_env(command)) == set(decision.injected_env_keys)


@pytest.mark.parametrize("command", ["npm install", "sudo npm install", "apt install curl"])
def test_denied_shell_command_policy_records_no_injected_env_key_names(command: str) -> None:
    decision = resolve_shell_command_policy(command, read_only=True)

    assert decision.allowed is False
    assert decision.injected_env_keys == ()


def test_shell_policy_owns_shell_path_candidate_extraction() -> None:
    assert extract_shell_path_candidates("tool --output=./../out.txt") == ("./../out.txt",)
    assert extract_shell_path_candidates("touch /tmp/out.txt") == ()
