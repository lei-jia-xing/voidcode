"""``voidcode doctor``: capability readiness for external tools, formatters, LSP, and MCP."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from ...cli_support import EXIT_CONFIG_ERROR, EXIT_RUNTIME_ERROR, EXIT_SUCCESS, EXIT_USAGE_ERROR
from ...doctor import (
    CapabilityCheckResult,
    CapabilityCheckStatus,
    CapabilityDoctor,
    DoctorCheckType,
    create_doctor_for_config,
    create_report,
    format_report,
    format_report_json,
)
from ...runtime.config import RUNTIME_CONFIG_FILE_NAME, RuntimeConfig, load_runtime_config
from ...runtime.config_schema import generate_starter_runtime_config, write_runtime_config_payload
from ..errors import CliError
from ..handler_args import DoctorArgs
from ..options import json_option, workspace_option


def _handle_doctor_command(args: DoctorArgs) -> int:
    """Run the capability doctor to check external tool readiness."""
    workspace = args.workspace
    verbose = args.verbose
    json_output = args.json

    # Load runtime config to get all capability settings
    config_error: str | None = None
    config: RuntimeConfig | None = None
    results: list[CapabilityCheckResult] = []
    if args.fix:
        if args.model is None or not args.model.strip():
            raise CliError(code=EXIT_USAGE_ERROR, message="doctor --fix requires --model provider/model")
        config_path = workspace.resolve() / RUNTIME_CONFIG_FILE_NAME
        if config_path.exists():
            raise CliError(code=EXIT_CONFIG_ERROR, message=f"runtime config already exists: {config_path}; edit it or run config init --force")
        payload = generate_starter_runtime_config(model=args.model)
        written_path = write_runtime_config_payload(workspace, payload)
        print(json.dumps({"config_path": str(written_path), "next_command": f"voidcode doctor --workspace {workspace}"}))
        return EXIT_SUCCESS
    try:
        config = load_runtime_config(workspace)
    except ValueError as exc:
        # Config file has a parse/validation error - report it but continue
        # with minimal checks so the user can still see what's wrong.
        config_error = str(exc)
        doctor = CapabilityDoctor(workspace=workspace)
        doctor.add_executable_check("ast-grep", "ast-grep")
        results = doctor.results
        results.append(
            CapabilityCheckResult(
                status=CapabilityCheckStatus.ERROR,
                name="runtime.config",
                check_type=DoctorCheckType.RUNTIME_CONFIG.value,
                error_message=config_error,
            )
        )
    except Exception:
        # OSError (permissions, path not found) and other unexpected errors
        # should propagate so they are not silently swallowed.
        raise

    if config_error is not None:
        print(f"WARN runtime config error: {config_error}", file=sys.stderr, flush=True)

    if config is not None:
        # Create doctor with full config
        doctor = create_doctor_for_config(workspace, config)
        results = doctor.run_all_checks()

    # Create and format report
    report = create_report(results, workspace=workspace)

    if json_output:
        print(format_report_json(report))
    else:
        print(format_report(report, verbose=verbose))

    # Return 0 only when healthy and runtime config parsed successfully.
    return EXIT_SUCCESS if (report.is_healthy and config_error is None) else EXIT_RUNTIME_ERROR


@click.command(name="doctor", help="Check runtime capability readiness (external tools, formatters, LSP, MCP).")
@workspace_option("Workspace root used to resolve runtime config.")
@click.option("--verbose", "verbose", "-v", is_flag=True)
@click.option("--fix", is_flag=True, help="Create a starter runtime config when none exists.")
@click.option("--model", type=str, help="Provider/model used with --fix, for example openai/gpt-4o.")
@json_option("Output report in JSON format.")
def doctor(workspace: Path, verbose: bool, fix: bool, model: str | None, json_output: bool) -> int:
    return _handle_doctor_command(
        DoctorArgs(
            workspace=workspace,
            verbose=verbose,
            json=json_output,
            fix=fix,
            model=model,
        )
    )
