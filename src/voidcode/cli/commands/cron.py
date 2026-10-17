"""``voidcode cron``: inspect and manage scheduled background tasks."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import click

from ...runtime.background.models import ScheduleSummary
from ..handler_args import CronArgs
from ..options import json_option, workspace_option
from ..output import emit_output
from ..runtime_gateway import open_runtime, runtime_error_boundary

if TYPE_CHECKING:
    pass


def _format_datetime(epoch: int | None) -> str:
    if not epoch:
        return "-"
    return datetime.datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")


def _schedule_summary_payload(s: ScheduleSummary) -> dict[str, object]:
    return {
        "id": s.id,
        "workspace": str(s.workspace),
        "schedule": s.schedule_text,
        "human_schedule": s.human_schedule,
        "kind": s.kind,
        "prompt": s.prompt,
        "parent_session_id": s.parent_session_id,
        "enabled": s.enabled,
        "next_fire_at": s.next_fire_at,
        "running": s.running_task_id is not None,
        "running_task_id": s.running_task_id,
        "created_at": s.created_at,
        "expires_at": s.expires_at,
    }


def _handle_cron_list_command(args: CronArgs) -> int:
    workspace = args.workspace
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        schedules = list(runtime.list_schedules())

    def _print_schedules() -> None:
        if not schedules:
            print("No scheduled tasks found.")
            return
        header = f"{'ID':<38}  {'SCHEDULE':<14}  {'NEXT FIRE':<20}  {'RUNNING':<8}  {'ENABLED':<8}"
        print(header)
        print("-" * len(header))
        for s in schedules:
            next_fire = _format_datetime(s.next_fire_at)
            running_str = "yes" if s.running_task_id is not None else "no"
            enabled_str = "yes" if s.enabled else "no"
            print(f"{s.id:<38}  {s.human_schedule:<14}  {next_fire:<20}  {running_str:<8}  {enabled_str:<8}")

    return emit_output(
        args,
        {
            "workspace": str(workspace),
            "schedules": [_schedule_summary_payload(s) for s in schedules],
        },
        _print_schedules,
    )


def _handle_cron_show_command(args: CronArgs) -> int:
    workspace = args.workspace
    schedule_id = args.schedule_id
    assert schedule_id is not None

    with open_runtime(workspace) as runtime, runtime_error_boundary():
        sched = runtime.get_schedule(schedule_id=schedule_id)
        if sched is None:
            raise click.ClickException(f"Schedule '{schedule_id}' not found.")

    def _print_detail() -> None:
        running_str = f"yes (task {sched.running_task_id})" if sched.running_task_id else "no"
        enabled_str = "yes" if sched.enabled else "no"
        print(f"ID:             {sched.id}")
        print(f"Workspace:      {sched.workspace}")
        print(f"Schedule:       {sched.schedule_text}")
        print(f"Cadence:        {sched.human_schedule}")
        print(f"Kind:           {sched.kind}")
        print(f"Enabled:        {enabled_str}")
        print(f"Running:        {running_str}")
        print(f"Next Fire:      {_format_datetime(sched.next_fire_at)}")
        print(f"Expires:        {_format_datetime(sched.expires_at)}")
        print(f"Created:        {_format_datetime(sched.created_at)}")
        print(f"Parent Session: {sched.parent_session_id or '-'}")
        print("Prompt:")
        for line in sched.prompt.splitlines():
            print(f"  {line}")

    return emit_output(
        args,
        _schedule_summary_payload(sched),
        _print_detail,
    )


def _handle_cron_enable_command(args: CronArgs) -> int:
    workspace = args.workspace
    schedule_id = args.schedule_id
    assert schedule_id is not None

    with open_runtime(workspace) as runtime, runtime_error_boundary():
        ok = runtime.enable_schedule(schedule_id=schedule_id)
        if not ok:
            raise click.ClickException(f"Schedule '{schedule_id}' not found.")

    def _print_enable() -> None:
        print(f"Schedule '{schedule_id}' enabled.")

    return emit_output(
        args,
        {"id": schedule_id, "enabled": True},
        _print_enable,
    )


def _handle_cron_disable_command(args: CronArgs) -> int:
    workspace = args.workspace
    schedule_id = args.schedule_id
    assert schedule_id is not None

    with open_runtime(workspace) as runtime, runtime_error_boundary():
        ok = runtime.disable_schedule(schedule_id=schedule_id)
        if not ok:
            raise click.ClickException(f"Schedule '{schedule_id}' not found.")

    def _print_disable() -> None:
        print(f"Schedule '{schedule_id}' disabled.")

    return emit_output(
        args,
        {"id": schedule_id, "enabled": False},
        _print_disable,
    )


def _handle_cron_remove_command(args: CronArgs) -> int:
    workspace = args.workspace
    schedule_id = args.schedule_id
    assert schedule_id is not None

    with open_runtime(workspace) as runtime, runtime_error_boundary():
        ok = runtime.delete_schedule(schedule_id=schedule_id)
        if not ok:
            raise click.ClickException(f"Schedule '{schedule_id}' not found.")

    def _print_remove() -> None:
        print(f"Schedule '{schedule_id}' removed.")

    return emit_output(
        args,
        {"id": schedule_id, "removed": True},
        _print_remove,
    )


@click.group(help="Inspect and manage scheduled background tasks.")
def cron() -> None:
    pass


@cron.command(name="list", help="List scheduled cron jobs in this workspace.")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output scheduled tasks as JSON.")
def cron_list(workspace: Path, json_output: bool) -> int:
    return _handle_cron_list_command(
        CronArgs(
            workspace=workspace,
            json=json_output,
        )
    )


@cron.command(help="Show detailed information for a scheduled task.")
@click.argument("schedule_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output scheduled task detail as JSON.")
def show(schedule_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_cron_show_command(
        CronArgs(
            schedule_id=schedule_id,
            workspace=workspace,
            json=json_output,
        )
    )


@cron.command(help="Enable a scheduled task.")
@click.argument("schedule_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output result as JSON.")
def enable(schedule_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_cron_enable_command(
        CronArgs(
            schedule_id=schedule_id,
            workspace=workspace,
            json=json_output,
        )
    )


@cron.command(help="Disable a scheduled task.")
@click.argument("schedule_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output result as JSON.")
def disable(schedule_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_cron_disable_command(
        CronArgs(
            schedule_id=schedule_id,
            workspace=workspace,
            json=json_output,
        )
    )


@cron.command(help="Remove a scheduled task.")
@click.argument("schedule_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output result as JSON.")
def remove(schedule_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_cron_remove_command(
        CronArgs(
            schedule_id=schedule_id,
            workspace=workspace,
            json=json_output,
        )
    )
