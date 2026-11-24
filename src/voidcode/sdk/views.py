"""Declarative, bounded UI view descriptors contributed by plugins."""

import json
import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from typing import Annotated, Any, Literal, Protocol, runtime_checkable

from pydantic import Field, TypeAdapter

from .commands import CommandCatalog, ConfirmationUIType
from .json_values import json_wire_object, own_json_object
from .services import ArtifactReference, PluginServices
from .tools import ToolEffect

StyleTone = Literal["default", "info", "success", "warning", "error"]
StyleColorName = Literal[
    "black",
    "red",
    "green",
    "yellow",
    "blue",
    "magenta",
    "cyan",
    "white",
    "bright_black",
    "bright_red",
    "bright_green",
    "bright_yellow",
    "bright_blue",
    "bright_magenta",
    "bright_cyan",
    "bright_white",
]
#: Validated typed color: 16 ANSI names or #RRGGBB. The pattern rides the JSON
#: Schema so OpenAPI carries the same constraint as SDK ingress.
_STYLE_COLOR_NAMES = (
    "black|red|green|yellow|blue|magenta|cyan|white"
    "|bright_black|bright_red|bright_green|bright_yellow"
    "|bright_blue|bright_magenta|bright_cyan|bright_white"
)
StyleColor = Annotated[str, Field(pattern=rf"^(?:#(?:[0-9a-fA-F]{{6}})|{_STYLE_COLOR_NAMES})$")]


@dataclass(frozen=True, slots=True)
class ComponentStyle:
    """View-local tint: semantic tone plus optional explicit colors (frozen contract)."""

    tone: StyleTone = "default"
    foreground: StyleColor | None = None
    background: StyleColor | None = None


@dataclass(frozen=True, slots=True)
class NoticeViewData:
    message: str
    level: Literal["info", "warning", "error"] = "info"
    style: ComponentStyle | None = None

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> NoticeViewData:
        return _decode_payload(_VIEW_ADAPTERS["notice"], payload)


@dataclass(frozen=True, slots=True)
class ViewAction:
    """An explicit command action from a live, revision-bound presentation."""

    label: str
    action: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    prepare_input: bool = False

    def __post_init__(self) -> None:
        if not self.label.strip() or not self.action.strip():
            raise ValueError("view action label and command must be non-empty")


@dataclass(frozen=True, slots=True)
class MarkdownViewData:
    content: str
    style: ComponentStyle | None = None
    actions: tuple[ViewAction, ...] = ()

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
    style: ComponentStyle | None = None

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
    style: ComponentStyle | None = None
    #: True routes a selection to command.input (read-only); False invokes the action.
    prepare_input: bool = False

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> ChoicesViewData:
        return _decode_payload(_VIEW_ADAPTERS["choices"], payload)


@dataclass(frozen=True, slots=True)
class ConfirmationViewData:
    request_id: str
    message: str
    actions: tuple[str, ...] = ("allow", "deny")
    style: ComponentStyle | None = None

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
    #: None permits free text; an explicit option list restricts text to those ids.
    options: tuple[ChoiceOption, ...] | None = None

    def __post_init__(self) -> None:
        if self.options is not None:
            object.__setattr__(self, "options", tuple(self.options))
            ids = [option.id for option in self.options]
            if self.field_type != "text" or any(not identifier for identifier in ids) or len(set(ids)) != len(ids):
                raise ValueError("form options require a text field with unique non-empty ids")


@dataclass(frozen=True, slots=True)
class FormViewData:
    fields: tuple[FormField, ...]
    action: str
    initial_values: Mapping[str, object] = field(default_factory=dict)
    load_action: str | None = None
    style: ComponentStyle | None = None
    #: Fixed action arguments (e.g. a state revision), never editable form fields.
    arguments: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        names = [item.name for item in self.fields]
        if len(names) != len(set(names)) or any(name in self.arguments for name in names):
            raise ValueError("form fields must be unique and cannot shadow fixed arguments")

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> FormViewData:
        return _decode_payload(_VIEW_ADAPTERS["form"], payload)


@dataclass(frozen=True, slots=True)
class DiffViewData:
    before: str
    after: str
    path: str | None = None
    style: ComponentStyle | None = None

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> DiffViewData:
        return _decode_payload(_VIEW_ADAPTERS["diff"], payload)


@dataclass(frozen=True, slots=True)
class StatusViewData:
    label: str
    value: str
    style: ComponentStyle | None = None

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> StatusViewData:
        return _decode_payload(_VIEW_ADAPTERS["status"], payload)


