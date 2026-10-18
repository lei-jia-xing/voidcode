"""Strict JSON boundary shared by the kernel and plugin host."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .commands import (
    CommandDescriptor,
    CommandInvocation,
    CommandResult,
    ConfirmationRequest,
)
from .invocation import ToolContext
from .json_values import json_wire_object
from .provider import ProviderCacheRetention, ProviderExecutionError, ProviderMessage, ProviderTurnRequest, ProviderTurnResult
from .services import ContinuationResult, PluginBudget, SessionUsageReport
from .tools import (
    AttachmentOutput,
    ConfirmedStop,
    EmptyOutput,
    OpaqueToolBody,
    OutputBounds,
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
from .views import PluginViewDescriptor


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
_PROVIDER_ERROR = TypeAdapter(ProviderExecutionError)


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
    if definition.replay_policy == "safe" and definition.effects & {ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.SPAWN}:
        raise ValueError("mutating tools cannot declare safe replay")
    return definition


def encode_message(message: ProviderMessage | Mapping[str, object]) -> dict[str, object]:
    return _encode(message)


def decode_message(payload: Mapping[str, object]) -> ProviderMessage:
    return _decode(_MESSAGE, payload)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _ProviderRequest(_WireModel):
    messages: tuple[ProviderMessage, ...]
    available_tools: tuple[ToolDefinition, ...] = ()
    raw_model: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    reasoning_effort: str | None = None
    cache_retention: ProviderCacheRetention | None = None
    attempt: int = Field(default=0, ge=0)
    metadata: dict[str, object] = Field(default_factory=dict)


_REQUEST = TypeAdapter(_ProviderRequest)


def encode_provider_request(request: ProviderTurnRequest | Mapping[str, object]) -> dict[str, object]:
    return _encode(request)


def decode_provider_request(payload: Mapping[str, object]) -> ProviderTurnRequest:
    wire = _decode(_REQUEST, payload)
    return ProviderTurnRequest(
        messages=wire.messages,
        available_tools=wire.available_tools,
        raw_model=wire.raw_model,
        provider_name=wire.provider_name,
        model_name=wire.model_name,
        reasoning_effort=wire.reasoning_effort,
        cache_retention=wire.cache_retention,
        attempt=wire.attempt,
        metadata=wire.metadata,
    )


def encode_provider_result(result: ProviderTurnResult | Mapping[str, object]) -> dict[str, object]:
    return _encode(result)


def decode_provider_result(payload: Mapping[str, object]) -> ProviderTurnResult:
    return _decode(_PROVIDER_RESULT, payload)


def encode_provider_error(error: ProviderExecutionError) -> dict[str, object]:
    return _encode(error)


def decode_provider_error(payload: Mapping[str, object]) -> ProviderExecutionError:
    return _decode(_PROVIDER_ERROR, payload)


class _Context(_WireModel):
    workspace: Path
    session_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    invocation_id: str = Field(min_length=1)
    revision_id: str = Field(min_length=1)
    actor: str = Field(min_length=1)
    capabilities: frozenset[str] = frozenset()
    logical_turn: str = ""


_CONTEXT = TypeAdapter(_Context)


def encode_context(context: ToolContext | Mapping[str, object]) -> dict[str, object]:
    return _encode(context)


def decode_context(payload: Mapping[str, object]) -> ToolContext:
    wire = _decode(_CONTEXT, payload)
    return ToolContext(
        workspace=wire.workspace,
        session_id=wire.session_id,
        run_id=wire.run_id,
        invocation_id=wire.invocation_id,
        revision_id=wire.revision_id,
        actor=wire.actor,
        capabilities=wire.capabilities,
        logical_turn=wire.logical_turn,
    )


class _Text(_WireModel):
    type: Literal["text"] = "text"
    text: str
    presentation: str | None = None
    bounds: OutputBounds = OutputBounds()


class _Empty(_WireModel):
    type: Literal["empty"] = "empty"
    presentation: str | None = None
    bounds: OutputBounds = OutputBounds()


class _Attachment(_WireModel):
    type: Literal["attachment"] = "attachment"
    mime: str
    data_uri: str | None
    presentation: str | None = None
    bounds: OutputBounds = OutputBounds()


class _Execution(_WireModel):
    cancellation_signalled: bool
    execution_stopped: bool
    side_effect_state: Literal["settled", "unknown"]


class _ToolResult(_WireModel):
    status: Literal["ok", "error"]
    tool_name: str = Field(min_length=1)
    output: Annotated[_Text | _Empty | _Attachment, Field(discriminator="type")]
    body: dict[str, object] | None = None
    error: str | None = None
    diagnostics: ToolDiagnostics | None = None
    execution: _Execution | None = None
    timeout_seconds: int | None = Field(default=None, ge=0)


_RESULT = TypeAdapter(_ToolResult)


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
    if isinstance(output, _Text):
        decoded = TextOutput(output.text, output.presentation, output.bounds)
    elif isinstance(output, _Empty):
        decoded = EmptyOutput(output.presentation, output.bounds)
    else:
        decoded = AttachmentOutput(output.mime, output.data_uri, output.presentation, output.bounds)
    body = None if wire.body is None else OpaqueToolBody(wire.body)
    if wire.status == "ok":
        if any(value is not None for value in (wire.error, wire.diagnostics, wire.execution, wire.timeout_seconds)):
            raise ValueError("success result contains failure-only fields")
        return ToolSuccess(wire.tool_name, decoded, body)
    if wire.error is None:
        raise ValueError("failure result requires an error")
    execution = None
    if wire.execution is not None:
        observation = wire.execution
        if observation.side_effect_state != ("settled" if observation.execution_stopped else "unknown"):
            raise ValueError("execution observation contradicts its side-effect state")
        if observation.execution_stopped:
            execution = ConfirmedStop(observation.cancellation_signalled)
        else:
            execution = UnconfirmedStop(observation.cancellation_signalled)
    return ToolFailure(wire.tool_name, wire.error, decoded, body, wire.diagnostics, execution, wire.timeout_seconds)


_COMMAND_DESCRIPTOR = TypeAdapter(CommandDescriptor)
_COMMAND_INVOCATION = TypeAdapter(CommandInvocation)
_CONFIRMATION_REQUEST = TypeAdapter(ConfirmationRequest)
_COMMAND_RESULT = TypeAdapter(CommandResult)
_VIEW_DESCRIPTOR = TypeAdapter(PluginViewDescriptor)
_CONTINUATION_RESULT = TypeAdapter(ContinuationResult)
_SESSION_USAGE = TypeAdapter(SessionUsageReport)


def encode_command_descriptor(descriptor: CommandDescriptor | Mapping[str, object]) -> dict[str, object]:
    return _encode(descriptor)


def decode_command_descriptor(payload: Mapping[str, object]) -> CommandDescriptor:
    return _decode(_COMMAND_DESCRIPTOR, payload)


def encode_command_invocation(invocation: CommandInvocation | Mapping[str, object]) -> dict[str, object]:
    return _encode(invocation)


def decode_command_invocation(payload: Mapping[str, object]) -> CommandInvocation:
    return _decode(_COMMAND_INVOCATION, payload)


def encode_command_result(result: CommandResult | Mapping[str, object]) -> dict[str, object]:
    return _encode(result)


def decode_command_result(payload: Mapping[str, object]) -> CommandResult:
    return _decode(_COMMAND_RESULT, payload)


def encode_confirmation_request(request: ConfirmationRequest | Mapping[str, object]) -> dict[str, object]:
    return _encode(request)


def decode_confirmation_request(payload: Mapping[str, object]) -> ConfirmationRequest:
    return _decode(_CONFIRMATION_REQUEST, payload)


def encode_view_descriptor(descriptor: PluginViewDescriptor | Mapping[str, object]) -> dict[str, object]:
    return _encode(descriptor)


def decode_view_descriptor(payload: Mapping[str, object]) -> PluginViewDescriptor:
    return _decode(_VIEW_DESCRIPTOR, payload)


def encode_continuation_result(result: ContinuationResult | Mapping[str, object]) -> dict[str, object]:
    return _encode(result)


def decode_continuation_result(payload: Mapping[str, object]) -> ContinuationResult:
    return _decode(_CONTINUATION_RESULT, payload)


def encode_session_usage(report: SessionUsageReport | Mapping[str, object]) -> dict[str, object]:
    return _encode(report)


def decode_session_usage(payload: Mapping[str, object]) -> SessionUsageReport:
    return _decode(_SESSION_USAGE, payload)


_BUDGET = TypeAdapter(PluginBudget)


def encode_budget(budget: PluginBudget | Mapping[str, object]) -> dict[str, object]:
    return _encode(budget)


def decode_budget(payload: Mapping[str, object]) -> PluginBudget:
    return _decode(_BUDGET, payload)
