"""``voidcode tasks``: inspect, cancel, retry, and steer delegated background work."""

from __future__ import annotations

import sys
from pathlib import Path

import click

from ...runtime.background.models import BackgroundTaskState
from ..handler_args import TasksArgs
from ..options import json_option, workspace_option
from ..output import emit_output, print_runtime_output
from ..runtime_gateway import open_runtime, runtime_error_boundary
from ..tasks_view import (
    background_task_result_payload,
    background_task_state_payload,
    background_task_summary_payload,
    format_background_task_result,
    format_background_task_state,
    format_background_task_summary,
    print_background_task_guidance,
)


def _emit_task_state(
    args: TasksArgs,
    task: BackgroundTaskState,
    *,
    note: str | None = None,
    extra: dict[str, object] | None = None,
) -> int:
    workspace = args.workspace
    payload = background_task_state_payload(task, workspace=workspace)
    if extra is not None:
        payload.update(extra)

    def _print_task() -> None:
        print(format_background_task_state(task))
        if note is not None:
            print(note)
        print_background_task_guidance(payload)

    return emit_output(args, {"workspace": str(workspace), "task": payload}, _print_task)


def _handle_tasks_status_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        task = runtime.load_background_task(task_id)

    return _emit_task_state(args, task)


def _handle_tasks_output_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        session_output: str | None = None
        task_result = runtime.load_background_task_result(task_id)
        if task_result.result_available and task_result.child_session_id is not None:
            try:
                session_output = runtime.session_result(session_id=task_result.child_session_id).output
            except ValueError as exc:
                print(f"warning: session result output unavailable: {exc}", file=sys.stderr)
                session_output = None

    fallback_output = task_result.summary_output if task_result.summary_output is not None else task_result.error
    if session_output is None and fallback_output is not None:
        print("warning: WARN: session output unavailable; using fallback output", file=sys.stderr)
    output = session_output if session_output is not None else fallback_output
    payload = background_task_result_payload(task_result, workspace=workspace)

    def _print_task_output() -> None:
        print(format_background_task_result(task_result))
        print_background_task_guidance(payload)
        print_runtime_output(output)

    return emit_output(
        args,
        {"workspace": str(workspace), "task": payload, "output": output},
        _print_task_output,
    )


def _handle_tasks_cancel_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        task = runtime.cancel_background_task(task_id)

    return _emit_task_state(args, task)


def _handle_tasks_retry_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    assert task_id is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        task = runtime.retry_background_task(task_id)

    return _emit_task_state(
        args,
        task,
        note=f"RETRY previous_task_id={task_id} new_task_id={task.task.id}",
        extra={"retry_of_task_id": task_id},
    )


def _handle_tasks_steer_command(args: TasksArgs) -> int:
    workspace = args.workspace
    task_id = args.task_id
    prompt = args.prompt
    assert task_id is not None
    assert prompt is not None
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        task = runtime.steer_background_task(task_id, prompt)

    return _emit_task_state(
        args,
        task,
        note=f"STEER task_id={task_id} status={task.status}",
        extra={"steer_prompt": prompt},
    )


def _handle_tasks_list_command(args: TasksArgs) -> int:
    workspace = args.workspace
    parent_session_id = args.parent_session_id
    with open_runtime(workspace) as runtime, runtime_error_boundary():
        tasks = (
            runtime.list_background_tasks_by_parent_session(parent_session_id=parent_session_id)
            if parent_session_id is not None
            else runtime.list_background_tasks()
        )

    def _print_tasks() -> None:
        for task in tasks:
            print(format_background_task_summary(task))

    return emit_output(
        args,
        {
            "workspace": str(workspace),
            "parent_session_id": parent_session_id,
            "tasks": [background_task_summary_payload(task) for task in tasks],
        },
        _print_tasks,
    )


@click.group(help="Inspect delegated background tasks.")
def tasks() -> None:
    pass


@tasks.command(help="Show delegated task lifecycle state.")
@click.argument("task_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output delegated task state as JSON.")
def status(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_status_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Show delegated task output and correlation details.")
@click.argument("task_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output delegated task result and guidance as JSON.")
def output(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_output_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Cancel delegated background work.")
@click.argument("task_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output cancelled delegated task state as JSON.")
def cancel(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_cancel_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Retry failed, cancelled, or interrupted delegated background work.")
@click.argument("task_id")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output retried delegated task state as JSON.")
def retry(task_id: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_retry_command(
        TasksArgs(
            task_id=task_id,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(help="Dispatch a new worker turn for an idle keep-alive delegated task.")
@click.argument("task_id")
@click.argument("prompt")
@workspace_option("Workspace root used to resolve the local session database.")
@json_option("Output steered delegated task state as JSON.")
def steer(task_id: str, prompt: str, workspace: Path, json_output: bool) -> int:
    return _handle_tasks_steer_command(
        TasksArgs(
            task_id=task_id,
            prompt=prompt,
            workspace=workspace,
            json=json_output,
        )
    )


@tasks.command(name="list", help="List delegated background tasks.")
@workspace_option("Workspace root used to resolve the local session database.")
@click.option("--parent-session", "parent_session_id")
@json_option("Output delegated task summaries as JSON.")
def tasks_list(workspace: Path, parent_session_id: str | None, json_output: bool) -> int:
    return _handle_tasks_list_command(
        TasksArgs(
            workspace=workspace,
            parent_session_id=parent_session_id,
            json=json_output,
        )
    )
