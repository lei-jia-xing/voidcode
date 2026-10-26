"""Declarative, bounded UI view descriptors contributed by plugins."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import TypeAdapter

from .commands import CommandCatalog, ConfirmationUIType
from .json_values import json_wire_object, own_json_object
from .services import PluginServices
from .tools import ToolEffect


@dataclass(frozen=True, slots=True)
class NoticeViewData:
    message: str
    level: Literal["info", "warning", "error"] = "info"

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> NoticeViewData:
        return _decode_payload(_VIEW_ADAPTERS["notice"], payload)


@dataclass(frozen=True, slots=True)
class MarkdownViewData:
    content: str

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> MarkdownViewData:
        return _decode_payload(_VIEW_ADAPTERS["markdown"], payload)


@dataclass(frozen=True, slots=True)
class FieldRow:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class FieldsViewData:
    rows: tuple[FieldRow, ...]

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> FieldsViewData:
        return _decode_payload(_VIEW_ADAPTERS["fields"], payload)


@dataclass(frozen=True, slots=True)
class ChoiceOption:
    id: str
    label: str


@dataclass(frozen=True, slots=True)
class ChoicesViewData:
    options: tuple[ChoiceOption, ...]
    action: str
    argument_name: str = "choice"

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ChoicesViewData:
        return _decode_payload(_VIEW_ADAPTERS["choices"], payload)


@dataclass(frozen=True, slots=True)
class ConfirmationViewData:
    request_id: str
    message: str
    actions: tuple[str, ...] = ("allow", "deny")

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ConfirmationViewData:
        return _decode_payload(_VIEW_ADAPTERS["confirmation"], payload)


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

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> FormViewData:
        return _decode_payload(_VIEW_ADAPTERS["form"], payload)


@dataclass(frozen=True, slots=True)
class DiffViewData:
    before: str
    after: str
    path: str | None = None

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> DiffViewData:
        return _decode_payload(_VIEW_ADAPTERS["diff"], payload)


@dataclass(frozen=True, slots=True)
class StatusViewData:
    label: str
    value: str

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> StatusViewData:
        return _decode_payload(_VIEW_ADAPTERS["status"], payload)


_VIEW_ADAPTERS: dict[str, TypeAdapter[Any]] = {
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


def _decode_payload[T](adapter: TypeAdapter[T], payload: Mapping[str, object]) -> T:
    if not isinstance(payload, Mapping):
        raise ValueError("view data payload must be an object")
    return adapter.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


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
    canonical = asdict(parsed)
    return own_json_object(canonical)


type ViewData = (
    NoticeViewData | MarkdownViewData | FieldsViewData | ChoicesViewData | ConfirmationViewData | FormViewData | DiffViewData | StatusViewData
)


def parse_view_data(view_type: ConfirmationUIType, data: Mapping[str, object]) -> ViewData:
    """Decode owned view data through the same strict adapters as admission."""
    adapter = _VIEW_ADAPTERS.get(view_type)
    if adapter is None:
        raise ValueError(f"Unknown view_type: {view_type}")
    if not isinstance(data, Mapping):
        raise ValueError("view data payload must be an object")
    return adapter.validate_json(json.dumps(json_wire_object(data), allow_nan=False), strict=True, extra="forbid")


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

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> PluginViewDescriptor:
        if not isinstance(payload, Mapping):
            raise ValueError("view descriptor payload must be an object")
        return _DESCRIPTOR_ADAPTER.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


_DESCRIPTOR_ADAPTER: TypeAdapter[PluginViewDescriptor] = TypeAdapter(PluginViewDescriptor)


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

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ViewsSnapshot:
        if not isinstance(payload, Mapping):
            raise ValueError("views snapshot payload must be an object")
        return _SNAPSHOT_ADAPTER.validate_json(json.dumps(json_wire_object(payload), allow_nan=False), strict=True, extra="forbid")


_SNAPSHOT_ADAPTER: TypeAdapter[ViewsSnapshot] = TypeAdapter(ViewsSnapshot)


@runtime_checkable
class PluginView(Protocol):
    """Declarative view provided by a plugin for session/workspace presentation."""

    @property
    def name(self) -> str: ...

    def render(
        self,
        session_id: str,
        services: PluginServices,
    ) -> PluginViewDescriptor: ...


def choices_dispatch(view: ChoicesViewData, option_id: str) -> tuple[str, dict[str, object]]:
    """Dispatch one choices selection: (action, {argument_name: option_id})."""
    for option in view.options:
        if option.id == option_id:
            return view.action, {view.argument_name: option_id}
    raise ValueError(f"unknown choice option: {option_id!r}")


#: Strict numeric literals: no silent truncation, no scientific-integer coercion.
_INTEGER_PATTERN = re.compile(r"^[+-]?\d+$")
_NUMBER_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


def _field_schema_allows_null(schema: Mapping[str, object]) -> bool:
    """Whether a JSON Schema (Draft 2020-12) admits ``null`` for this position."""
    field_type = schema.get("type")
    if field_type == "null":
        return True
    if isinstance(field_type, (list, tuple)) and "null" in field_type:
        return True
    for keyword in ("anyOf", "oneOf"):
        branches = schema.get(keyword)
        if isinstance(branches, (list, tuple)):
            if any(isinstance(branch, Mapping) and _field_schema_allows_null(branch) for branch in branches):
                return True
    return False


def _allows_null(action_schema: Mapping[str, object] | None, name: str) -> bool:
    properties = (action_schema or {}).get("properties")
    if not isinstance(properties, Mapping):
        return False
    field_schema = properties.get(name)
    return isinstance(field_schema, Mapping) and _field_schema_allows_null(field_schema)


def validate_form_submission(
    view: FormViewData,
    values: Mapping[str, object],
    *,
    action_schema: Mapping[str, object] | None = None,
) -> dict[str, object]:
    """Validate one form draft into command arguments, failing visibly.

    Absent (never loaded, never typed) is not the same as present-but-blank
    (the user cleared it): only the latter may clear a nullable value. Extra
    keys beyond the declared fields (load_action metadata) are ignored.
    """
    arguments: dict[str, object] = {}
    for form_field in view.fields:
        name = form_field.name
        label = form_field.label or name
        raw = values.get(name)

        # Absent (never loaded, never typed) is not the same as present-but-blank
        # (the user cleared it): only the latter may clear a nullable value.
        present = name in values

        if form_field.field_type == "boolean":
            if raw is True or raw is False:
                arguments[name] = raw
                continue
            if present and raw is not None and not isinstance(raw, bool) and not (isinstance(raw, str) and not raw.strip()):
                raise ValueError(f"Field '{label}' must be a boolean.")
            if form_field.required and (not present or not _allows_null(action_schema, name)):
                raise ValueError(f"Field '{label}' is required.")
            if present and _allows_null(action_schema, name):
                arguments[name] = None
                continue
            continue  # absent or optional non-nullable: omit

        if form_field.field_type == "integer" and isinstance(raw, int) and not isinstance(raw, bool):
            arguments[name] = raw
            continue
        if form_field.field_type == "number" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            if not math.isfinite(float(raw)):
                raise ValueError(f"Field '{label}' must be a finite number.")
            arguments[name] = raw
            continue
        if present and raw is not None and not isinstance(raw, str):
            raise ValueError(f"Field '{label}' must be text.")
        text = "" if raw is None else str(raw)
        if not text.strip():
            if form_field.required and (not present or not _allows_null(action_schema, name)):
                raise ValueError(f"Field '{label}' is required.")
            if present and _allows_null(action_schema, name):
                # A deliberate blank clears the nullable value.
                arguments[name] = None
                continue
            # Absent or optional non-nullable: omit rather than fabricate.
            continue
        # Numeric candidates are trimmed for parsing only; text stays verbatim.
        candidate = text.strip()
        if form_field.field_type == "integer":
            if not _INTEGER_PATTERN.match(candidate):
                raise ValueError(f"Field '{label}' must be an integer.")
            arguments[name] = int(candidate)
        elif form_field.field_type == "number":
            if not _NUMBER_PATTERN.match(candidate):
                raise ValueError(f"Field '{label}' must be a number.")
            value = float(candidate)
            if not math.isfinite(value):
                raise ValueError(f"Field '{label}' must be a finite number.")
            arguments[name] = value
        else:
            # Text is preserved exactly as typed, including whitespace.
            arguments[name] = text
    return arguments


def is_read_only_load(view: FormViewData, catalog: CommandCatalog) -> bool:
    """Whether the form's load_action is a catalog command declaring exactly read."""
    if view.load_action is None:
        return False
    for command in catalog.commands:
        if command.name == view.load_action:
            return command.effects == (ToolEffect.READ,)
    return False
