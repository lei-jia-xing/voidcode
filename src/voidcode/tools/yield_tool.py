from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ._pydantic_args import format_validation_error
from .contracts import ToolCall, ToolDefinition, ToolResult
from .runtime_context import require_runtime_tool_context

YIELD_PROGRESS_MAX_SECTIONS = 100
YIELD_PROGRESS_MAX_SECTION_CHARS = 4_096
YIELD_PROGRESS_MAX_RETAINED_CHARS = 65_536
YIELD_PROGRESS_MAX_TYPE_CHARS = 64


def _bounded_progress_data(value: dict[str, object]) -> dict[str, object]:
    """Keep one progress section JSON-safe and bounded before persistence."""
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except TypeError, ValueError:
        return {"unavailable": "progress data could not be serialized"}
    if len(encoded) <= YIELD_PROGRESS_MAX_SECTION_CHARS:
        return value
    return {
        "truncated": True,
        "preview": encoded[: YIELD_PROGRESS_MAX_SECTION_CHARS - 1] + "…",
    }


def _normalize_type(value: str | list[str] | None) -> str | list[str] | None:
    if value is None:
        return None
    values = [value] if isinstance(value, str) else value
    if not values or len(values) > YIELD_PROGRESS_MAX_SECTIONS:
        raise ValueError(f"type must contain 1-{YIELD_PROGRESS_MAX_SECTIONS} non-empty strings")
    normalized: list[str] = []
    for index, item in enumerate(values):
        stripped = item.strip()
        if not stripped:
            raise ValueError(f"type[{index}] must be a non-empty string")
        if len(stripped) > YIELD_PROGRESS_MAX_TYPE_CHARS:
            raise ValueError(f"type[{index}] must be at most {YIELD_PROGRESS_MAX_TYPE_CHARS} characters")
        normalized.append(stripped)
    return normalized[0] if isinstance(value, str) else normalized


class YieldArgs(BaseModel):
    """Terminal handoff plus bounded incremental child progress.

    The existing ``summary``/``data`` shape remains the terminal contract.
    Supplying a non-result ``type`` creates one progress section and does not
    complete the child. ``result`` is the short human-readable section text;
    ``data`` carries optional structured detail. Error sections are terminal child failures,
    never successful handoffs, and are represented with an explicit error payload.
    """

    model_config = ConfigDict(extra="forbid")

    summary: str | None = Field(default=None, min_length=1)
    data: dict[str, object] = Field(default_factory=dict)
    type: str | list[str] | None = None
    result: str | None = Field(default=None, min_length=1)
    error: str | None = Field(default=None, min_length=1)

    @field_validator("summary", "result", "error", mode="after")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("text fields must be non-empty strings")
        if len(normalized) > YIELD_PROGRESS_MAX_SECTION_CHARS:
            raise ValueError(f"text fields must be at most {YIELD_PROGRESS_MAX_SECTION_CHARS} characters")
        return normalized

    @field_validator("type", mode="after")
    @classmethod
    def _normalize_type_field(cls, value: str | list[str] | None) -> str | list[str] | None:
        return _normalize_type(value)

    @model_validator(mode="after")
    def _validate_shape(self) -> YieldArgs:
        if isinstance(self.type, list) and any(item in {"result", "error"} for item in self.type):
            if self.type not in (["result"], ["error"]):
                raise ValueError("result and error types cannot be combined with progress types")
        terminal_error = self.error is not None or self.type == "error" or self.type == ["error"]
        result_type = self.type is None or self.type == "result" or self.type == ["result"]
        if terminal_error:
            if self.summary is not None or self.result is not None:
                raise ValueError("error yield payload cannot include summary or result")
            if self.error is None:
                raise ValueError("error yield payload requires a non-empty error")
            return self
        if result_type:
            if self.summary is None:
                raise ValueError("summary is required for a terminal yield")
            if self.result is not None:
                raise ValueError("result is only valid for incremental yield sections")
            return self
        if self.summary is not None:
            raise ValueError("summary is only valid for a terminal yield")
        if self.result is None and not self.data:
            raise ValueError("incremental yield requires result or non-empty data")
        return self

    def is_terminal(self) -> bool:
        return self.type is None or self.type == "result" or self.type == "error" or self.type == ["result"] or self.type == ["error"]

    def is_terminal_error(self) -> bool:
        return self.error is not None or self.type == "error" or self.type == ["error"]

    def progress_payload(self) -> dict[str, object]:
        if self.is_terminal():
            raise ValueError("terminal yield has no progress payload")
        payload: dict[str, object] = {"type": self.type}
        if self.result is not None:
            payload["result"] = self.result
        if self.data:
            payload["data"] = _bounded_progress_data(self.data)
        try:
            encoded_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
        except (TypeError, ValueError) as exc:
            raise ValueError("progress payload must be JSON serializable") from exc
        if encoded_size > YIELD_PROGRESS_MAX_SECTION_CHARS:
            raise ValueError(f"progress section must be at most {YIELD_PROGRESS_MAX_SECTION_CHARS} characters")
        return payload


