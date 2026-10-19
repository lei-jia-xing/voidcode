"""Root options are accepted on either side of the subcommand.

`argparse` only binds root options before the subcommand, but `voidcode serve --workspace .` is
what a user types (and what the replaced CLI accepted), so the kernel CLI hoists them.
"""

from __future__ import annotations

import pytest

from voidcode.kernel.__main__ import _normalized_argv


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        # Already canonical: nothing moves.
        (["--workspace", "/w", "serve", "--port", "8000"], ["--workspace", "/w", "serve", "--port", "8000"]),
        # The root pair after the command is hoisted, the command's own flags stay.
        (["serve", "--workspace", "/w", "--port", "8000"], ["--workspace", "/w", "serve", "--port", "8000"]),
        # `--option=value` and repeated root options keep their shape and order.
        (
            ["run", "prompt", "--state-dir=/s", "--trust-plugin", "counter", "--trust-plugin", "extra"],
            ["--state-dir=/s", "--trust-plugin", "counter", "--trust-plugin", "extra", "run", "prompt"],
        ),
        # No subcommand (e.g. --version): untouched.
        (["--version"], ["--version"]),
        (["--workspace", "/w", "sessions", "--limit", "5"], ["--workspace", "/w", "sessions", "--limit", "5"]),
    ],
)
def test_root_options_are_accepted_on_either_side(argv: list[str], expected: list[str]) -> None:
    assert _normalized_argv(argv) == expected


def test_normalized_argv_does_not_mutate_its_input() -> None:
    argv = ["serve", "--workspace", "/w"]
    _normalized_argv(argv)
    assert argv == ["serve", "--workspace", "/w"]
