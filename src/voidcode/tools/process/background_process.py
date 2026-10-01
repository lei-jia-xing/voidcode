from __future__ import annotations

from ...core.tool_context import ToolContext
from ..contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult


class BackgroundProcessTool:
    definition = ToolDefinition(
        name="background_process",
        description=(
            "Manage a long-running background process. Use op=start to launch, ps to list the "
            "current workspace processes, logs to read bounded output, send to write stdin, or "
            "stop to terminate a process. Process rows are scoped to the current owner and workspace."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": True,
            "oneOf": [
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "command"],
                    "properties": {
                        "op": {"const": "start"},
                        "command": {"type": "string"},
                        "description": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op"],
                    "properties": {"op": {"const": "ps"}},
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id"],
                    "properties": {
                        "op": {"const": "logs"},
                        "process_id": {"type": "string"},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id", "input"],
                    "properties": {
                        "op": {"const": "send"},
                        "process_id": {"type": "string"},
                        "input": {"type": "string"},
                        "newline": {"type": "boolean", "default": True},
                    },
                },
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["op", "process_id"],
                    "properties": {
                        "op": {"const": "stop"},
                        "process_id": {"type": "string"},
                    },
                },
            ],
        },
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN}),
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        context.require_workspace()
        context.require_session_id()
        command = context.process_runtime
        if command is None:
            raise RuntimeError("background_process requires a runtime-owned process command")
        return command(call, context=context)
