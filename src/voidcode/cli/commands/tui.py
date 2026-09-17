"""``voidcode tui``: the interactive Textual client."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import click

from ...runtime.permission import PermissionDecision
from ..handler_args import TuiArgs
from ..options import APPROVAL_MODES, workspace_option


class TuiAppProtocol(Protocol):
    def run(self) -> None: ...


def _handle_tui_command(args: TuiArgs) -> int:
    workspace = args.workspace
    approval_mode: PermissionDecision | None = cast(PermissionDecision | None, args.approval_mode)

    from ...tui import VoidCodeTUI

    app = cast(TuiAppProtocol, VoidCodeTUI(workspace=workspace, approval_mode=approval_mode))
    app.run()
    return 0


@click.command(name="tui", help="Run the VoidCode interactive Textual UI.")
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
