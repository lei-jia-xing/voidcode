from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..core.tool_context import ToolContext
from ..security.json_values import json_wire_object, own_json_object
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
from .output import create_tool_output_artifact

YIELD_PROGRESS_MAX_SECTIONS = 100
YIELD_PROGRESS_MAX_SECTION_CHARS = 4_096
YIELD_PROGRESS_MAX_RETAINED_CHARS = 65_536
YIELD_PROGRESS_MAX_TYPE_CHARS = 64


def _json_data(value: dict[str, object]) -> tuple[dict[str, object], str]:
    owned = own_json_object(value)
    encoded = json.dumps(json_wire_object(owned), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return dict(owned), encoded


def _data_artifact(*, call: ToolCall, session_id: str, encoded: str) -> OutputReference:
    artifact = create_tool_output_artifact(
        session_id=session_id,
        tool_call_id=call.tool_call_id,
        tool_name=call.tool_name,
        content=encoded,
        kind="structured_data",
        mime="application/json",
        source="yield",
    )
    uri = artifact.get("uri")
    if not isinstance(uri, str):
        raise ValueError("yield data artifact has no URI")
    return OutputReference(uri=uri, artifact=artifact)


def _visible_yield_content(
    *,
    label: str | None,
    encoded_data: str,
    reference: OutputReference | None,
) -> str:
    parts: list[str] = []
    if label is not None:
        parts.append(label[:1024])
    if encoded_data:
        parts.append("Data preview: " + encoded_data[:2048] + ("…" if len(encoded_data) > 2048 else ""))
    if reference is not None:
        parts.append(f"Full data: {reference.uri}")
    return "\n".join(parts)


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

    @field_validator("data", mode="after")
    @classmethod
    def _validate_data(cls, value: dict[str, object]) -> dict[str, object]:
        return dict(own_json_object(value))

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
        return ProgressYield(types, self.result, self.data)


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

        session_id = context.require_session_id()
        if not context.parent_session_id:
            raise ValueError("yield is only available to delegated child sessions")
        args = parse_tool_args(YieldArgs, call.arguments, tool_name=self.definition.name)
        data, encoded_data = _json_data(args.data)
        reference = _data_artifact(call=call, session_id=session_id, encoded=encoded_data) if data else None
        bounds = OutputBounds(
            truncated=len(encoded_data) > 2048,
            reference=reference,
        )
        if not args.is_terminal():
            progress = args.progress_control()
            content = _visible_yield_content(
                label=args.result,
                encoded_data=encoded_data if data else "",
                reference=reference,
            )
            return ToolSuccess(
                tool_name=self.definition.name,
                output=TextOutput(content, presentation=content, bounds=bounds),
                control=progress,
            )
        if args.is_terminal_error():
            assert args.error is not None
            return ToolFailure(
                tool_name=self.definition.name,
                error=args.error,
                output=EmptyOutput(bounds=bounds),
                control=TerminalYieldFailure(data),
            )
        assert args.summary is not None
        content = _visible_yield_content(
            label=args.summary,
            encoded_data=encoded_data if data else "",
            reference=reference,
        )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(content, presentation=content, bounds=bounds),
            control=TerminalYield(args.summary, data),
        )


__all__ = [
    "YIELD_PROGRESS_MAX_RETAINED_CHARS",
    "YIELD_PROGRESS_MAX_SECTION_CHARS",
    "YIELD_PROGRESS_MAX_SECTIONS",
    "YieldArgs",
    "YieldTool",
]
