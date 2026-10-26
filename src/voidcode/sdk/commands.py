"""Narrow typed command declarations and invocation models."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import TypeAdapter

from .invocation import ToolContext
from .json_values import json_wire_object, own_json_object
from .services import PluginServices
from .tools import ToolEffect

ConfirmationUIType = Literal["notice", "markdown", "fields", "choices", "confirmation", "form", "diff", "status"]


def _decode_command[T](adapter: TypeAdapter[T], payload: Mapping[str, object], label: str) -> T:
    if not isinstance(payload, Mapping):
        raise ValueError(f"{label} payload must be an object")
    return adapter.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


@dataclass(frozen=True, slots=True)
class ConfirmationRequest:
    """Request for client/user confirmation before performing a governed command effect."""

    request_id: str
    owner: str
    command_name: str
    arguments: Mapping[str, object]
    ui_type: ConfirmationUIType
    ui_data: Mapping[str, object]
    expected_state_version: int | None = None

    def __post_init__(self) -> None:
        if not self.request_id or not isinstance(self.request_id, str):
            raise ValueError("request_id must be a non-empty string")
        if not self.owner or not isinstance(self.owner, str):
            raise ValueError("owner must be a non-empty string")
        if not self.command_name or not isinstance(self.command_name, str):
            raise ValueError("command_name must be a non-empty string")
        if not isinstance(self.arguments, Mapping):
            raise ValueError("arguments must be a mapping")
        from .views import validate_view_data

        validated_ui_data = validate_view_data(self.ui_type, self.ui_data)
        object.__setattr__(self, "arguments", own_json_object(self.arguments))
        object.__setattr__(self, "ui_data", validated_ui_data)

    def as_payload(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "owner": self.owner,
            "command_name": self.command_name,
            "arguments": json_wire_object(self.arguments),
            "ui_type": self.ui_type,
            "ui_data": json_wire_object(self.ui_data),
            "expected_state_version": self.expected_state_version,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ConfirmationRequest:
        return _decode_command(_CONFIRMATION_REQUEST_ADAPTER, payload, "confirmation request")


@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    """Catalog entry describing a plugin command or reserved core command."""

    name: str
    owner: str
    description: str
    argument_schema: Mapping[str, object] = field(default_factory=dict)
    effects: tuple[ToolEffect, ...] = ()
    busy_gate: bool = False
    #: Declarative client syntax prefix, when the command declares one.
    client_syntax: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argument_schema", own_json_object(self.argument_schema))

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "description": self.description,
            "argument_schema": json_wire_object(self.argument_schema),
            "effects": [e.value for e in self.effects],
            "busy_gate": self.busy_gate,
            "client_syntax": self.client_syntax,
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CommandDescriptor:
        return _decode_command(_DESCRIPTOR_ADAPTER, payload, "command descriptor")


@dataclass(frozen=True, slots=True)
class CommandCatalog:
    """Command descriptors together with the capability revision they were captured from.

    Clients bind later actions to this revision, so a descriptor can never be
    displayed under one generation and executed under another.
    """

    revision_id: str
    commands: tuple[CommandDescriptor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.revision_id, str) or not self.revision_id:
            raise ValueError("command catalog requires the capability revision it was captured from")
        object.__setattr__(self, "commands", tuple(self.commands))

    def as_payload(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "commands": [command.as_payload() for command in self.commands],
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CommandCatalog:
        return _decode_command(_CATALOG_ADAPTER, payload, "command catalog")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a command invocation, returning data, error, or confirmation request."""

    status: Literal["ok", "error", "pending_confirmation"]
    output: str | None = None
    error: str | None = None
    data: Mapping[str, object] | None = None
    confirmation: ConfirmationRequest | None = None

    def __post_init__(self) -> None:
        if self.status == "pending_confirmation":
            if self.confirmation is None:
                raise ValueError("CommandResult with status 'pending_confirmation' requires a confirmation request")
        else:
            if self.confirmation is not None:
                raise ValueError(f"CommandResult with status '{self.status}' cannot carry a confirmation request")

        if self.status == "error":
            if not self.error or not isinstance(self.error, str) or not self.error.strip():
                raise ValueError("CommandResult with status 'error' requires a non-empty error message")

        if self.data is not None:
            object.__setattr__(self, "data", own_json_object(self.data))

    def as_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "data": None if self.data is None else json_wire_object(self.data),
            "confirmation": None if self.confirmation is None else self.confirmation.as_payload(),
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CommandResult:
        return _decode_command(_RESULT_ADAPTER, payload, "command result")


_CONFIRMATION_REQUEST_ADAPTER: TypeAdapter[ConfirmationRequest] = TypeAdapter(ConfirmationRequest)
_DESCRIPTOR_ADAPTER: TypeAdapter[CommandDescriptor] = TypeAdapter(CommandDescriptor)
_CATALOG_ADAPTER: TypeAdapter[CommandCatalog] = TypeAdapter(CommandCatalog)
_RESULT_ADAPTER: TypeAdapter[CommandResult] = TypeAdapter(CommandResult)


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """Context and arguments handed to a command handler."""

    context: ToolContext
    name: str
    arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    """Declared interface and requirements of a registered plugin command."""

    name: str
    description: str
    argument_schema: Mapping[str, object] = field(default_factory=dict)
    effects: frozenset[ToolEffect] | Sequence[ToolEffect] = frozenset()
    busy_gate: bool = False
    #: Declarative client syntax prefix (clients route it; the core never parses it).
    client_syntax: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argument_schema", own_json_object(self.argument_schema))
        if not isinstance(self.effects, frozenset):
            object.__setattr__(self, "effects", frozenset(self.effects))


@runtime_checkable
class Command(Protocol):
    """A registered plugin command executable via core."""

    @property
    def definition(self) -> CommandDefinition: ...

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult: ...


__all__ = [
    "Command",
    "CommandCatalog",
    "CommandDefinition",
    "CommandDescriptor",
    "CommandInvocation",
    "CommandResult",
    "ConfirmationRequest",
    "ConfirmationUIType",
]