@dataclass(frozen=True, slots=True)
class TableViewData:
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    total_rows: int | None = None
    reference: ArtifactReference | None = None
    style: ComponentStyle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.columns, tuple):
            try:
                object.__setattr__(self, "columns", tuple(self.columns))
            except TypeError:
                raise ValueError("table columns must be a sequence of strings") from None
        if not isinstance(self.rows, tuple):
            try:
                object.__setattr__(self, "rows", tuple(self.rows))
            except TypeError:
                raise ValueError("table rows must be a sequence of rows") from None
        normalized: list[tuple[str, ...]] = []
        for row in self.rows:
            if isinstance(row, tuple):
                normalized.append(row)
            elif isinstance(row, list):
                normalized.append(tuple(row))
            else:
                raise ValueError("table row must be a sequence of strings")
        object.__setattr__(self, "rows", tuple(normalized))
        if not self.columns:
            raise ValueError("table requires at least one column")
        for column in self.columns:
            if not isinstance(column, str):
                raise ValueError("table column must be a string")
        for row in self.rows:
            if len(row) != len(self.columns):
                raise ValueError("table row length must match columns")
            for cell in row:
                if not isinstance(cell, str):
                    raise ValueError("table cell must be a string")
        if self.total_rows is not None:
            if type(self.total_rows) is not int or self.total_rows < 0:
                raise ValueError("table total_rows must be a non-negative integer or null")
            if self.total_rows < len(self.rows):
                raise ValueError("table total_rows must cover inline rows")
        if self.reference is not None and not isinstance(self.reference, ArtifactReference):
            raise ValueError("table reference must be an artifact reference or null")
        if self.total_rows is not None and self.total_rows > len(self.rows) and self.reference is None:
            raise ValueError("truncated table requires a reference to complete data")

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> TableViewData:
        return _decode_payload(_VIEW_ADAPTERS["table"], payload)


@dataclass(frozen=True, slots=True)
class JsonViewData:
    preview: str
    reference: ArtifactReference | None = None
    media_type: str = "application/json"
    style: ComponentStyle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.preview, str) or not self.preview:
            raise ValueError("json preview must be a non-empty string")
        if self.reference is not None and not isinstance(self.reference, ArtifactReference):
            raise ValueError("json reference must be an artifact reference or null")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("json media_type must be a non-empty string")

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> JsonViewData:
        return _decode_payload(_VIEW_ADAPTERS["json"], payload)


@dataclass(frozen=True, slots=True)
class MediaViewData:
    reference: ArtifactReference
    mime: str
    alt: str
    style: ComponentStyle | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ArtifactReference):
            raise ValueError("media view requires an artifact reference")
        if not isinstance(self.mime, str) or not self.mime:
            raise ValueError("media view mime must be a non-empty string")
        if not isinstance(self.alt, str) or not self.alt:
            raise ValueError("media view alt must be a non-empty string")

    @staticmethod
    def from_payload(payload: Mapping[str, object]) -> MediaViewData:
        return _decode_payload(_VIEW_ADAPTERS["media"], payload)


_VIEW_ADAPTERS: dict[str, TypeAdapter[Any]] = {
    "notice": TypeAdapter(NoticeViewData),
    "markdown": TypeAdapter(MarkdownViewData),
    "fields": TypeAdapter(FieldsViewData),
    "choices": TypeAdapter(ChoicesViewData),
    "confirmation": TypeAdapter(ConfirmationViewData),
    "form": TypeAdapter(FormViewData),
    "diff": TypeAdapter(DiffViewData),
    "status": TypeAdapter(StatusViewData),
    "table": TypeAdapter(TableViewData),
    "json": TypeAdapter(JsonViewData),
    "media": TypeAdapter(MediaViewData),
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
    NoticeViewData
    | MarkdownViewData
    | FieldsViewData
    | ChoicesViewData
    | ConfirmationViewData
    | FormViewData
    | DiffViewData
    | StatusViewData
    | TableViewData
    | JsonViewData
    | MediaViewData
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
    live: bool = False
    live_action: ViewAction | None = None

    def __post_init__(self) -> None:
        if not self.owner or not isinstance(self.owner, str):
            raise ValueError("owner must be a non-empty string")
        if not self.name or not isinstance(self.name, str):
            raise ValueError("name must be a non-empty string")
        if not self.title or not isinstance(self.title, str):
            raise ValueError("title must be a non-empty string")
        if type(self.live) is not bool:
            raise ValueError("live must be a boolean")
        if self.live_action is not None and not isinstance(self.live_action, ViewAction):
            raise ValueError("live_action must be a ViewAction or null")
        validated = validate_view_data(self.view_type, self.data)
        object.__setattr__(self, "data", validated)

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "owner": self.owner,
            "name": self.name,
            "title": self.title,
            "view_type": self.view_type,
            "data": json_wire_object(self.data),
        }
        if self.live:
            payload["live"] = True
        if self.live_action is not None:
            payload["live_action"] = {
                "label": self.live_action.label,
                "action": self.live_action.action,
                "arguments": json_wire_object(self.live_action.arguments),
                "prepare_input": self.live_action.prepare_input,
            }
        return payload

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
#: Shared by form submission and ask answers so both clients admit the same shapes.
INTEGER_LITERAL_PATTERN = re.compile(r"^[+-]?\d+$")
NUMBER_LITERAL_PATTERN = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


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
    arguments = json_wire_object(view.arguments)
    for form_field in view.fields:
        name = form_field.name
        label = form_field.label or name
        raw = values.get(name)

        if form_field.options is not None and raw not in (None, ""):
            if not isinstance(raw, str) or raw not in {option.id for option in form_field.options}:
                raise ValueError(f"Field '{label}' must be one of the available choices.")

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
            if not INTEGER_LITERAL_PATTERN.match(candidate):
                raise ValueError(f"Field '{label}' must be an integer.")
            arguments[name] = int(candidate)
        elif form_field.field_type == "number":
            if not NUMBER_LITERAL_PATTERN.match(candidate):
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
