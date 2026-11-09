"""Strict JSON boundary for durable event payloads and provider transcripts."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .json_values import json_wire_object
from .provider import ProviderMessage, ProviderTurnResult
from .services import ArtifactReference
from .tools import (
    AttachmentOutput,
    ConfirmedStop,
    EmptyOutput,
    OpaqueToolBody,
    OutputBounds,
    ReadObservation,
    ResourceChange,
    TextOutput,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolEffect,
    ToolFailure,
    ToolOutput,
    ToolResult,
    ToolSuccess,
    UnconfirmedStop,
)


def _wire(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _wire(getattr(value, item.name)) for item in fields(value) if item.name != "abort_signal"}
    if isinstance(value, Mapping):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_wire(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _encode(value: object) -> dict[str, object]:
    encoded = _wire(value)
    if not isinstance(encoded, dict):
        raise ValueError("wire payload must be an object")
    return encoded


def _decode[T](adapter: TypeAdapter[T], payload: Mapping[str, object]) -> T:
    if not isinstance(payload, Mapping):
        raise ValueError("wire payload must be an object")
    return adapter.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


_CALL = TypeAdapter(ToolCall)
_DEFINITION = TypeAdapter(ToolDefinition)
_MESSAGE = TypeAdapter(ProviderMessage)
_PROVIDER_RESULT = TypeAdapter(ProviderTurnResult)


def encode_tool_call(call: ToolCall | Mapping[str, object]) -> dict[str, object]:
    return _encode(call)


def decode_tool_call(payload: Mapping[str, object]) -> ToolCall:
    call = _decode(_CALL, payload)
    if not call.tool_name:
        raise ValueError("tool_name must be nonempty")
    return call


def encode_tool_definition(definition: ToolDefinition | Mapping[str, object]) -> dict[str, object]:
    return _encode(definition)


def decode_tool_definition(payload: Mapping[str, object]) -> ToolDefinition:
    definition = _decode(_DEFINITION, payload)
    if not definition.name or not definition.effects:
        raise ValueError("tool definition requires a name and declared effects")
    if definition.replay_policy == "safe" and definition.effects & {ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.SPAWN, ToolEffect.SESSION}:
        raise ValueError("mutating tools cannot declare safe replay")
    return definition


def encode_message(message: ProviderMessage | Mapping[str, object]) -> dict[str, object]:
    return _encode(message)


def decode_message(payload: Mapping[str, object]) -> ProviderMessage:
    return _decode(_MESSAGE, payload)


class WireModel(BaseModel):
    """Strict wire-model base shared by codec and history wire docs."""

    model_config = ConfigDict(extra="forbid", strict=True)


def encode_provider_result(result: ProviderTurnResult | Mapping[str, object]) -> dict[str, object]:
    return _encode(result)


def decode_provider_result(payload: Mapping[str, object]) -> ProviderTurnResult:
    return _decode(_PROVIDER_RESULT, payload)


class OutputBoundsWire(WireModel):
    truncated: bool = False
    partial: bool = False
    reference: ArtifactReference | str | None = None
    source: str | None = None
    fallback_reason: str | None = None


class TextOutputWire(WireModel):
    type: Literal["text"] = "text"
    text: str
    presentation: str | None = None
    bounds: OutputBoundsWire = OutputBoundsWire()


class EmptyOutputWire(WireModel):
    type: Literal["empty"] = "empty"
    presentation: str | None = None
    bounds: OutputBoundsWire = OutputBoundsWire()


class AttachmentOutputWire(WireModel):
    type: Literal["attachment"] = "attachment"
    mime: str
    data_uri: str | None
    presentation: str | None = None
    bounds: OutputBoundsWire = OutputBoundsWire()


class ExecutionWire(WireModel):
    cancellation_signalled: bool
    execution_stopped: bool
    side_effect_state: Literal["settled", "unknown"]


class ReadObservationWire(WireModel):
    path: str = Field(min_length=1)
    content_hash: str
    lines: list[int] = Field(default_factory=list)
    line_count: int = Field(default=0, ge=0)
    whole_file: bool = False
    clipped: bool = False


class ResourceChangeWire(WireModel):
    path: str = Field(min_length=1)
    operation: Literal["create", "update", "delete", "move"] = "update"
    old_path: str | None = None


class ToolResultWire(WireModel):
    status: Literal["ok", "error"]
    tool_name: str = Field(min_length=1)
    output: Annotated[TextOutputWire | EmptyOutputWire | AttachmentOutputWire, Field(discriminator="type")]
    body: dict[str, object] | None = None
    #: Only ever sent by a read-shaped success; absent means "made no file visible".
    coverage: list[ReadObservationWire] | None = None
    changes: list[ResourceChangeWire] | None = None
    error: str | None = None
    diagnostics: ToolDiagnostics | None = None
    execution: ExecutionWire | None = None
    timeout_seconds: int | None = Field(default=None, ge=0)
    steering_context: str | None = None


_RESULT = TypeAdapter(ToolResultWire)


def encode_tool_result(result: ToolResult | Mapping[str, object]) -> dict[str, object]:
    if isinstance(result, Mapping):
        return _encode(result)
    output: ToolOutput = result.output
    output_type = "text" if isinstance(output, TextOutput) else "empty" if isinstance(output, EmptyOutput) else "attachment"
    payload = {
        "status": result.status,
        "tool_name": result.tool_name,
        "output": {"type": output_type, **_encode(output)},
        "body": None if result.body is None else result.body.as_payload(),
    }
    if result.steering_context is not None:
        payload["steering_context"] = result.steering_context
    if isinstance(result, ToolSuccess) and result.coverage:
        payload["coverage"] = [observation.as_payload() for observation in result.coverage]
    if result.changes:
        payload["changes"] = [_encode(change) for change in result.changes]
    if isinstance(result, ToolFailure):
        execution = result.execution
        payload.update(
            {
                "error": result.error,
                "diagnostics": None if result.diagnostics is None else _encode(result.diagnostics),
                "execution": None
                if execution is None
                else {
                    "cancellation_signalled": execution.cancellation_signalled,
                    "execution_stopped": execution.execution_stopped,
                    "side_effect_state": execution.side_effect_state,
                },
                "timeout_seconds": result.timeout_seconds,
            }
        )
    return payload


def decode_tool_result(payload: Mapping[str, object]) -> ToolResult:
    wire = _decode(_RESULT, payload)
    output = wire.output
    bounds = output.bounds
    decoded_bounds = OutputBounds(bounds.truncated, bounds.partial, bounds.reference, bounds.source, bounds.fallback_reason)
    if isinstance(output, TextOutputWire):
        decoded = TextOutput(output.text, output.presentation, decoded_bounds)
    elif isinstance(output, EmptyOutputWire):
        decoded = EmptyOutput(output.presentation, decoded_bounds)
    else:
        decoded = AttachmentOutput(output.mime, output.data_uri, output.presentation, decoded_bounds)
    body = None if wire.body is None else OpaqueToolBody(wire.body)
    if wire.status == "ok":
        if any(value is not None for value in (wire.error, wire.diagnostics, wire.execution, wire.timeout_seconds)):
            raise ValueError("success result contains failure-only fields")
        coverage = tuple(
            ReadObservation(item.path, item.content_hash, tuple(item.lines), item.line_count, item.whole_file, item.clipped)
            for item in wire.coverage or ()
        )
        return ToolSuccess(
            wire.tool_name,
            decoded,
            body,
            coverage,
            tuple(ResourceChange(item.path, item.operation, item.old_path) for item in wire.changes or ()),
            steering_context=wire.steering_context,
        )
    if wire.error is None:
        raise ValueError("failure result requires an error")
    if wire.coverage:
        raise ValueError("failure result must not carry read coverage")
    execution = None
    if wire.execution is not None:
        observation = wire.execution
        if observation.side_effect_state != ("settled" if observation.execution_stopped else "unknown"):
            raise ValueError("execution observation contradicts its side-effect state")
        if observation.execution_stopped:
            execution = ConfirmedStop(observation.cancellation_signalled)
        else:
            execution = UnconfirmedStop(observation.cancellation_signalled)
    return ToolFailure(
        wire.tool_name,
        wire.error,
        decoded,
        body,
        wire.diagnostics,
        execution,
        wire.timeout_seconds,
        tuple(ResourceChange(item.path, item.operation, item.old_path) for item in wire.changes or ()),
        steering_context=wire.steering_context,
    )
