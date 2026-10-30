"""Cron plugin: scheduled prompts and the ``loop`` command over core wakeups."""

from collections.abc import Mapping

from voidcode.sdk.plugin import Plugin, StateSchema

from .cron_plugin import (
    SCHEMA_VERSION,
    CronCreateTool,
    CronDeleteCommand,
    CronDeleteTool,
    CronListCommand,
    CronListTool,
    CronLoopCommand,
    cron_observer,
    prepare_cron_deactivation,
)


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    unknown = sorted(set(config or {}) - {"providers"})
    if unknown:
        raise ValueError(f"cron config does not support field: {unknown[0]}")
    return Plugin(
        tools=(CronCreateTool(), CronDeleteTool(), CronListTool()),
        commands=(CronLoopCommand(), CronListCommand(), CronDeleteCommand()),
        observer=cron_observer,
        observed_facts=("run_finished",),
        state=StateSchema(
            version=SCHEMA_VERSION,
            schema={
                "type": "object",
                "required": ["schema_version", "schedules", "cursor_sequence"],
                "additionalProperties": False,
                "properties": {
                    "schema_version": {"type": "integer", "enum": [SCHEMA_VERSION]},
                    "cursor_sequence": {"type": "integer", "minimum": 0},
                    "schedules": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "object",
                            "required": ["schedule_text", "kind", "prompt", "enabled", "expires_at", "wakeup_id"],
                            "additionalProperties": False,
                            "properties": {
                                "schedule_text": {"type": "string"},
                                "kind": {"type": "string", "enum": ["every", "at", "cron", "once"]},
                                "prompt": {"type": "string"},
                                "enabled": {"type": "boolean"},
                                "expires_at": {"type": ["integer", "null"]},
                                "wakeup_id": {"type": ["string", "null"]},
                            },
                        },
                    },
                },
            },
        ),
        prepare_deactivate=prepare_cron_deactivation,
    )
