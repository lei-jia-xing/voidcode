"""``voidcode config``: inspect effective runtime configuration and scaffold a workspace config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import click

from ...cli_support import EXIT_CONFIG_ERROR, EXIT_SUCCESS, print_json
from ...provider.snapshot import resolved_provider_snapshot
from ...runtime.config import RUNTIME_CONFIG_FILE_NAME, serialize_runtime_agent_config
from ...runtime.config_schema import (
    format_starter_runtime_config_json,
    generate_starter_runtime_config,
    runtime_config_json_schema,
    write_runtime_config_payload,
)
from ...runtime.permission import DEFAULT_APPROVAL_MODE, ApprovalMode
from ..errors import CliError, require_workspace
from ..handler_args import ConfigArgs
from ..options import APPROVAL_MODES, workspace_option
from ..output import mcp_status_payload
from ..provider_view import provider_readiness_payload
from ..runtime_gateway import open_runtime, runtime_error_boundary


def _handle_config_show_command(args: ConfigArgs) -> int:
    workspace = args.workspace
    require_workspace(workspace)

    session_id = args.session_id
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        effective_config = runtime.effective_runtime_config(session_id=session_id)
        readiness = runtime.provider_readiness(session_id=session_id)
        agents = runtime.effective_agent_model_config(session_id=session_id)
        status = runtime.current_status()

    print_json(
        {
            "workspace": str(workspace),
            "session_id": session_id,
            "approval_mode": effective_config.approval_mode,
            "execution_engine": effective_config.execution_engine,
            "model": effective_config.model,
            "fallback_models": (list(effective_config.provider_fallback.fallback_models) if effective_config.provider_fallback is not None else []),
            "reasoning_effort": effective_config.reasoning_effort,
            "agent": serialize_runtime_agent_config(effective_config.agent),
            "agents": agents,
            "resolved_provider": resolved_provider_snapshot(effective_config.resolved_provider),
            "provider_readiness": provider_readiness_payload(readiness),
            "context_budget": {
                "context_window": readiness.context_window,
                "max_output_tokens": readiness.max_output_tokens,
            },
            "mcp": mcp_status_payload(status.mcp),
        }
    )
    return EXIT_SUCCESS


def _handle_config_schema_command(args: ConfigArgs) -> int:
    _ = args
    print(json.dumps(runtime_config_json_schema(), indent=2, sort_keys=True))
    return 0


def _handle_config_init_command(args: ConfigArgs) -> int:
    workspace = args.workspace
    require_workspace(workspace)

    # CLI boundary: click.Choice(APPROVAL_MODES) guarantees an ApprovalMode literal.
    approval_mode: ApprovalMode = cast(ApprovalMode, args.approval_mode)
    with runtime_error_boundary():
        payload = generate_starter_runtime_config(
            approval_mode=approval_mode,
            model=args.model,
            include_examples=args.with_examples,
        )
    if args.print:
        print(format_starter_runtime_config_json(payload), end="")
        return 0

    config_path = workspace.resolve() / RUNTIME_CONFIG_FILE_NAME
    if config_path.exists() and not args.force:
        raise CliError(
            code=EXIT_CONFIG_ERROR,
            message=f"runtime config already exists: {config_path}; pass --force to overwrite",
        )
    written_path = write_runtime_config_payload(workspace, payload)
    print(
        json.dumps(
            {
                "workspace": str(workspace),
                "config_path": str(written_path),
                "next_command": f"voidcode doctor --workspace {workspace}",
                "first_task_command": f'voidcode run "read README.md" --workspace {workspace}',
            }
        )
    )
    return 0


@click.group(help="Inspect effective runtime configuration.")
def config() -> None:
    pass


@config.command(name="show", help="Show effective runtime config for a workspace or session.")
@workspace_option("Workspace root used to resolve runtime config and sessions.")
@click.option("--session", "session_id")
def config_show(workspace: Path, session_id: str | None) -> int:
    return _handle_config_show_command(
        ConfigArgs(
            workspace=workspace,
            session_id=session_id,
            json=True,
        )
    )


@config.command(name="schema", help="Print the JSON Schema for .voidcode.json.")
def config_schema() -> int:
    return _handle_config_schema_command(ConfigArgs())


@config.command(
    name="init",
    help="Generate a starter workspace .voidcode.json. Note: yolo/write auto-approve execution without isolation (no OS-level sandbox in v1).",
)
@workspace_option("Workspace root where .voidcode.json should be generated.")
@click.option("--approval-mode", type=click.Choice(APPROVAL_MODES), default=DEFAULT_APPROVAL_MODE)
@click.option("--model")
@click.option("--with-examples", is_flag=True)
@click.option("--print", "print_config", is_flag=True)
@click.option("--force", is_flag=True)
def config_init(
    workspace: Path,
    approval_mode: str,
    model: str | None,
    with_examples: bool,
    print_config: bool,
    force: bool,
) -> int:
    return _handle_config_init_command(
        ConfigArgs(
            workspace=workspace,
            approval_mode=approval_mode,
            model=model,
            with_examples=with_examples,
            print=print_config,
            force=force,
        )
    )