class YieldTool:
    """Submit a terminal handoff or one bounded progress section."""

    definition: ClassVar[ToolDefinition] = ToolDefinition(
        name="yield",
        description=(
            "Submit a final delegated handoff with summary/data, or emit one bounded progress "
            "section using type and result/data. Progress does not complete the child."
        ),
        input_schema={
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "summary": {"type": "string", "minLength": 1, "description": "Required for terminal handoff."},
                "data": {"type": "object", "description": "Structured terminal or progress detail."},
                "type": {
                    "oneOf": [
                        {"type": "string", "minLength": 1, "maxLength": YIELD_PROGRESS_MAX_TYPE_CHARS},
                        {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1, "maxLength": YIELD_PROGRESS_MAX_TYPE_CHARS},
                            "minItems": 1,
                            "maxItems": YIELD_PROGRESS_MAX_SECTIONS,
                        },
                    ],
                    "description": "Optional progress type; result is terminal only when omitted or result.",
                },
                "result": {"type": "string", "minLength": 1, "maxLength": YIELD_PROGRESS_MAX_SECTION_CHARS},
                "error": {"type": "string", "minLength": 1, "maxLength": YIELD_PROGRESS_MAX_SECTION_CHARS},
            },
            "anyOf": [{"required": ["summary"]}, {"required": ["type"]}],
        },
        read_only=True,
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:  # noqa: ARG002 — protocol-required workspace parameter.
        context = require_runtime_tool_context(self.definition.name)
        if context.parent_session_id is None:
            raise ValueError("yield is only available to delegated child sessions")
        try:
            args = YieldArgs.model_validate(call.arguments)
        except ValidationError as exc:
            raise ValueError(format_validation_error(self.definition.name, exc)) from exc
        if not args.is_terminal():
            progress = args.progress_payload()
            content = args.result or json.dumps(progress.get("data", {}), ensure_ascii=False, default=str)
            return ToolResult(
                tool_name=self.definition.name,
                status="ok",
                content=content,
                data={"yield_kind": "progress", "progress": progress},
                reference=f"child-yield-progress:{context.session_id}",
            )
        if args.is_terminal_error():
            assert args.error is not None
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                content=None,
                error=args.error,
                data={
                    "yield_kind": "terminal_error",
                    "handoff": {"summary": args.error, "data": args.data, "error": args.error},
                },
                reference=f"child-yield-error:{context.session_id}",
            )
        assert args.summary is not None
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=args.summary,
            data={"yield_kind": "terminal", "handoff": {"summary": args.summary, "data": args.data}},
            reference=f"child-yield:{context.session_id}",
        )


__all__ = [
    "YIELD_PROGRESS_MAX_RETAINED_CHARS",
    "YIELD_PROGRESS_MAX_SECTION_CHARS",
    "YIELD_PROGRESS_MAX_SECTIONS",
    "YieldArgs",
    "YieldTool",
]
