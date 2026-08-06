from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, Field, ValidationError

from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import require_runtime_tool_context


class SubmitResultArgs(BaseModel):
    summary: str = Field(min_length=1)
    completed_work: list[str] = Field(default_factory=list)
    files_touched: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)


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
                "completed_work": {"type": "array", "items": {"type": "string"}, "description": "Completed actions or findings."},
                "files_touched": {"type": "array", "items": {"type": "string"}, "description": "Files inspected or changed."},
                "verification": {"type": "array", "items": {"type": "string"}, "description": "Tests, diagnostics, or other evidence."},
                "open_questions": {"type": "array", "items": {"type": "string"}, "description": "Questions the parent must resolve."},
                "blockers": {"type": "array", "items": {"type": "string"}, "description": "Unresolved blockers; empty means none known."},
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
