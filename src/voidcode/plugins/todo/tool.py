"""The model-facing todo tool."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import ClassVar

from voidcode.sdk.icons import Icon
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess

from . import limits
from .models import TodoPhase, TodoSummary, todo_summary
from .mutations import apply_and_store
from .operations import TARGET_OPS, TodoOp, parse_args
from .rendering import render_phases
from .state import phases_payload


@dataclass(frozen=True, slots=True)
class TodoResultBody:
    phases: tuple[TodoPhase, ...]
    summary: TodoSummary
    op: str
    mutated: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "phases", tuple(deepcopy(phase) for phase in self.phases))
        object.__setattr__(self, "summary", deepcopy(self.summary))

    def as_payload(self) -> dict[str, object]:
        return {
            "phases": phases_payload(self.phases),
            "summary": dict(self.summary),
            "op": self.op,
            "mutated": self.mutated,
        }


TODO_DESCRIPTION = (
    "Apply one operation to the runtime-owned todo list. The operation is one of "
    "init, start, done, drop, block, unblock, append, rm, or view. Use init with "
    "list=[{phase, items}] (or items plus optional phase) to create a plan. Use "
    "exact task content and phase names for targeting. start requires a task; "
    "done, drop, block, unblock, and rm require an explicit task or phase target "
    "(there is no implicit all-task operation). Start a task before starting "
    "implementation and mark it done immediately after finishing. Only one task can "
    "be in_progress; the runtime normalizes that invariant. block accepts an optional "
    "reason, and view never mutates state. This tool does not write workspace files."
)


class TodoTool:
    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="todo",
        icon=Icon(glyph="☑"),
        description=TODO_DESCRIPTION,
        input_schema={
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": [op.value for op in TodoOp]},
                "list": {
                    "type": "array",
                    "maxItems": limits.MAX_TODO_PHASES,
                    "description": "init phases, each with phase and non-empty items",
                    "items": {
                        "type": "object",
                        "properties": {
                            "phase": {"type": "string", "maxLength": limits.MAX_TODO_PHASE_NAME_BYTES},
                            "items": {
                                "type": "array",
                                "maxItems": limits.MAX_TODO_TASKS_PER_PHASE,
                                "items": {"type": "string", "maxLength": limits.MAX_TODO_TASK_CONTENT_BYTES},
                            },
                        },
                        "required": ["phase", "items"],
                        "additionalProperties": False,
                    },
                },
                "task": {
                    "type": "string",
                    "maxLength": limits.MAX_TODO_TASK_CONTENT_BYTES,
                    "description": "Exact task content target",
                },
                "phase": {
                    "type": "string",
                    "maxLength": limits.MAX_TODO_PHASE_NAME_BYTES,
                    "description": "Exact phase name target or append destination",
                },
                "items": {
                    "type": "array",
                    "maxItems": limits.MAX_TODO_TASKS_PER_PHASE,
                    "items": {"type": "string", "maxLength": limits.MAX_TODO_TASK_CONTENT_BYTES},
                    "description": "Task strings for init or append",
                },
                "reason": {"type": "string", "maxLength": limits.MAX_TODO_BLOCKER_BYTES, "description": "Optional reason for block"},
            },
            "required": ["op"],
            "additionalProperties": False,
            "allOf": [
                {
                    "if": {"properties": {"op": {"const": TodoOp.START.value}}, "required": ["op"]},
                    "then": {"required": ["task"]},
                },
                {
                    "if": {
                        "properties": {"op": {"enum": sorted(op.value for op in TARGET_OPS if op is not TodoOp.START)}},
                        "required": ["op"],
                    },
                    "then": {"anyOf": [{"required": ["task"]}, {"required": ["phase"]}]},
                },
            ],
        },
        effects=frozenset({ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        services: PluginServices,
    ) -> ToolResult:
        context.require_session_id()
        try:
            args = parse_args(call.arguments)
            change = apply_and_store(args, services)
        except ValueError as exc:
            message = str(exc)
            return ToolFailure(tool_name=self.definition.name, error=message, output=TextOutput(text=message))

        phases = list(change.state.phases)
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(render_phases(phases, args.op, original=change.original, format="tool")),
            body=TodoResultBody(
                phases=tuple(phases),
                summary=todo_summary(phases),
                op=args.op,
                mutated=change.mutated,
            ),
        )
