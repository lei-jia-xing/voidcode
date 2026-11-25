"""Cron tools: create, delete, and list scheduled prompts."""

from __future__ import annotations

import json
from typing import ClassVar

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

from . import runtime
from .presentation import _schedule_status
from .schedule_expr import human_schedule, next_fire_after, parse_schedule
from .scheduling import _create, _delete_schedule
from .state import _store

CRON_CREATE_DESCRIPTION = (
    "Schedule a prompt to run at a future time — either recurring on an interval or cron schedule, "
    "or once at a specific time. Max 50 schedules per session. Recurring schedules expire after 7 days; "
    "an 'every' interval must be at least 1 minute and shorter than 7 days. "
    "Returns the schedule ID, human-readable cadence, first fire, and expiry."
)

CRON_DELETE_DESCRIPTION = "Cancel a scheduled cron job by ID. A run already in flight is not interrupted."

CRON_LIST_DESCRIPTION = "List scheduled cron jobs in this session."


class CronCreateTool:
    """Agent tool to create a scheduled prompt."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_create",
        icon=Icon(glyph="◷"),
        description=CRON_CREATE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "schedule": {
                    "type": "string",
                    "description": (
                        "Schedule expression: 'every <N><s|m|h|d>' (minimum 1 minute, shorter than 7 days; e.g. 'every 10m'), "
                        "'at <ISO-8601>' (e.g. 'at 2026-10-18T10:00:00'), "
                        "'cron <min> <hour> <day> <month> <dow>' (e.g. 'cron */5 * * * *'), or 'once'."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": "The prompt to enqueue and execute on each fire.",
                },
            },
            "required": ["schedule", "prompt"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        context.require_session_id()
        raw_schedule = call.arguments.get("schedule")
        raw_prompt = call.arguments.get("prompt")
        if not isinstance(raw_schedule, str) or not raw_schedule.strip():
            return _failure(self.definition.name, "Schedule expression must be a non-empty string.")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            return _failure(self.definition.name, "Prompt must be a non-empty string.")
        schedule_text = raw_schedule.strip()
        prompt = raw_prompt.strip()
        try:
            state, version = _store(services).load()
        except ValueError as exc:
            return _failure(self.definition.name, f"Cron state is invalid: {exc}")
        try:
            expr = parse_schedule(schedule_text)
            now_ms = runtime._clock_ms()
            first_fire_ms = next_fire_after(expr, now_ms // 1000) * 1000
            schedule_id, entry = _create(
                services,
                state=state,
                version=version,
                expr=expr,
                schedule_text=schedule_text,
                prompt=prompt,
                now_ms=now_ms,
                due_at_ms=first_fire_ms,
            )
        except ValueError as exc:
            return _failure(self.definition.name, str(exc))
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(
                json.dumps(
                    {
                        "id": schedule_id,
                        "human_schedule": human_schedule(expr),
                        "next_fire_at": entry.next_fire_at,
                        "expires_at": entry.expires_at,
                    },
                    indent=2,
                )
            ),
        )


class CronDeleteTool:
    """Agent tool to cancel a scheduled prompt by ID."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_delete",
        icon=Icon(glyph="◷"),
        description=CRON_DELETE_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "id": {
                    "type": "string",
                    "description": "Schedule/job ID to cancel.",
                },
            },
            "required": ["id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        context.require_session_id()
        raw_id = call.arguments.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            return _failure(self.definition.name, "Schedule id must be a non-empty string.")
        schedule_id = raw_id.strip()
        try:
            _delete_schedule(services, schedule_id)
        except ValueError as exc:
            return _failure(self.definition.name, str(exc))
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps({"id": schedule_id, "note": runtime.DELETE_IN_FLIGHT_NOTE}, indent=2)),
        )


class CronListTool:
    """Agent tool to list this session's scheduled prompts."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_list",
        icon=Icon(glyph="◷"),
        description=CRON_LIST_DESCRIPTION,
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        replay_policy="safe",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult:
        _ = call
        context.require_session_id()
        try:
            state, _version = _store(services).load()
        except ValueError as exc:
            return _failure(self.definition.name, f"Cron state is invalid: {exc}")
        jobs: list[dict[str, object]] = []
        for schedule_id, entry in state.schedules.items():
            expr = parse_schedule(entry.schedule_text)
            jobs.append(
                {
                    "id": schedule_id,
                    "schedule_text": entry.schedule_text,
                    "human_schedule": human_schedule(expr),
                    "next_fire_at": entry.next_fire_at,
                    "pending_wakeup_id": None if entry.pending is None else entry.pending.wakeup_id,
                    "status": _schedule_status(entry),
                    "enabled": entry.enabled,
                    "expires_at": entry.expires_at,
                }
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(json.dumps({"jobs": jobs}, indent=2)),
        )


def _failure(tool_name: str, message: str) -> ToolResult:
    return ToolFailure(tool_name=tool_name, error=message, output=TextOutput(text=message))
