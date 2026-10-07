from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.tool_context import ToolContext
from ..security.json_values import json_wire_object
from ._pydantic_args import parse_tool_args
from .contracts import (
    EmptyOutput,
    OutputBounds,
    OutputReference,
    ProgressYield,
    TerminalYield,
    TerminalYieldFailure,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolEffect,
    ToolFailure,
    ToolResult,
    ToolSuccess,
)

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
            if self.type not in (None, "error", ["error"]):
                raise ValueError("error yield type must be omitted or error")
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

    def progress_control(self) -> ProgressYield:
        if self.is_terminal():
            raise ValueError("terminal yield has no progress section")
        if isinstance(self.type, list):
            types = tuple(self.type)
        elif isinstance(self.type, str):
            types = (self.type,)
        else:
            raise ValueError("progress yield requires a type")
        data = _bounded_progress_data(self.data)
        payload: dict[str, object] = {"type": self.type}
        if self.result is not None:
            payload["result"] = self.result
        if data:
            payload["data"] = data
        try:
            encoded_size = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str))
        except (TypeError, ValueError) as exc:
            raise ValueError("progress payload must be JSON serializable") from exc
        if encoded_size > YIELD_PROGRESS_MAX_SECTION_CHARS:
            raise ValueError(f"progress section must be at most {YIELD_PROGRESS_MAX_SECTION_CHARS} characters")
        return ProgressYield(types, self.result, data)


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
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": YIELD_PROGRESS_MAX_SECTION_CHARS,
                    "pattern": r"\S",
                    "description": "Required for terminal handoff.",
                },
                "data": {"type": "object", "description": "Structured terminal or progress detail."},
                "type": {
                    "oneOf": [
                        {"type": "string", "minLength": 1, "maxLength": YIELD_PROGRESS_MAX_TYPE_CHARS, "pattern": r"\S"},
                        {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": YIELD_PROGRESS_MAX_TYPE_CHARS,
                                "pattern": r"\S",
                            },
                            "minItems": 1,
                            "maxItems": YIELD_PROGRESS_MAX_SECTIONS,
                        },
                    ],
                    "description": "Optional progress type; result and error identify terminal yields.",
                },
                "result": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": YIELD_PROGRESS_MAX_SECTION_CHARS,
                    "pattern": r"\S",
                },
                "error": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": YIELD_PROGRESS_MAX_SECTION_CHARS,
                    "pattern": r"\S",
                },
            },
            "oneOf": [
                {
                    "required": ["summary"],
                    "properties": {
                        "type": {
                            "oneOf": [
                                {"type": "string", "pattern": r"^\s*result\s*$"},
                                {
                                    "type": "array",
                                    "items": {"type": "string", "pattern": r"^\s*result\s*$"},
                                    "minItems": 1,
                                    "maxItems": 1,
                                },
                            ]
                        }
                    },
                    "not": {"anyOf": [{"required": ["result"]}, {"required": ["error"]}]},
                },
                {
                    "required": ["error"],
                    "properties": {
                        "type": {
                            "oneOf": [
                                {"type": "string", "pattern": r"^\s*error\s*$"},
                                {
                                    "type": "array",
                                    "items": {"type": "string", "pattern": r"^\s*error\s*$"},
                                    "minItems": 1,
                                    "maxItems": 1,
                                },
                            ]
                        }
                    },
                    "not": {"anyOf": [{"required": ["summary"]}, {"required": ["result"]}]},
                },
                {
                    "required": ["type"],
                    "properties": {
                        "type": {
                            "oneOf": [
                                {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": YIELD_PROGRESS_MAX_TYPE_CHARS,
                                    "pattern": r"\S",
                                    "not": {"pattern": r"^\s*(?:result|error)\s*$"},
                                },
                                {
                                    "type": "array",
                                    "items": {
                                        "type": "string",
                                        "minLength": 1,
                                        "maxLength": YIELD_PROGRESS_MAX_TYPE_CHARS,
                                        "pattern": r"\S",
                                        "not": {"pattern": r"^\s*(?:result|error)\s*$"},
                                    },
                                    "minItems": 1,
                                    "maxItems": YIELD_PROGRESS_MAX_SECTIONS,
                                },
                            ]
                        }
                    },
                    "not": {"anyOf": [{"required": ["summary"]}, {"required": ["error"]}]},
                    "anyOf": [
                        {"required": ["result"]},
                        {"required": ["data"], "properties": {"data": {"minProperties": 1}}},
                    ],
                },
            ],
        },
        effects=frozenset({ToolEffect.SESSION}),
        replay_policy="never",
    )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        context.require_session_id()
        if not context.parent_session_id:
            raise ValueError("yield is only available to delegated child sessions")
        args = parse_tool_args(YieldArgs, call.arguments, tool_name=self.definition.name)
        if not args.is_terminal():
            progress = args.progress_control()
            content = args.result if args.result is not None else json.dumps(json_wire_object(progress.data), ensure_ascii=False)
            return ToolSuccess(
                tool_name=self.definition.name,
                output=TextOutput(
                    content,
                    presentation=content,
                    bounds=OutputBounds(reference=OutputReference(f"child-yield-progress:{context.session_id}")),
                ),
                control=progress,
            )
        if args.is_terminal_error():
            assert args.error is not None
            return ToolFailure(
                tool_name=self.definition.name,
                error=args.error,
                output=EmptyOutput(bounds=OutputBounds(reference=OutputReference(f"child-yield-error:{context.session_id}"))),
                control=TerminalYieldFailure(args.data),
            )
        assert args.summary is not None
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(
                args.summary,
                presentation=args.summary,
                bounds=OutputBounds(reference=OutputReference(f"child-yield:{context.session_id}")),
            ),
            control=TerminalYield(args.summary, args.data),
        )


__all__ = [
    "YIELD_PROGRESS_MAX_RETAINED_CHARS",
    "YIELD_PROGRESS_MAX_SECTION_CHARS",
    "YIELD_PROGRESS_MAX_SECTIONS",
    "YieldArgs",
    "YieldTool",
]
