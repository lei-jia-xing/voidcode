"""Cron commands: ``/loop`` and its list/delete companions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandPresentation, CommandResult
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect

from . import runtime
from .loop_command import DEFAULT_INTERVAL, USAGE_MESSAGE, loop_confirmation_message, parse_loop_command
from .presentation import _human_time, _schedule_status
from .schedule_expr import human_schedule, parse_schedule
from .scheduling import _create, _delete_schedule
from .state import _store


@dataclass(frozen=True, slots=True)
class _LoopSummary:
    """The structural summary ``loop_confirmation_message`` reads (legacy ``ScheduleSummary``)."""

    id: str
    prompt: str
    human_schedule: str


class CronLoopCommand:
    """`/loop [interval] <prompt>`: create an ``every <interval>`` schedule and fire it now."""

    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="loop",
        text_argument="input",
        description="Run a prompt on a recurring interval (minimum 1 minute), firing it immediately.",
        argument_schema={
            "type": "object",
            "properties": {
                "input": {"type": "string", "description": "Raw /loop arguments: '[interval] <prompt>'."},
            },
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
        client_syntax="/loop",
    )

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation:
        _ = (invocation, services)
        return CommandPresentation(
            title="Schedule a repeating prompt",
            view_type="form",
            data={
                "action": self.definition.name,
                "fields": [
                    {
                        "name": "input",
                        "label": (f"[interval] prompt — minimum 1m, e.g. 5m check the deploy; default {DEFAULT_INTERVAL}"),
                        "field_type": "multiline",
                        "required": True,
                    }
                ],
            },
        )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        raw_input = invocation.arguments.get("input")
        interval, prompt = parse_loop_command(raw_input if isinstance(raw_input, str) else "")
        if interval is None or prompt is None:
            return CommandResult(status="ok", output=USAGE_MESSAGE, data={"is_usage": True}, presentation=self.prepare_input(invocation, services))
        schedule_text = f"every {interval}"
        try:
            expr = parse_schedule(schedule_text)
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        try:
            state, version = _store(services).load()
        except ValueError as exc:
            return CommandResult(status="error", error=f"Cron state is invalid: {exc}")
        now_ms = runtime._clock_ms()
        try:
            schedule_id, entry = _create(
                services,
                state=state,
                version=version,
                expr=expr,
                schedule_text=schedule_text,
                prompt=prompt,
                now_ms=now_ms,
                due_at_ms=now_ms,
            )
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        summary = _LoopSummary(id=schedule_id, prompt=prompt, human_schedule=human_schedule(expr))
        return CommandResult(
            status="ok",
            output=loop_confirmation_message(summary),
            data={
                "session_id": invocation.context.session_id,
                "schedule_id": schedule_id,
                "schedule": schedule_text,
                "human_schedule": summary.human_schedule,
                "prompt": prompt,
                "next_fire_at": entry.next_fire_at,
                "expires_at": entry.expires_at,
                "is_usage": False,
            },
            presentation=CommandPresentation(
                title="Repeating prompt created",
                view_type="markdown",
                data={
                    "content": (
                        f"{summary.human_schedule}\n\n{prompt[:1800]}\n\n"
                        f"Schedule ID: {schedule_id}\nExpires: {_human_time(entry.expires_at) if entry.expires_at is not None else 'never'}"
                    ),
                    "actions": [
                        {"label": "Browse schedules", "action": "loop.list"},
                        {"label": "Cancel this schedule", "action": "loop.delete", "arguments": {"id": schedule_id}},
                    ],
                },
            ),
        )


class CronListCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="loop.list",
        description="Browse this session's scheduled prompts.",
        argument_schema={"type": "object", "properties": {"offset": {"type": "integer", "minimum": 0}}, "additionalProperties": False},
        effects=frozenset({ToolEffect.READ}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        try:
            state, _ = _store(services).load()
        except ValueError as exc:
            return CommandResult(status="error", error=f"Cron state is invalid: {exc}")
        entries = list(state.schedules.items())
        offset = invocation.arguments.get("offset", 0)
        if type(offset) is not int or not 0 <= offset <= len(entries):
            return CommandResult(status="error", error="Schedule list changed; reopen /loop list.")
        lines = [f"Schedules {offset}–{min(offset + 10, len(entries))} of {len(entries)}"]
        actions: list[dict[str, object]] = [{"label": "Create repeating prompt", "action": "loop", "prepare_input": True}]
        if not entries:
            lines.append("No scheduled prompts. Create one to begin.")
        for identifier, entry in entries[offset : offset + 10]:
            detail = f"{_schedule_status(entry)}"
            if entry.next_fire_at is not None:
                detail += f" · next {_human_time(entry.next_fire_at)}"
            if entry.expires_at is not None:
                detail += f" · expires {_human_time(entry.expires_at)}"
            lines.append(f"{identifier} · {detail} · {entry.schedule_text[:100]}\n{entry.prompt[:200]}")
            actions.append({"label": f"Cancel: {entry.prompt[:40]}", "action": "loop.delete", "arguments": {"id": identifier}})
        if offset:
            actions.append({"label": "Previous schedules", "action": "loop.list", "arguments": {"offset": max(0, offset - 10)}})
        if offset + 10 < len(entries):
            actions.append({"label": "Next schedules", "action": "loop.list", "arguments": {"offset": offset + 10}})
        return CommandResult(
            status="ok",
            output="\n\n".join(lines),
            data={"total": len(entries)},
            presentation=CommandPresentation(
                title="Scheduled prompts", view_type="markdown", data={"content": "\n\n".join(lines), "actions": actions}
            ),
        )


class CronDeleteCommand:
    definition: ClassVar[CommandDefinition] = CommandDefinition(
        name="loop.delete",
        description="Cancel a scheduled prompt by ID. A run already in flight is not interrupted.",
        text_argument="id",
        argument_schema={
            "type": "object",
            "properties": {"id": {"type": "string", "minLength": 1}},
            "required": ["id"],
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.SESSION}),
        busy_gate=False,
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        identifier = invocation.arguments.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            return CommandResult(status="error", error="Choose a schedule from /loop list.")
        try:
            _delete_schedule(services, identifier.strip())
        except ValueError as exc:
            return CommandResult(status="error", error=str(exc))
        listed = CronListCommand().invoke(CommandInvocation(invocation.context, "loop.list"), services)
        return CommandResult(
            status="ok",
            output=f"Schedule {identifier} cancelled. {runtime.DELETE_IN_FLIGHT_NOTE}",
            data={"id": identifier},
            presentation=listed.presentation,
        )
