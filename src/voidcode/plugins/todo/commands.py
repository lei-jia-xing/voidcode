"""Todo slash commands and their interactive inputs."""

from __future__ import annotations

from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.services import PluginServices, StateConflict
from voidcode.sdk.tools import ToolEffect

from . import limits, text
from .identity import TASK_ID_PATTERN
from .mutations import apply_and_store
from .operations import TASK_UPDATE_OPS, TodoOp, _target_tasks, _TodoArgs, parse_args
from .presentation import (
    duplicate_contents,
    paged_phase_payload,
    picker_label,
    task_from_id,
    task_operations,
    todo_presentation,
)
from .rendering import render_phases
from .state import TodoState, _store, todo_event_payload


class TodoStatusCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo",
        description="Show the current todo list.",
        argument_schema={
            "type": "object",
            "properties": {"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 10}},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        offset, limit = invocation.arguments.get("offset", 0), invocation.arguments.get("limit", 10)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 10:
            return CommandResult(status="error", error="offset must be non-negative and limit must be between 1 and 10")
        state, _ = _store(services).load()
        if offset > state.summary["total"]:
            return CommandResult(status="error", error="Todo offset is beyond the end of the list")
        return CommandResult(
            status="ok",
            output=text.bounded_command_output(render_phases(state.phases, format="table")),
            data={
                "phases": paged_phase_payload(state, offset=offset, limit=limit),
                "summary": dict(state.summary),
                "revision": state.revision,
            },
            presentation=todo_presentation(state, offset=offset, limit=limit),
        )


class TodoEditCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.edit",
        description="Choose a task operation without changing the todo list.",
        text_argument="task",
        argument_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "minLength": 1, "maxLength": limits.MAX_TODO_TASK_CONTENT_BYTES},
                "task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
            },
            "oneOf": [{"required": ["task"]}, {"required": ["task_id"]}],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = invocation
        state, _ = _store(services).load()
        tasks = [task for phase in state.phases for task in phase["tasks"]]
        if not tasks:
            return todo_presentation(state)
        duplicates = duplicate_contents(state)
        options = [{"id": task["id"], "label": picker_label(task, duplicates)} for task in tasks[:100]]
        return CommandPresentation(
            title="Choose todo task",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "task_id",
                        "label": f"Task ({min(len(tasks), 100)} of {len(tasks)})",
                        "options": options,
                    }
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        try:
            state, _ = _store(services).load()
            if "task_id" in invocation.arguments:
                task = task_from_id(state, invocation.arguments["task_id"])
            else:
                raw = invocation.arguments.get("task")
                if not isinstance(raw, str) or not raw.strip():
                    raise ValueError("todo.edit requires a task name")
                task = _target_tasks(list(state.phases), task=raw.strip(), phase=None)[0]
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        return CommandResult(
            status="ok",
            output=text.bounded_command_output(f"{task['content']} [{task['status']}]"),
            presentation=CommandPresentation(
                title=f"Edit task: {text.display_text(task['content'], 120)} [{task['status']}]",
                view_type="form",
                data={
                    "action": "todo.update",
                    "arguments": {"task_id": task["id"], "expected_revision": state.revision},
                    "fields": [
                        {"name": "op", "label": "Operation", "options": task_operations(task)},
                        {"name": "reason", "label": "Block reason (optional)", "field_type": "multiline", "required": False},
                    ],
                },
            ),
        )


class TodoUpdateCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.update",
        slash_visible=False,
        description="Apply a selected todo operation under its displayed list revision.",
        argument_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "pattern": TASK_ID_PATTERN.pattern},
                "op": {"type": "string", "enum": [op.value for op in TASK_UPDATE_OPS]},
                "reason": {"type": "string", "maxLength": limits.MAX_TODO_BLOCKER_BYTES},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["task_id", "op", "expected_revision"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        retry_state_conflict=True,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        revision = invocation.arguments.get("expected_revision")
        if type(revision) is not int or revision < 0:
            return CommandResult(status="error", error="todo.update requires the displayed todo revision")
        raw_op = invocation.arguments.get("op")
        try:
            op = TodoOp(raw_op) if isinstance(raw_op, str) else None
        except ValueError:
            op = None
        if op not in TASK_UPDATE_OPS:
            return CommandResult(status="error", error="Choose a valid task operation")
        try:
            state, _ = _store(services).load()
            task = task_from_id(state, invocation.arguments.get("task_id"))
            args = parse_args({**invocation.arguments, "task": task["content"]})
            change = apply_and_store(args, services, expected_revision=revision)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        return CommandResult(
            status="ok",
            output=text.bounded_command_output(render_phases(list(change.state.phases), args.op, original=change.original, format="tool")),
            data={"revision": change.state.revision, "mutated": change.mutated},
            presentation=todo_presentation(change.state),
        )


class TodoAddCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.add",
        description="Add tasks to a phase, one task per line.",
        argument_schema={
            "type": "object",
            "properties": {
                "phase": {"type": "string", "minLength": 1, "maxLength": limits.MAX_TODO_PHASE_NAME_BYTES},
                "items": {"type": "string", "minLength": 1},
                "expected_revision": {"type": "integer", "minimum": 0},
            },
            "required": ["phase", "items", "expected_revision"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        retry_state_conflict=True,
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = invocation
        state, _ = _store(services).load()
        return CommandPresentation(
            title="Add todo tasks",
            view_type="form",
            data={
                "action": self.definition.name,
                "arguments": {"expected_revision": state.revision},
                "initial_values": {"phase": state.phases[-1]["name"] if state.phases else "Tasks"},
                "fields": [{"name": "phase", "label": "Phase"}, {"name": "items", "label": "Tasks (one per line)", "field_type": "multiline"}],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        phase, items, revision = invocation.arguments.get("phase"), invocation.arguments.get("items"), invocation.arguments.get("expected_revision")
        if (
            not isinstance(phase, str)
            or not phase.strip()
            or not isinstance(items, str)
            or not items.strip()
            or type(revision) is not int
            or revision < 0
        ):
            return CommandResult(status="error", error="Provide a phase, tasks and the displayed todo revision")
        try:
            change = apply_and_store(
                _TodoArgs(op=TodoOp.APPEND, phase=phase, items=tuple(line for line in items.splitlines() if line.strip())),
                services,
                expected_revision=revision,
            )
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        return CommandResult(
            status="ok",
            output=text.bounded_command_output(
                render_phases(list(change.state.phases), TodoOp.APPEND.value, original=change.original, format="tool")
            ),
            data={"revision": change.state.revision, "mutated": change.mutated},
            presentation=todo_presentation(change.state),
        )


class TodoClearCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="todo.clear",
        description="Clear the todo list.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
        # Deliberate: clear is an explicit user reset and must remain available while
        # a run is busy. The list CAS is retried and owner-scoped queue cancellation
        # makes it safe against observer bookkeeping churn.
        busy_gate=False,
        retry_state_conflict=True,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        _ = invocation
        for attempt in range(limits.MAX_TODO_STATE_CONFLICT_ATTEMPTS):
            state, version = _store(services).load()
            if not state.phases:
                services.cancel_continuations()
                return CommandResult(status="ok", output="No todo list to clear.", data={"cleared": False}, presentation=todo_presentation(state))
            revision = state.revision + 1
            cleared = TodoState(revision=revision)
            try:
                _store(services).save(cleared, version)
            except StateConflict:
                if attempt + 1 >= limits.MAX_TODO_STATE_CONFLICT_ATTEMPTS:
                    return CommandResult(status="error", error="Todo state changed concurrently; retry the clear")
                continue
            services.cancel_continuations()
            services.stage_event("todo_cleared", todo_event_payload(phases=(), revision=revision))
            return CommandResult(status="ok", output="Todo list cleared.", data={"cleared": True}, presentation=todo_presentation(cleared))
        raise AssertionError("todo clear state-conflict retry exhausted without a result")
