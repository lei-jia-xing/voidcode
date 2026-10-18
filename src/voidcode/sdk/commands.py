"""Narrow typed command declarations and invocation models."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .invocation import ToolContext
from .json_values import json_wire_object, own_json_object
from .services import PluginServices
from .tools import ToolEffect

ConfirmationUIType = Literal["notice", "markdown", "fields", "choices", "confirmation", "form", "diff", "status"]


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


@dataclass(frozen=True, slots=True)
class CommandDescriptor:
    """Catalog entry describing an admitted plugin command or reserved core command."""

    name: str
    owner: str
    description: str
    argument_schema: Mapping[str, object] = field(default_factory=dict)
    effects: tuple[ToolEffect, ...] = ()
    capabilities: tuple[str, ...] = ()
    busy_gate: bool = False
    client_capabilities: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "argument_schema", own_json_object(self.argument_schema))

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "description": self.description,
            "argument_schema": json_wire_object(self.argument_schema),
            "effects": [e.value for e in self.effects],
            "capabilities": list(self.capabilities),
            "busy_gate": self.busy_gate,
            "client_capabilities": list(self.client_capabilities),
        }


@dataclass(frozen=True, slots=True)
class CommandInvocation:
    """Context and arguments handed to a command handler."""

    context: ToolContext
    name: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    client_capabilities: frozenset[str] | Sequence[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))
        if not isinstance(self.client_capabilities, frozenset):
            object.__setattr__(self, "client_capabilities", frozenset(self.client_capabilities))


@dataclass(frozen=True, slots=True)
class CommandDefinition:
    """Declared interface and requirements of a registered plugin command."""

    name: str
    description: str
    argument_schema: Mapping[str, object] = field(default_factory=dict)
    effects: frozenset[ToolEffect] | Sequence[ToolEffect] = frozenset()
    capabilities: frozenset[str] | Sequence[str] = frozenset()
    busy_gate: bool = False
    client_capabilities: frozenset[str] | Sequence[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "argument_schema", own_json_object(self.argument_schema))
        if not isinstance(self.effects, frozenset):
            object.__setattr__(self, "effects", frozenset(self.effects))
        if not isinstance(self.capabilities, frozenset):
            object.__setattr__(self, "capabilities", frozenset(self.capabilities))
        if not isinstance(self.client_capabilities, frozenset):
            object.__setattr__(self, "client_capabilities", frozenset(self.client_capabilities))


@runtime_checkable
class Command(Protocol):
    """A registered plugin command executable via kernel."""

    @property
    def definition(self) -> CommandDefinition: ...

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult: ...
