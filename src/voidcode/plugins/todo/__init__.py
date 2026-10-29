"""The ``todo`` tool and commands: session plan list plus reminder nudges."""

from collections.abc import Mapping
from functools import partial

from voidcode.sdk.plugin import Plugin, StateSchema

from .todo_plugin import (
    SCHEMA_VERSION,
    TODO_STATUSES,
    TodoClearCommand,
    TodoStatusCommand,
    TodoTool,
    parse_todo_config,
    prepare_todo_deactivation,
    todo_context,
    todo_observer,
)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    reminders = parse_todo_config(config)
    return Plugin(
        tools=(TodoTool(),),
        commands=(TodoStatusCommand(), TodoClearCommand()),
        context=todo_context,
        observer=partial(todo_observer, config=reminders),
        observed_facts=("invocation_finished", "run_finished"),
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "revision", "phases", "summary", "reminders"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "revision": {"type": "integer", "minimum": 0},
                    "phases": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["name", "tasks"],
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "tasks": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "required": ["content", "status"],
                                        "additionalProperties": False,
                                        "properties": {
                                            "content": {"type": "string"},
                                            "status": {"type": "string", "enum": list(TODO_STATUSES)},
                                            "blocker": {"type": "string"},
                                        },
                                    },
                                },
                            },
                        },
                    },
                    "summary": {
                        "type": "object",
                        "required": ["total", "pending", "in_progress", "completed", "abandoned", "blocked", "active"],
                        "additionalProperties": False,
                        "properties": {
                            "total": {"type": "integer"},
                            "pending": {"type": "integer"},
                            "in_progress": {"type": "integer"},
                            "completed": {"type": "integer"},
                            "abandoned": {"type": "integer"},
                            "blocked": {"type": "integer"},
                            "active": {"type": "integer"},
                        },
                    },
                    "reminders": {
                        "type": "object",
                        "required": ["todo", "todo_mid_run", "invocations", "cursor_sequence", "cycle_run_id"],
                        "additionalProperties": False,
                        "properties": {
                            "todo": {
                                "type": "object",
                                "required": ["attempts", "awaiting_progress", "progress_watermark", "cycle_run_id"],
                                "additionalProperties": False,
                                "properties": {
                                    "attempts": {"type": "integer", "minimum": 0},
                                    "awaiting_progress": {"type": "boolean"},
                                    "progress_watermark": {"type": "integer", "minimum": 0},
                                    "cycle_run_id": {"type": ["string", "null"]},
                                },
                            },
                            "todo_mid_run": {
                                "type": "object",
                                "required": ["attempts", "mutations", "mutation_baseline", "cycle_run_id"],
                                "additionalProperties": False,
                                "properties": {
                                    "attempts": {"type": "integer", "minimum": 0},
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
