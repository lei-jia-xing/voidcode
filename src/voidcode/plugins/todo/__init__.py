"""The ``todo`` tool and commands: session plan list plus reminder nudges."""

from collections.abc import Mapping
from functools import partial

from voidcode.sdk.plugin import Plugin, StateSchema

from .todo_plugin import (
    MAX_TODO_BLOCKER_BYTES,
    MAX_TODO_PHASE_NAME_BYTES,
    MAX_TODO_PHASES,
    MAX_TODO_TASK_CONTENT_BYTES,
    MAX_TODO_TASKS_PER_PHASE,
    SCHEMA_VERSION,
    TODO_STATUSES,
    TodoAddCommand,
    TodoClearCommand,
    TodoEditCommand,
    TodoStatusCommand,
    TodoTool,
    TodoUpdateCommand,
    parse_todo_config,
    prepare_todo_deactivation,
    todo_context,
    todo_observer,
)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    reminders = parse_todo_config(config)
    return Plugin(
        tools=(TodoTool(),),
        commands=(TodoStatusCommand(), TodoClearCommand(), TodoAddCommand(), TodoEditCommand(), TodoUpdateCommand()),
        context=todo_context,
        observer=partial(todo_observer, config=reminders),
        observed_facts=("invocation_finished", "run_finished"),
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "revision", "phases", "reminders"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "revision": {"type": "integer", "minimum": 0},
                    "phases": {
                        "type": "array",
                        "maxItems": MAX_TODO_PHASES,
                        "items": {
                            "type": "object",
                            "required": ["name", "tasks"],
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string", "maxLength": MAX_TODO_PHASE_NAME_BYTES},
                                "tasks": {
                                    "type": "array",
                                    "maxItems": MAX_TODO_TASKS_PER_PHASE,
                                    "items": {
                                        "type": "object",
                                        "required": ["id", "content", "status"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
                                            "content": {"type": "string", "maxLength": MAX_TODO_TASK_CONTENT_BYTES},
                                            "status": {"type": "string", "enum": list(TODO_STATUSES)},
                                            "blocker": {"type": "string", "maxLength": MAX_TODO_BLOCKER_BYTES},
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "reminders": {
                        "type": "object",
                        "required": ["todo", "todo_mid_run", "invocations", "cursor_sequence", "cycle_run_id"],
                        "additionalProperties": False,
                        "properties": {
                            "todo": {
                                "type": "object",
                                "required": ["attempts", "awaiting_progress", "progress_watermark", "mutations", "mutation_baseline", "cycle_run_id"],
                                "additionalProperties": False,
                                "properties": {
                                    "attempts": {"type": "integer", "minimum": 0},
                                    "awaiting_progress": {"type": "boolean"},
                                    "progress_watermark": {"type": "integer", "minimum": 0},
                                    "mutations": {"type": "integer", "minimum": 0},
                                    "mutation_baseline": {"type": "integer", "minimum": 0},
                                    "cycle_run_id": {"type": ["string", "null"]},
                                },
                            },
                            "todo_mid_run": {
                                "type": "object",
                                "required": ["attempts", "awaiting_progress", "progress_watermark", "mutations", "mutation_baseline", "cycle_run_id"],
                                "additionalProperties": False,
                                "properties": {
                                    "attempts": {"type": "integer", "minimum": 0},
                                    "awaiting_progress": {"type": "boolean"},
                                    "progress_watermark": {"type": "integer", "minimum": 0},
                                    "mutations": {"type": "integer", "minimum": 0},
                                    "mutation_baseline": {"type": "integer", "minimum": 0},
                                    "cycle_run_id": {"type": ["string", "null"]},
                                },
                            },
                            "invocations": {"type": "integer", "minimum": 0},
                            "cursor_sequence": {"type": "integer", "minimum": 0},
                            "cycle_run_id": {"type": ["string", "null"]},
                        },
                    },
                },
            },
        ),
        prepare_deactivate=prepare_todo_deactivation,
    )
