"""Shared Click option decorators and choice tuples for the CLI surface."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click

APPROVAL_MODES = ("allow", "deny", "ask")


APPROVAL_DECISIONS = ("allow", "deny")


BUNDLE_FORMATS = ("zip", "json")


def workspace_option(help_text: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
    return click.option(
        "--workspace",
        type=click.Path(path_type=Path),
        default=Path.cwd,
        show_default=False,
        help=help_text,
    )


def json_option(help_text: str) -> Callable[[Callable[..., object]], Callable[..., object]]:
    return click.option("--json", "json_output", is_flag=True, help=help_text)


def show_thinking_option(
    help_text: str,
) -> Callable[[Callable[..., object]], Callable[..., object]]:
    return click.option("--show-thinking", is_flag=True, help=help_text)


def command_discovery_options(function: Callable[..., object]) -> Callable[..., object]:
    function = workspace_option("Workspace root used to discover project-local commands.")(function)
    return click.option(
        "--user-commands-dir",
        type=click.Path(path_type=Path),
        help="Optional user command directory to merge before project commands.",
    )(function)
