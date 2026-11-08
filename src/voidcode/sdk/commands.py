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


_SHORTCUT_MODIFIERS: tuple[str, ...] = ("shift", "ctrl", "alt", "super")
_SHORTCUT_MODIFIER_SET: frozenset[str] = frozenset(_SHORTCUT_MODIFIERS)
#: Canonical default-shortcut chords stay short enough for every client keymap.
_MAX_SHORTCUT_CHARS: int = 32


def normalize_default_shortcut(value: str) -> str:
    """Canonical ``modifier+...+base`` chord (Alt+A becomes ``alt+a``)."""
    if not isinstance(value, str):
        raise ValueError("default shortcut must be a string")
    text = value.strip().lower()
    if not text or len(text) > _MAX_SHORTCUT_CHARS:
        raise ValueError(f"default shortcut must be 1..{_MAX_SHORTCUT_CHARS} printable characters")
    parts = text.split("+")
    if len(parts) < 2:
        raise ValueError("default shortcut must carry at least one modifier (e.g. alt+a)")
    *modifiers, base = parts
    if not base or base in _SHORTCUT_MODIFIER_SET:
        raise ValueError("default shortcut needs a non-modifier base key")
    if any(modifier not in _SHORTCUT_MODIFIER_SET for modifier in modifiers):
        raise ValueError("default shortcut modifiers must be ctrl, alt, shift, or super")
    if len(set(modifiers)) != len(modifiers):
        raise ValueError("default shortcut modifiers must not repeat")
    for char in text:
        if char != "+" and (ord(char) < 33 or ord(char) > 126):
            raise ValueError("default shortcut must be printable without whitespace")
    ordered = sorted(modifiers, key=_SHORTCUT_MODIFIERS.index)
    return "+".join([*ordered, base])


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
    has_input: bool = False
    slash_visible: bool = True
    text_argument: str | None = None
    #: Canonical default key chord clients may bind (None means no default).
    default_shortcut: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argument_schema", own_json_object(self.argument_schema))
        if self.default_shortcut is not None:
            object.__setattr__(self, "default_shortcut", normalize_default_shortcut(self.default_shortcut))

    def as_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "owner": self.owner,
            "description": self.description,
            "argument_schema": json_wire_object(self.argument_schema),
            "effects": [e.value for e in self.effects],
            "busy_gate": self.busy_gate,
            "client_syntax": self.client_syntax,
            "has_input": self.has_input,
            "slash_visible": self.slash_visible,
            "text_argument": self.text_argument,
            "default_shortcut": self.default_shortcut,
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
class CommandPresentation:
    """Bounded command UI, mounted only from a direct receipt, never replay."""

    title: str
    view_type: Literal["notice", "markdown", "fields", "choices", "form", "diff", "status"]
    data: Mapping[str, object]
    #: Advisory client refresh cadence in ms; only carrying views refresh, only while open.
    refresh_after_ms: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("command presentation title must be non-empty")
        if self.view_type == "confirmation":
            raise ValueError("command presentations cannot carry confirmations")
        if self.refresh_after_ms is not None:
            if type(self.refresh_after_ms) is not int or not 250 <= self.refresh_after_ms <= 60000:
                raise ValueError("refresh_after_ms must be an integer in 250..60000")
        from .views import validate_view_data

        object.__setattr__(self, "data", validate_view_data(self.view_type, self.data))

    def as_payload(self) -> dict[str, object]:
        return {
            "title": self.title,
            "view_type": self.view_type,
            "data": json_wire_object(self.data),
            "refresh_after_ms": self.refresh_after_ms,
        }


@dataclass(frozen=True, slots=True)
class CommandInputSnapshot:
    """Read-only prepared input bound to the generation that produced it."""

    revision_id: str
    presentation: CommandPresentation

    def __post_init__(self) -> None:
        if not self.revision_id:
            raise ValueError("command input requires its captured revision")

    def as_payload(self) -> dict[str, object]:
        return {"revision_id": self.revision_id, "presentation": self.presentation.as_payload()}

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CommandInputSnapshot:
        return _decode_command(_INPUT_ADAPTER, payload, "command input")


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of a command invocation, returning data, error, or confirmation request."""

    status: Literal["ok", "error", "pending_confirmation"]
    output: str | None = None
    error: str | None = None
    data: Mapping[str, object] | None = None
    confirmation: ConfirmationRequest | None = None
    presentation: CommandPresentation | None = None

    def __post_init__(self) -> None:
        if self.presentation is not None and self.status != "ok":
            raise ValueError("only successful commands may carry a presentation")
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
            "presentation": None if self.presentation is None else self.presentation.as_payload(),
        }

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> CommandResult:
        return _decode_command(_RESULT_ADAPTER, payload, "command result")


_CONFIRMATION_REQUEST_ADAPTER: TypeAdapter[ConfirmationRequest] = TypeAdapter(ConfirmationRequest)
_DESCRIPTOR_ADAPTER: TypeAdapter[CommandDescriptor] = TypeAdapter(CommandDescriptor)
_CATALOG_ADAPTER: TypeAdapter[CommandCatalog] = TypeAdapter(CommandCatalog)
_RESULT_ADAPTER: TypeAdapter[CommandResult] = TypeAdapter(CommandResult)
_INPUT_ADAPTER: TypeAdapter[CommandInputSnapshot] = TypeAdapter(CommandInputSnapshot)


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
    #: Optional string property receiving natural slash trailing text.
    text_argument: str | None = None
    #: Internal UI action helpers remain callable but need not appear in slash discovery.
    slash_visible: bool = True
    #: Canonical default key chord (e.g. "alt+a"); clients bind it unless reserved or conflicting.
    default_shortcut: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "argument_schema", own_json_object(self.argument_schema))
        if not isinstance(self.effects, frozenset):
            object.__setattr__(self, "effects", frozenset(self.effects))
        if self.default_shortcut is not None:
            object.__setattr__(self, "default_shortcut", normalize_default_shortcut(self.default_shortcut))


@runtime_checkable
class Command(Protocol):
    """A registered plugin command executable via core."""

    @property
    def definition(self) -> CommandDefinition: ...

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult: ...


@runtime_checkable
class CommandInput(Protocol):
    """Optional command input preparation, called with strictly read-only services."""

    def prepare_input(self, invocation: CommandInvocation, services: PluginServices) -> CommandPresentation: ...


__all__ = [
    "Command",
    "CommandCatalog",
    "CommandDefinition",
    "CommandDescriptor",
    "CommandInvocation",
    "CommandInput",
    "CommandInputSnapshot",
    "CommandResult",
    "CommandPresentation",
    "ConfirmationRequest",
    "ConfirmationUIType",
    "normalize_default_shortcut",
]
