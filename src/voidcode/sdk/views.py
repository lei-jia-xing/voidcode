"""Declarative, bounded UI view descriptors contributed by plugins."""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Literal, Protocol, runtime_checkable

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
    argument_name: str = "choice"


@dataclass(frozen=True, slots=True)
class ConfirmationViewData:
    request_id: str
    message: str
    actions: tuple[str, ...] = ("allow", "deny")


FormFieldType = Literal["text", "multiline", "integer", "number", "boolean"]


@dataclass(frozen=True, slots=True)
class FormField:
    name: str
    label: str
    field_type: FormFieldType = "text"
    required: bool = True


@dataclass(frozen=True, slots=True)
class FormViewData:
    fields: tuple[FormField, ...]
    action: str
    initial_values: Mapping[str, object] = field(default_factory=dict)
    load_action: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.initial_values, Mapping):
            raise ValueError("initial_values must be a mapping")
        object.__setattr__(self, "initial_values", own_json_object(self.initial_values))


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


def _canonical_wire(value: object) -> object:
    """Serialize a validated view model to wire JSON, including declared field defaults."""
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _canonical_wire(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Mapping):
        return {str(k): _canonical_wire(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_canonical_wire(v) for v in value]
    return value


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
    canonical = _canonical_wire(parsed)
    if not isinstance(canonical, Mapping):
        raise ValueError(f"View data for {view_type} must serialize to an object")
    return own_json_object(canonical)


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


@dataclass(frozen=True, slots=True)
class ViewsSnapshot:
    """Rendered views together with the capability revision of the snapshot that rendered them.

    A view rendered under one generation is never relabelled with a later revision:
    the client refreshes both together after an activation commit.
    """

    revision_id: str
    views: tuple[PluginViewDescriptor, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.revision_id, str) or not self.revision_id:
            raise ValueError("views snapshot requires the capability revision that rendered it")
        object.__setattr__(self, "views", tuple(self.views))

    def as_payload(self) -> dict[str, object]:
        return {
            "revision_id": self.revision_id,
            "views": [view.as_payload() for view in self.views],
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
