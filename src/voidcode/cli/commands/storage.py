"""``voidcode storage`` and ``voidcode stats``: local runtime store maintenance and metrics."""

from __future__ import annotations

from pathlib import Path

import click

from ...cli_support import EXIT_RUNTIME_ERROR, EXIT_SUCCESS, print_json
from ...runtime.storage import SqliteSessionStore
from ..errors import CliError
from ..handler_args import StatsArgs, StorageArgs
from ..options import json_option, workspace_option
from ..output import emit_output, format_rate
from ..runtime_gateway import open_runtime


def _handle_storage_diagnostics_command(args: StorageArgs) -> int:
    workspace = args.workspace
    with open_runtime(workspace) as runtime:
        diagnostics = runtime.storage_diagnostics()
    print_json({"workspace": str(workspace), "storage": diagnostics})
    return EXIT_SUCCESS


def _handle_stats_tools_command(args: StatsArgs) -> int:
    workspace = args.workspace
    with open_runtime(workspace) as runtime:
        report = runtime.tool_effectiveness_report()
    payload = report.to_payload()

    def _print_report() -> None:
        print(
            "TOOL EFFECTIVENESS "
            f"sessions={report.session_count} calls={report.tool_call_count} "
            f"success={report.success_count} errors={report.error_count} "
            f"success_rate={format_rate(report.success_rate)}"
        )
        print(
            "SIGNALS "
            f"repeated_reads={report.repeated_read_count} followup_reads={report.followup_read_count} "
            f"compactions={report.compaction_count} approvals={report.approval_request_count} "
            f"resumed_runs={report.resumed_run_count} delegated_tasks={report.delegated_task_count}"
        )
        print(
            "TOKENS "
            f"input={report.input_tokens} output={report.output_tokens} "
            f"cache_read={report.cache_read_tokens} cache_write={report.cache_write_tokens} "
            f"uncached_input={report.uncached_input_tokens} cache_hit_rate={format_rate(report.cache_hit_rate)}"
        )
        if not report.tools:
            print("No persisted tool calls for this workspace.")
            return
        print("TOOL                         CALLS     OK  ERRORS   RATE  RETRIES  TRUNCATED  ERROR KINDS")
        for tool in report.tools:
            error_kinds = ",".join(f"{kind}:{count}" for kind, count in tool.error_kinds.items()) or "-"
            print(
                f"{tool.tool[:28]:<28} {tool.calls:>5} {tool.successes:>6} {tool.errors:>7} "
                f"{format_rate(tool.success_rate):>6} {tool.retries_after_error:>8} "
                f"{tool.truncated_results:>10}  {error_kinds}"
            )

    return emit_output(
        args,
        {"workspace": str(workspace), "effectiveness": payload},
        _print_report,
    )


def _handle_storage_prune_command(args: StorageArgs) -> int:
    workspace = args.workspace
    with open_runtime(workspace) as runtime:
        try:
            counts = runtime.prune_runtime_storage(
                keep_sessions=args.keep_sessions,
                keep_background_tasks=args.keep_background_tasks,
                older_than=args.older_than,
            )
        except ValueError as exc:
            raise CliError(code=EXIT_RUNTIME_ERROR, message=str(exc)) from None
    print_json({"workspace": str(workspace), "pruned": counts})
    return EXIT_SUCCESS


def _handle_storage_reset_command(args: StorageArgs) -> int:
    workspace = args.workspace
    # Reset is a pure file-deletion operation on the global store. It must NOT
    # boot a full runtime: runtime teardown reconnects to the session store
    # (background-task shutdown terminalizes queued tasks), which would
    # recreate the database files that were just deleted.
    store = SqliteSessionStore()
    result = store.reset_runtime_storage(workspace=workspace)
    print_json({"storage": result})
    return EXIT_SUCCESS


@click.group(help="Inspect and maintain the local runtime SQLite store.")
def storage() -> None:
    pass


@storage.command(help="Show SQLite runtime storage policy, checkpoint, size, and row counts.")
@workspace_option("Workspace root used to resolve the local session database.")
def diagnostics(workspace: Path) -> int:
    return _handle_storage_diagnostics_command(
        StorageArgs(
            workspace=workspace,
            json=True,
        )
    )


@click.group(help="Inspect local agent effectiveness metrics.")
def stats() -> None:
    pass


@stats.command(name="tools", help="Summarize persisted tool success, errors, retries, and output pressure.")
@workspace_option("Workspace root used to select persisted runtime sessions.")
@json_option("Output tool effectiveness metrics as JSON.")
def stats_tools(workspace: Path, json_output: bool) -> int:
    return _handle_stats_tools_command(
        StatsArgs(
            workspace=workspace,
            json=json_output,
        )
    )


@storage.command(help="Prune terminal sessions and terminal background tasks from local storage.")
@workspace_option("Workspace root used to resolve the local session database.")
@click.option("--keep-sessions", type=int)
@click.option("--keep-background-tasks", type=int)
@click.option("--older-than", type=int)
def prune(
    workspace: Path,
    keep_sessions: int | None,
    keep_background_tasks: int | None,
    older_than: int | None,
) -> int:
    return _handle_storage_prune_command(
        StorageArgs(
            workspace=workspace,
            keep_sessions=keep_sessions,
            keep_background_tasks=keep_background_tasks,
            older_than=older_than,
        )
    )


@storage.command(help="Delete the runtime SQLite database and WAL/SHM files.")
@workspace_option("Workspace root accepted for CLI parity; the database itself is global.")
def reset(workspace: Path) -> int:
    return _handle_storage_reset_command(
        StorageArgs(
            workspace=workspace,
        )
    )
