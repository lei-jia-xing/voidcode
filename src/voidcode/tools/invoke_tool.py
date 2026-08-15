"""Runtime dispatch builtin: invoke a registry-resolved tool by name.

``invoke_tool`` is the on-demand access half of the essential/discoverable
tool split. It is always top-level so that discoverable tools (whose schemas
are excluded from the provider tools array) stay reachable.

The actual execution does NOT happen inside ``invoke()``: this tool has no
registry or permission access of its own. The runtime run loop recognizes
``invoke_tool`` calls and re-enters the standard tool-execution pipeline
(policy denial -> registry resolve -> permission -> hooks -> executor) with
the *inner* tool call, reusing the exact boundary provider-native tool calls
use (see ``runtime/run_loop.py`` ``execute_graph_loop``). Direct invocation of
this tool outside that boundary is a governance error.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar, final

from pydantic import BaseModel, ValidationError, field_validator

from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult


class InvokeToolArgs(BaseModel):
    name: str
    arguments: dict[str, object] | None = None

    @field_validator("name", mode="after")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must be a non-empty string")
        return stripped

    @field_validator("arguments", mode="after")
    @classmethod
    def _validate_arguments(cls, value: dict[str, object] | None) -> dict[str, object] | None:
        if value is None:
            return None
        return dict(value)


@final
class InvokeTool:
    """Dispatch a runtime-registered tool through the standard execution boundary."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="invoke_tool",
        description=(
            "Execute a runtime-registered tool by name, passing its own input schema arguments. "
            "Use this when the target tool is not listed top-level (it is still available for "
            "dispatch). Read its documentation first with read on 'voidcode://tool/<name>'. "
            "The runtime applies the target tool's normal permission, allowlist, and read-only "
            "policy; a denied or unknown tool returns an error result instead of failing the run."
        ),
        input_schema={
            "name": {
                "type": "string",
                "description": "Tool name to invoke (for example 'web_search', 'apply_patch', or an MCP tool such as 'mcp/context7/query-docs').",
            },
            "arguments": {
                "type": "object",
                "description": "Arguments object matching the target tool's input schema; omitted when the tool takes no arguments.",
            },
            "required": ["name"],
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = workspace
        try:
            InvokeToolArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc
        raise ValueError(
            "invoke_tool must be dispatched by the runtime run loop; direct invocation is not supported outside the tool-execution boundary"
        )
