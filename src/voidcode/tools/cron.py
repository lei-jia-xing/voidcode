"""Agent tools for scheduled tasks (Claude parity).

Provides cron_create, cron_delete, and cron_list tools on top of the runtime
scheduler core.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, ClassVar, Protocol

from ..runtime.background.models import ScheduleSummary
from ..runtime.background.schedule_expr import parse_schedule
from .contracts import (
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

if TYPE_CHECKING:
    from ..core.tool_context import ToolContext

MAX_SCHEDULES_PER_WORKSPACE: int = 50


class CronRuntime(Protocol):
    """Runtime surface required by cron tools."""

    def create_schedule(
        self,
        *,
        schedule: str,
        prompt: str,
        parent_session_id: str | None = None,
        enabled: bool = True,
        expires_at: int | None = None,
        schedule_id: str | None = None,
    ) -> ScheduleSummary: ...

    def delete_schedule(self, *, schedule_id: str) -> bool: ...

    def list_schedules(self, *, parent_session_id: str | None = None) -> tuple[ScheduleSummary, ...]: ...

    def get_schedule(self, *, schedule_id: str) -> ScheduleSummary | None: ...


class CronCommand:
    """Command executor for cron agent tool calls."""

    def __init__(self, *, runtime: CronRuntime) -> None:
        self._runtime = runtime

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if call.tool_name == "cron_create":
            return self._invoke_create(call, context)
        if call.tool_name == "cron_delete":
            return self._invoke_delete(call, context)
        if call.tool_name == "cron_list":
            return self._invoke_list(call, context)
        return ToolFailure(tool_name=call.tool_name, error=f"unknown tool: {call.tool_name}")

    def _invoke_create(self, call: ToolCall, context: ToolContext) -> ToolResult:
        args = call.arguments
        raw_schedule = args.get("schedule")
        raw_prompt = args.get("prompt")
        if not isinstance(raw_schedule, str) or not raw_schedule.strip():
            return ToolFailure(tool_name=call.tool_name, error="Schedule expression must be a non-empty string.")
        if not isinstance(raw_prompt, str) or not raw_prompt.strip():
            return ToolFailure(tool_name=call.tool_name, error="Prompt must be a non-empty string.")

        schedule_str = raw_schedule.strip()
        prompt_str = raw_prompt.strip()

        # Enforce MAX 50 schedules per workspace (Claude parity)
        existing = self._runtime.list_schedules()
        if len(existing) >= MAX_SCHEDULES_PER_WORKSPACE:
            return ToolFailure(
                tool_name=call.tool_name,
                error=(
                    f"Workspace schedule limit reached (maximum {MAX_SCHEDULES_PER_WORKSPACE} jobs). "
                    "Delete an existing job before scheduling a new one."
                ),
            )

        # Validate schedule grammar early
        try:
            parse_schedule(schedule_str)
        except ValueError as exc:
            return ToolFailure(tool_name=call.tool_name, error=str(exc))

        summary = self._runtime.create_schedule(
            schedule=schedule_str,
            prompt=prompt_str,
            parent_session_id=context.session_id,
        )
        payload = {
            "id": summary.id,
            "human_schedule": summary.human_schedule,
        }
        return ToolSuccess(
            tool_name=call.tool_name,
            output=TextOutput(json.dumps(payload, indent=2)),
        )

    def _invoke_delete(self, call: ToolCall, context: ToolContext) -> ToolResult:
        _ = context
        args = call.arguments
        raw_id = args.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            return ToolFailure(tool_name=call.tool_name, error="Schedule id must be a non-empty string.")
        sched_id = raw_id.strip()
        deleted = self._runtime.delete_schedule(schedule_id=sched_id)
        if not deleted:
            return ToolFailure(tool_name=call.tool_name, error=f"Schedule '{sched_id}' not found.")
        payload = {"id": sched_id}
        return ToolSuccess(
            tool_name=call.tool_name,
            output=TextOutput(json.dumps(payload, indent=2)),
        )

    def _invoke_list(self, call: ToolCall, context: ToolContext) -> ToolResult:
        _ = context
        schedules = self._runtime.list_schedules()
        jobs = [
            {
                "id": s.id,
                "schedule_text": s.schedule_text,
                "human_schedule": s.human_schedule,
                "prompt": s.prompt,
                "enabled": s.enabled,
                "next_fire_at": s.next_fire_at,
                "running": s.running_task_id is not None,
            }
            for s in schedules
        ]
        payload = {"jobs": jobs}
        return ToolSuccess(
            tool_name=call.tool_name,
            output=TextOutput(json.dumps(payload, indent=2)),
        )


class CronCreateTool:
    """Agent tool to create a scheduled prompt."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_create",
        description=(
            "Schedule a prompt to run at a future time — either recurring on an interval or cron schedule, "
            "or once at a specific time. Max 50 schedules per workspace. Returns the schedule ID and human-readable cadence."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "schedule": {
                    "type": "string",
                    "description": (
                        "Schedule expression: 'every <N><s|m|h|d>' (e.g. 'every 10m'), "
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
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def __init__(self, *, runtime: CronRuntime | None = None) -> None:
        self._command = CronCommand(runtime=runtime) if runtime is not None else None

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if self._command is not None:
            return self._command.invoke(call, context=context)
        if context.cron_runtime is not None:
            return context.cron_runtime(call, context=context)
        raise RuntimeError("cron_create requires a runtime-owned cron command")


class CronDeleteTool:
    """Agent tool to cancel a scheduled cron job by ID."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_delete",
        description="Cancel a scheduled cron job by ID.",
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
        effects=frozenset({ToolEffect.WRITE}),
        replay_policy="never",
    )

    def __init__(self, *, runtime: CronRuntime | None = None) -> None:
        self._command = CronCommand(runtime=runtime) if runtime is not None else None

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if self._command is not None:
            return self._command.invoke(call, context=context)
        if context.cron_runtime is not None:
            return context.cron_runtime(call, context=context)
        raise RuntimeError("cron_delete requires a runtime-owned cron command")


class CronListTool:
    """Agent tool to list scheduled cron jobs in this workspace."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="cron_list",
        description="List scheduled cron jobs in this workspace.",
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        effects=frozenset({ToolEffect.READ}),
        replay_policy="safe",
        supports_parallel=True,
    )

    def __init__(self, *, runtime: CronRuntime | None = None) -> None:
        self._command = CronCommand(runtime=runtime) if runtime is not None else None

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        if self._command is not None:
            return self._command.invoke(call, context=context)
        if context.cron_runtime is not None:
            return context.cron_runtime(call, context=context)
        raise RuntimeError("cron_list requires a runtime-owned cron command")
