"""``voidcode commands``, ``voidcode agents``, and ``voidcode mcp``: capability discovery."""

from __future__ import annotations

from pathlib import Path

import click

from ...cli_support import (
    EXIT_INVALID_COMMAND,
    serialize_command_definition,
    serialize_command_summary,
)
from ...command.loader import load_command_registry
from ...command.registry import CommandRegistry
from ...runtime.contracts import AgentSummary
from ..errors import CliError, require_workspace
from ..handler_args import AgentsArgs, CommandsArgs, McpArgs
from ..options import command_discovery_options, json_option, workspace_option
from ..output import emit_output, format_named_record, mcp_status_payload
from ..runtime_gateway import open_runtime, runtime_error_boundary


def _serialize_agent_summary(summary: AgentSummary) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": summary.id,
        "label": summary.label,
        "description": summary.description,
        "mode": summary.mode,
        "selectable": summary.selectable,
        "configured": summary.configured,
        "model": summary.model,
        "model_label": summary.model_label,
        "model_source": summary.model_source,
        "provider": summary.provider,
        "fallback_chain": list(summary.fallback_chain),
    }
    if summary.source_scope is not None:
        payload["source_scope"] = summary.source_scope
    if summary.source_path is not None:
        payload["source_path"] = summary.source_path
    return payload


def _handle_agents_list_command(args: AgentsArgs) -> int:
    workspace = args.workspace
    require_workspace(workspace)

    with open_runtime(workspace) as runtime:
        summaries = runtime.list_agent_summaries()

    payload = {
        "workspace": str(workspace),
        "agents": [_serialize_agent_summary(summary) for summary in summaries],
    }

    def _print_agents() -> None:
        for summary in summaries:
            fields: list[tuple[str, object]] = [
                ("id", summary.id),
                ("label", summary.label),
                ("mode", summary.mode),
                ("selectable", summary.selectable),
                ("configured", summary.configured),
                ("model", summary.model),
                ("provider", summary.provider),
            ]
            if summary.source_scope is not None:
                fields.append(("source_scope", summary.source_scope))
            if summary.source_path is not None:
                fields.append(("source_path", summary.source_path))
            print(format_named_record("AGENT", fields))

    return emit_output(args, payload, _print_agents)


def _handle_mcp_list_command(args: McpArgs) -> int:
    workspace = args.workspace
    require_workspace(workspace)

    with open_runtime(workspace) as runtime:
        status = runtime.current_status()

    payload = {
        "workspace": str(workspace),
        "mcp": mcp_status_payload(status.mcp),
    }

    def _print_mcp() -> None:
        details = status.mcp.details
        servers = details["servers"]
        assert isinstance(servers, list)
        print(
            format_named_record(
                "MCP",
                [
                    ("state", status.mcp.state),
                    ("mode", details["mode"]),
                    ("configured", details["configured"]),
                    ("configured_enabled", details["configured_enabled"]),
                    ("configured_server_count", details["configured_server_count"]),
                    ("running_server_count", details["running_server_count"]),
                    ("failed_server_count", details["failed_server_count"]),
                ],
            )
        )
        for item in servers:
            assert isinstance(item, dict)
            print(
                format_named_record(
                    "MCP_SERVER",
                    [
                        ("name", item.get("server")),
                        ("status", item.get("status")),
                        ("transport", item.get("transport")),
                        ("command", repr(item.get("command", []))),
                        ("stage", item.get("stage")),
                        ("error", repr(item.get("error"))),
                    ],
                )
            )

    return emit_output(args, payload, _print_mcp)


def _handle_commands_list_command(args: CommandsArgs) -> int:
    workspace = args.workspace
    registry = _load_cli_command_registry(args, workspace=workspace)
    commands = registry.list(
        include_hidden=args.include_hidden,
        include_disabled=args.include_disabled,
    )

    def _print_commands() -> None:
        for command in commands:
            print(
                format_named_record(
                    "COMMAND",
                    [
                        ("name", f"/{command.name}"),
                        ("source", command.source),
                        ("enabled", command.enabled),
                        ("description", repr(command.description)),
                    ],
                )
            )

    return emit_output(
        args,
        {
            "workspace": str(workspace),
            "commands": [serialize_command_summary(command) for command in commands],
        },
        _print_commands,
    )


