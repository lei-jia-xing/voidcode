"""Declarative, bounded UI view descriptors contributed by plugins."""

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol, cast, runtime_checkable

from pydantic import TypeAdapter

from .commands import ConfirmationUIType
from .json_values import json_wire_object, own_json_object
from .services import PluginServices


@dataclass(frozen=True, slots=True)
class NoticeViewData:
    message: str
    level: Literal["info", "warning", "error"] = "info"


@dataclass(frozen=True, slots=True)
class MarkdownViewData:
    content: str


@dataclass(frozen=True, slots=True)
class FieldRow:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class FieldsViewData:
    rows: tuple[FieldRow, ...]


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class ChoicesViewData:
    options: tuple[ChoiceOption, ...]
    action: str


@dataclass(frozen=True, slots=True)
class ConfirmationViewData:
    request_id: str
    message: str
    actions: tuple[str, ...] = ("allow", "deny")


@dataclass(frozen=True, slots=True)
class FormField:
    name: str
    label: str
    field_type: str = "text"


@dataclass(frozen=True, slots=True)
class FormViewData:
    fields: tuple[FormField, ...]
    action: str


@dataclass(frozen=True, slots=True)
class DiffViewData:
    before: str
    after: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class StatusViewData:
    label: str
    value: str


_VIEW_ADAPTERS: dict[str, TypeAdapter[object]] = {
    "notice": TypeAdapter(NoticeViewData),
    "markdown": TypeAdapter(MarkdownViewData),
    "fields": TypeAdapter(FieldsViewData),
    "choices": TypeAdapter(ChoicesViewData),
    "confirmation": TypeAdapter(ConfirmationViewData),
    "form": TypeAdapter(FormViewData),
    "diff": TypeAdapter(DiffViewData),
    "status": TypeAdapter(StatusViewData),
}

MAX_VIEW_DATA_BYTES = 64 * 1024


def validate_view_data(view_type: ConfirmationUIType, data: Mapping[str, object]) -> Mapping[str, object]:
    """Validate data shape matches view_type exactly, failing visibly on mismatch."""
    adapter = _VIEW_ADAPTERS.get(view_type)
    if adapter is None:
        raise ValueError(f"Unknown view_type: {view_type}")
    if not isinstance(data, Mapping):
        raise ValueError(f"View data for {view_type} must be an object")
    raw_json = json.dumps(json_wire_object(data), allow_nan=False)
    if len(raw_json.encode("utf-8")) > MAX_VIEW_DATA_BYTES:
        raise ValueError(f"View data exceeds {MAX_VIEW_DATA_BYTES} bytes limit")
    parsed = adapter.validate_json(raw_json, strict=True, extra="forbid")
    return own_json_object(asdict(cast(Any, parsed)))


@dataclass(frozen=True, slots=True)
class PluginViewDescriptor:
    """Bounded, declarative view presentation contributed by an admitted plugin."""

    owner: str
    name: str
    title: str
    view_type: ConfirmationUIType
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.owner or not isinstance(self.owner, str):
            raise ValueError("owner must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("name must be a non-empty string")
        if not self.title or not isinstance(self.title, str):
            raise ValueError("title must be a non-empty string")
        validated = validate_view_data(self.view_type, self.data)
        object.__setattr__(self, "data", validated)

    def as_payload(self) -> dict[str, object]:
        return {
            "owner": self.owner,
            "name": self.name,
            "title": self.title,
            "view_type": self.view_type,
            "data": json_wire_object(self.data),
        }


@runtime_checkable
class PluginView(Protocol):
    """Declarative view provided by a plugin for session/workspace presentation."""

    @property
    def name(self) -> str: ...

    def render(
        self,
        session_id: str,
        services: PluginServices,
        *,
        client_capabilities: frozenset[str] = frozenset(),
    ) -> PluginViewDescriptor: ...
