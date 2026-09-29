"""``voidcode tui``: the interactive inline client."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import click

from ...runtime.permission import ApprovalMode
from ..handler_args import TuiArgs
from ..options import APPROVAL_MODES, workspace_option


def _handle_tui_command(args: TuiArgs) -> int:
    workspace = args.workspace
    # CLI boundary: click.Choice(APPROVAL_MODES) guarantees an ApprovalMode literal.
    approval_mode: ApprovalMode | None = cast(ApprovalMode | None, args.approval_mode)

    from ...tui import run_tui

    return run_tui(workspace=workspace, approval_mode=approval_mode)


@click.command(name="tui", help="Run the VoidCode interactive inline UI.")
@workspace_option("Workspace root used to resolve relative read paths.")
@click.option(
    "--approval-mode",
    type=click.Choice(APPROVAL_MODES),
    help="Override the runtime approval mode for this invocation.",
)
def tui(workspace: Path, approval_mode: str | None) -> int:
    return _handle_tui_command(
        TuiArgs(
            workspace=workspace,
            approval_mode=approval_mode,
        )
    )
