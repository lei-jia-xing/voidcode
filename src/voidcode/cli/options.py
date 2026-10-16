"""Shared Click option decorators and choice tuples for the CLI surface."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import click

from ..runtime.permissions.policy import APPROVAL_MODES as APPROVAL_MODES

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
