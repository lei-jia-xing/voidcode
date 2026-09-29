"""``voidcode run``: one runtime request through the provider or deterministic harness."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

import click

from ...cli_support import EXIT_RUNTIME_ERROR, EXIT_SUCCESS, EXIT_USAGE_ERROR, print_json
from ...runtime.contracts import RuntimeRequest, validate_runtime_request_metadata
from ...runtime.permission import ApprovalMode
from ..errors import CliError
from ..handler_args import RunArgs
from ..options import APPROVAL_MODES, json_option, show_thinking_option, workspace_option
from ..output import print_plain_runtime_output
from ..presentation import print_noninteractive_blocked, print_runtime_failure_footer, runtime_stream_payload
from ..readiness import config_error_readiness_payload, print_readiness_failure, run_readiness_preflight
from ..runtime_gateway import (
    RuntimeConfigKwargs,
    blocked_exit_code,
    incomplete_runtime_stream_message,
    last_event,
    load_cli_config,
    open_runtime,
    pending_blocked_event,
    run_with_inline_approval,
    runtime_error_boundary,
)
from ..trace import print_trace_blocked, print_trace_final


def _handle_run_command(args: RunArgs) -> int:
    workspace = args.workspace
    request_text = args.request
    json_output = args.json
    trace_output = args.trace
    if json_output and trace_output:
        raise CliError(code=EXIT_USAGE_ERROR, message="--json and --trace cannot be used together")
    show_thinking = args.show_thinking
    cli_reasoning_effort = args.reasoning_effort
    cli_model = args.model
    # CLI boundary: click.Choice(APPROVAL_MODES) guarantees an ApprovalMode literal.
    approval_mode: ApprovalMode | None = cast(ApprovalMode | None, args.approval_mode)
    config_kwargs: RuntimeConfigKwargs = {
        "approval_mode": approval_mode,
        "reasoning_effort": cli_reasoning_effort,
    }
    if cli_model is not None:
        config_kwargs["model"] = cli_model
    try:
        config = load_cli_config(workspace, **config_kwargs)
    except CliError as exc:
        payload = config_error_readiness_payload(exc.message, workspace=workspace)
        if json_output:
            print_json(payload)
        else:
            print_readiness_failure(payload)
        return exc.code
    with open_runtime(workspace, config) as runtime:
        if config.execution_engine == "provider":
            try:
                readiness = runtime.provider_readiness()
            except ValueError:
                # Preserve the existing runtime error path for invalid effective
                # session/config state; the gate only handles typed readiness.
                readiness = None
            if readiness is not None:
                preflight_exit = run_readiness_preflight(
                    readiness=readiness,
                    workspace=workspace,
                    json_output=json_output,
                )
                if preflight_exit is not None:
                    return preflight_exit
        metadata: dict[str, object] = {}
        if args.agent is not None:
            metadata["agent"] = {"preset": args.agent}
        if args.skills:
            metadata["skills"] = list(args.skills)
        if args.runtime_mode is not None:
            metadata["mode"] = args.runtime_mode
        if args.read_only:
            metadata["read_only"] = True
        if cli_reasoning_effort is not None:
            metadata["reasoning_effort"] = cli_reasoning_effort
        provider_stream = args.provider_stream
        if provider_stream is not None:
            metadata["provider_stream"] = provider_stream
        elif trace_output:
            metadata["provider_stream"] = True
        request = RuntimeRequest(
            prompt=request_text,
            session_id=args.session_id,
            metadata=validate_runtime_request_metadata(metadata),
        )
        interactive = sys.stdin.isatty() and sys.stderr.isatty()
        try:
            with runtime_error_boundary():
                result = run_with_inline_approval(
                    runtime,
                    request,
                    interactive=interactive,
                    emit_events=interactive and not json_output and not trace_output,
                    trace_events=trace_output,
                    show_thinking=show_thinking,
                )
        except KeyboardInterrupt:
            print("Interrupted current run.", file=sys.stderr)
            return 130

        incomplete_stream_message = incomplete_runtime_stream_message(result)
        if incomplete_stream_message is not None:
            if trace_output:
                print(f"\n✖ Failed: {incomplete_stream_message}", flush=True)
            else:
                print(incomplete_stream_message, file=sys.stderr, flush=True)
            return EXIT_RUNTIME_ERROR
        blocked_event = pending_blocked_event(result.session, last_event(result))
        if json_output:
            print_json(runtime_stream_payload(result, workspace=workspace, show_thinking=show_thinking))
            if not interactive and blocked_event is not None:
                return blocked_exit_code(blocked_event)
            if result.session.status == "failed":
                return EXIT_RUNTIME_ERROR
        elif trace_output:
            print_trace_final(result)
            print_runtime_failure_footer(runtime, result, workspace=workspace)
            if blocked_event is not None:
                print_trace_blocked(result, blocked_event, workspace=workspace)
                return blocked_exit_code(blocked_event)
            if result.session.status == "failed":
                return EXIT_RUNTIME_ERROR
        elif not interactive:
            if blocked_event is not None:
                print_noninteractive_blocked(result, blocked_event, workspace=workspace)
                return blocked_exit_code(blocked_event)
            print_plain_runtime_output(result.output)
            print_runtime_failure_footer(runtime, result, workspace=workspace)
            if result.session.status == "failed":
                return EXIT_RUNTIME_ERROR
    return EXIT_SUCCESS


@click.command(
    name="run",
    help="Run through the local runtime provider or deterministic harness. "
    "Approval allow means consent to arbitrary command execution without isolation (no OS-level sandbox in v1).",
)
@click.argument("request")
@workspace_option("Workspace root used to resolve relative read paths.")
@click.option("--session-id", help="Optional session identifier used for persisted runs.")
@click.option(
    "--approval-mode",
    type=click.Choice(APPROVAL_MODES),
    help="Override the approval mode: always-ask, write, or yolo (which tool tiers are auto-approved).",
)
@click.option(
    "--agent",
    help="Select a top-level or local custom agent preset for this run.",
)
@click.option(
    "--mode",
    "runtime_mode",
    type=click.Choice(["normal", "plan"]),
    help="Select runtime mode metadata; plan is a runtime-enforced read-only mode.",
)
@click.option(
    "--read-only",
    is_flag=True,
    help="Request runtime-enforced read-only tool policy without selecting a named mode.",
)
@click.option("--model", help="Override the provider/model for this run.")
@click.option("--skills", multiple=True, help="Optional skill names applied for this run.")
@click.option(
    "--reasoning-effort",
    help="Reasoning-effort level: off, minimal, low, medium, high, xhigh, max.",
)
@show_thinking_option("Show persisted reasoning/thinking text; hidden by default.")
@json_option("Output a structured JSON payload with session, events, and final output.")
@click.option(
    "--trace",
    is_flag=True,
    help="Stream model text, TODO updates, tool calls, and command output for manual QA.",
)
@click.option(
    "--provider-stream/--no-provider-stream",
    default=None,
    help="Enable or disable provider-level streaming for this run.",
)
def run(
    request: str,
    workspace: Path,
    session_id: str | None,
    approval_mode: str | None,
    agent: str | None,
    model: str | None,
    skills: tuple[str, ...],
    reasoning_effort: str | None,
    show_thinking: bool,
    json_output: bool,
    trace: bool,
    provider_stream: bool | None,
    runtime_mode: str | None,
    read_only: bool,
) -> int:
    return _handle_run_command(
        RunArgs(
            request=request,
            workspace=workspace,
            session_id=session_id,
            approval_mode=approval_mode,
            agent=agent,
            model=model,
            skills=skills,
            reasoning_effort=reasoning_effort,
            show_thinking=show_thinking,
            json=json_output,
            trace=trace,
            provider_stream=provider_stream,
            runtime_mode=runtime_mode,
            read_only=read_only,
        )
    )
