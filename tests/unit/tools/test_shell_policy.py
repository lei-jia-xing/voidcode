from __future__ import annotations

import pytest

from voidcode.security.shell_policy import (
    extract_shell_path_candidates,
    non_interactive_shell_env,
)


@pytest.mark.parametrize("command", ["npm install", "pnpm install", "yarn install", "bun install"])
def test_non_interactive_shell_env_for_package_managers(command: str) -> None:
    assert non_interactive_shell_env(command) == {
        "CI": "1",
        "NPM_CONFIG_YES": "true",
        "YARN_ENABLE_IMMUTABLE_INSTALLS": "false",
    }


@pytest.mark.parametrize("command", ["ls", "pwd", "echo hello", "python -c 'print(1)'"])
def test_non_interactive_shell_env_is_empty_for_other_commands(command: str) -> None:
    assert non_interactive_shell_env(command) == {}


def test_shell_policy_owns_shell_path_candidate_extraction() -> None:
    assert extract_shell_path_candidates("tool --output=./../out.txt") == ("./../out.txt",)
    assert extract_shell_path_candidates("touch /tmp/out.txt") == ()
