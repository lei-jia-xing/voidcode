from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import require_runtime_tool_context


class SubmitResultArgs(BaseModel):
    """``submit_result(summary, data?)`` — the five fixed fields were deleted
    (breaking change); callers declaring equivalent structure must do so via
    the parent's ``outputSchema`` and put the payload in ``data``."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1)
    data: dict[str, object] = Field(default_factory=dict)


class SubmitResultTool:
    """Submit a structured handoff from a delegated child to its parent agent."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="submit_result",
        description=(
            "Submit the final structured handoff for delegated work. Child agents must call this "
            "after tools are complete so the parent agent can consume the result."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "minLength": 1, "description": "Short result for the parent agent."},
                "data": {
                    "type": "object",
                    "description": "Arbitrary structured detail validated against the parent-declared outputSchema when one was declared.",
                },
            },
            "required": ["summary"],
        },
        read_only=True,
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        context = require_runtime_tool_context(self.definition.name)
        if context.parent_session_id is None:
            raise ValueError("submit_result is only available to delegated child sessions")
        try:
            args = SubmitResultArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=args.summary,
            data={"handoff": args.model_dump()},
            reference=f"child-handoff:{context.session_id}",
        )
