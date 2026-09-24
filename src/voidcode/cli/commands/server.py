"""Long-running CLI entrypoints: ACP stdio, the HTTP transport, and the web launcher."""

from __future__ import annotations

from pathlib import Path
from typing import cast

import click

from ...acp.stdio import StdioAcpServer
from ...cli_support import EXIT_SUCCESS
from ...runtime.permission import PermissionDecision
from ...server import serve, web
from ..handler_args import AcpArgs
from ..options import APPROVAL_MODES, workspace_option
from ..runtime_gateway import load_cli_config, open_runtime


def _handle_acp_command(args: AcpArgs) -> int:
    workspace = args.workspace
    # CLI boundary: click.Choice(APPROVAL_MODES) guarantees a PermissionDecision literal.
    acp_approval_mode: PermissionDecision | None = cast(PermissionDecision | None, args.approval_mode)
    config = load_cli_config(workspace, approval_mode=acp_approval_mode)
    with open_runtime(workspace, config) as runtime:
        server = StdioAcpServer(runtime=runtime, workspace=workspace)
        return server.serve()


@click.command(name="acp", help="Run the minimal external-facing ACP stdio JSON-RPC facade.")
@workspace_option("Workspace root used by the ACP-backed runtime session database.")
@click.option(
    "--approval-mode",
    type=click.Choice(APPROVAL_MODES),
    help="Override the runtime approval mode for this ACP process.",
)
def acp(workspace: Path, approval_mode: str | None) -> int:
    return _handle_acp_command(
        AcpArgs(
            workspace=workspace,
            approval_mode=approval_mode,
        )
    )


@click.command(name="serve", help="Serve the local HTTP runtime transport.")
@workspace_option("Workspace root used by the local runtime and session database.")
@click.option("--host", default="127.0.0.1", help="Host interface for the local transport server.")
@click.option("--port", type=int, default=8000, help="Port for the local transport server.")
@click.option(
    "--approval-mode",
    type=click.Choice(APPROVAL_MODES),
    help="Override the runtime approval mode for this server process.",
)
def serve_command(workspace: Path, host: str, port: int, approval_mode: str | None) -> int:
    # CLI boundary: click.Choice(APPROVAL_MODES) guarantees a PermissionDecision literal.
    server_approval_mode: PermissionDecision | None = cast(PermissionDecision | None, approval_mode)
    config = load_cli_config(workspace, approval_mode=server_approval_mode)
    serve(workspace=workspace, host=host, port=port, config=config)
    return EXIT_SUCCESS


@click.command(
    name="web",
    help="Start the local web launcher entrypoint for the runtime transport.",
)
@workspace_option("Workspace root used by the local runtime and session database.")
@click.option("--host", default="127.0.0.1", help="Host interface for the local launcher server.")
@click.option(
    "--port",
    type=int,
    default=None,
    help="Port for the local launcher server. Defaults to an auto-assigned local port.",
)
@click.option(
    "--approval-mode",
    type=click.Choice(APPROVAL_MODES),
    help="Override the runtime approval mode for this launcher process.",
)
@click.option(
    "--no-open",
    "open_browser",
    flag_value=False,
    default=True,
    help="Start the web launcher without opening a browser window.",
)
def web_command(
    workspace: Path,
    host: str,
    port: int | None,
    approval_mode: str | None,
    open_browser: bool,
) -> int:
    # CLI boundary: click.Choice(APPROVAL_MODES) guarantees a PermissionDecision literal.
    server_approval_mode: PermissionDecision | None = cast(PermissionDecision | None, approval_mode)
    config = load_cli_config(workspace, approval_mode=server_approval_mode)
    web(workspace=workspace, host=host, port=port, config=config, open_browser=open_browser)
    return EXIT_SUCCESS