def _handle_commands_show_command(args: CommandsArgs) -> int:
    workspace = args.workspace
    registry = _load_cli_command_registry(args, workspace=workspace)
    command_name = args.name
    assert command_name is not None
    command = registry.get(command_name)
    if command is None:
        raise CliError(
            code=EXIT_INVALID_COMMAND,
            message=f"unknown command: /{command_name.removeprefix('/')}",
        )
    if command.hidden and not args.include_hidden:
        raise CliError(code=EXIT_INVALID_COMMAND, message=f"unknown command: /{command.name}")
    if not command.enabled and not args.include_disabled:
        raise CliError(code=EXIT_INVALID_COMMAND, message=f"command is disabled: /{command.name}")

    payload = serialize_command_definition(command)

    def _print_command() -> None:
        print(f"/{command.name}")
        print(f"Source: {command.source}")
        print(f"Enabled: {command.enabled}")
        print(f"Description: {command.description}")
        if command.path is not None:
            print(f"Path: {command.path}")
        print("Template:")
        print(command.template, end="" if command.template.endswith("\n") else "\n")

    return emit_output(args, payload, _print_command)


def _load_cli_command_registry(args: CommandsArgs, *, workspace: Path) -> CommandRegistry:
    user_commands_dir = args.user_commands_dir
    with runtime_error_boundary():
        return load_command_registry(workspace=workspace, user_commands_dir=user_commands_dir)


@click.group(help="Discover prompt commands available to runtime requests.")
def commands() -> None:
    pass


@commands.command(name="list", help="List enabled prompt commands discovered for a workspace.")
@command_discovery_options
@click.option("--include-hidden", is_flag=True)
@click.option("--include-disabled", is_flag=True)
@json_option("Output discovered commands as JSON.")
def commands_list(
    workspace: Path,
    user_commands_dir: Path | None,
    include_hidden: bool,
    include_disabled: bool,
    json_output: bool,
) -> int:
    return _handle_commands_list_command(
        CommandsArgs(
            workspace=workspace,
            user_commands_dir=user_commands_dir,
            include_hidden=include_hidden,
            include_disabled=include_disabled,
            json=json_output,
        )
    )


@commands.command(
    name="show",
    help="Show one prompt command definition and rendered template source.",
)
@click.argument("name")
@command_discovery_options
@click.option("--include-hidden", is_flag=True)
@click.option("--include-disabled", is_flag=True)
@json_option("Output the command definition as JSON.")
def commands_show(
    name: str,
    workspace: Path,
    user_commands_dir: Path | None,
    include_hidden: bool,
    include_disabled: bool,
    json_output: bool,
) -> int:
    return _handle_commands_show_command(
        CommandsArgs(
            name=name,
            workspace=workspace,
            user_commands_dir=user_commands_dir,
            include_hidden=include_hidden,
            include_disabled=include_disabled,
            json=json_output,
        )
    )


@click.group(help="Discover built-in and local custom agents available to runtime requests.")
def agents() -> None:
    pass


@agents.command(
    name="list",
    help="List built-in and local custom agent manifests discovered for a workspace.",
)
@workspace_option("Workspace root used to discover project-local agents.")
@json_option("Output discovered agents as JSON.")
def agents_list(workspace: Path, json_output: bool) -> int:
    return _handle_agents_list_command(
        AgentsArgs(
            workspace=workspace,
            json=json_output,
        )
    )


@click.group(help="Inspect runtime-managed MCP configuration and health.")
def mcp() -> None:
    pass


@mcp.command(name="list", help="List configured MCP servers and passive runtime status.")
@workspace_option("Workspace root used to resolve runtime config and MCP state.")
@json_option("Output MCP status as JSON.")
def mcp_list(workspace: Path, json_output: bool) -> int:
    return _handle_mcp_list_command(
        McpArgs(
            workspace=workspace,
            json=json_output,
        )
    )
