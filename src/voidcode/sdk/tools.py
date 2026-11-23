"""Tool contracts shared by core, runtime, and plugins.

Self-contained: the only internal imports are sibling SDK modules.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, ClassVar, Final, Literal, Protocol, TypeIs, get_args, runtime_checkable

from .icons import Icon
from .invocation import ToolContext
from .json_values import json_wire_object, own_json_object
from .services import ArtifactReference, PluginServices

DIAGNOSTIC_DEPTH: Final[int] = 8
DIAGNOSTIC_ITEMS: Final[int] = 256
TOOL_OUTPUT_BYTES: Final[int] = 50 * 1024
#: Raster image MIME types the model attachment path renders as media. Other
#: image formats (notably SVG) stay readable text presentations instead.
RASTER_IMAGE_MIMES: Final[frozenset[str]] = frozenset({"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"})
_MAX_DIAGNOSTIC_DEPTH = DIAGNOSTIC_DEPTH
_MAX_DIAGNOSTIC_ITEMS = DIAGNOSTIC_ITEMS

type ToolDiagnosticsDetails = Mapping[str, object]
#: Diagnostic kinds every bundled tool may produce. The wire-facing ``ToolDiagnostics.kind``
#: stays open because external/historical results can carry plugin-defined tokens.
type ToolDiagnosticKind = Literal[
    "ambiguous_match",
    "no_op",
    "stale_edit",
    "stale_read",
    "tool_cancelled",
    "tool_environment_error",
    "tool_execution_error",
    "tool_input_mismatch",
    "tool_input_validation",
]
type ToolReplayPolicy = Literal["safe", "never"]

_TOOL_DIAGNOSTIC_KINDS: Final[frozenset[str]] = frozenset(value for value in get_args(ToolDiagnosticKind.__value__) if isinstance(value, str))


def is_tool_diagnostic_kind(value: object) -> TypeIs[ToolDiagnosticKind]:
    """Whether a diagnostic kind token names one bundled kind."""
    return isinstance(value, str) and value in _TOOL_DIAGNOSTIC_KINDS


class ToolEffect(StrEnum):
    READ = "read"
    WRITE = "write"
    EXECUTE = "execute"
    NETWORK = "network"
    SPAWN = "spawn"
    SESSION = "session"


EXTERNAL_MUTATION_EFFECTS = frozenset({ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.SPAWN})
#: Effects a restrictive (read-only) stance refuses. SPAWN is not here: delegation creates no
#: privilege by itself because a restricted owner may only spawn a child that inherits the
#: restriction (the core enforces the child's stance at the spawn service).
RESTRICTED_DENIAL_EFFECTS = frozenset({ToolEffect.WRITE, ToolEffect.EXECUTE})
_SESSION_COMMAND_EFFECTS = frozenset({ToolEffect.SESSION, ToolEffect.SPAWN})


def has_external_mutations(effects: frozenset[ToolEffect]) -> bool:
    return not effects.isdisjoint(EXTERNAL_MUTATION_EFFECTS)


def denied_under_restriction(effects: frozenset[ToolEffect]) -> bool:
    """Whether a read-only stance refuses this declared effect set."""
    return not effects.isdisjoint(RESTRICTED_DENIAL_EFFECTS)


def is_read_tier(effects: frozenset[ToolEffect]) -> bool:
    """Declared external read scope, independent of execution/resource effects."""
    if not effects or ToolEffect.WRITE in effects or ToolEffect.SESSION in effects:
        return False
    return ToolEffect.READ in effects or not has_external_mutations(effects)


def _freeze_diagnostic_value(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_DIAGNOSTIC_DEPTH:
        raise ValueError("diagnostics details exceed maximum nesting depth")
    if isinstance(value, str):
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("diagnostics details must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_DIAGNOSTIC_ITEMS:
            raise ValueError("diagnostics details contain too many entries")
        return MappingProxyType({str(item_key): _freeze_diagnostic_value(item_value, depth=depth + 1) for item_key, item_value in value.items()})
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_DIAGNOSTIC_ITEMS:
            raise ValueError("diagnostics details contain too many items")
        return tuple(_freeze_diagnostic_value(item, depth=depth + 1) for item in value)
    raise ValueError(f"diagnostics details contain non-JSON value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ToolDiagnostics:
    """Runtime-wide diagnostic slot shared by every tool result."""

    # Open on the wire; bundled producers use :data:`ToolDiagnosticKind`.
    kind: str | None = None
    summary: str | None = None
    details: ToolDiagnosticsDetails = field(default_factory=dict)
    guidance: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("kind", self.kind), ("summary", self.summary), ("guidance", self.guidance)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"diagnostics {name} must be a string or null")
        if not isinstance(self.details, Mapping):
            raise ValueError("diagnostics details must be an object")
        sanitized = _freeze_diagnostic_value(self.details)
        object.__setattr__(self, "details", sanitized)

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.kind is not None:
            payload["kind"] = self.kind
        if self.summary is not None:
            payload["summary"] = self.summary
        if self.details:
            payload["details"] = json_wire_object(self.details)
        if self.guidance is not None:
            payload["guidance"] = self.guidance
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> ToolDiagnostics:
        if not isinstance(payload, dict):
            raise ValueError("diagnostics must be an object")
        allowed = {"kind", "summary", "details", "guidance"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError("diagnostics contains unknown field(s): " + ", ".join(unknown))
        details = payload.get("details", {})
        if not isinstance(details, dict):
            raise ValueError("diagnostics details must be an object")
        values = {name: payload.get(name) for name in ("kind", "summary", "guidance")}
        return cls(details=details, **values)


def required_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"field {field!r} must be a non-empty string")
    return value


def optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"field {field!r} must be a non-empty string or null")
    return value


@dataclass(frozen=True, slots=True)
class Change:
    """One filesystem change: observed by a helper or attributed to a tool result."""

    path: str
    operation: Literal["create", "update", "delete", "move"] = "update"
    old_path: str | None = None
    content_hash: str | None = None
    #: The write landed in sandbox scratch (tmpfs), not on host paths: surface in
    #: output text/body only, never as a committed result change.
    scratch: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("change requires a non-empty path")
        if self.operation not in {"create", "update", "delete", "move"}:
            raise ValueError("invalid change operation")
        if self.old_path is not None and (not isinstance(self.old_path, str) or not self.old_path):
            raise ValueError("old_path must be a non-empty string or None")
        if self.operation == "move" and not self.old_path:
            raise ValueError("change move requires old_path")
        if self.content_hash is not None and (not isinstance(self.content_hash, str) or not self.content_hash):
            raise ValueError("content_hash must be a non-empty string or None")
        if type(self.scratch) is not bool:
            raise ValueError("scratch must be a boolean")

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"path": self.path, "operation": self.operation}
        if self.old_path is not None:
            payload["old_path"] = self.old_path
        if self.content_hash is not None:
            payload["content_hash"] = self.content_hash
        if self.scratch:
            payload["scratch"] = True
        return payload

    @classmethod
    def from_payload(cls, payload: object) -> Change:
        if not isinstance(payload, dict):
            raise ValueError("change must be an object")
        unknown = sorted(set(payload) - {"path", "operation", "old_path", "content_hash", "scratch"})
        if unknown:
            raise ValueError("change contains unknown field(s): " + ", ".join(unknown))
        return cls(
            path=required_str(payload.get("path"), "path"),
            operation=payload.get("operation", "update"),  # type: ignore[arg-type]
            old_path=optional_str(payload.get("old_path"), "old_path"),
            content_hash=optional_str(payload.get("content_hash"), "content_hash"),
            scratch=bool(payload.get("scratch", False)),
        )


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    input_schema: Mapping[str, object] = field(default_factory=dict)
    effects: frozenset[ToolEffect] = frozenset({ToolEffect.READ})
    path_argument_keys: tuple[str, ...] = ()
    # Safe read/query tools may be replayed after a process crash. Mutating
    # tools default to never replay unless they explicitly opt in.
    replay_policy: ToolReplayPolicy | None = None
    icon: Icon | None = None
    #: Declared JSON Schema (Draft 2020-12) for this tool's success body payload
    #: (``body.as_payload()`` on ``ToolSuccess``). ``None`` means the producer
    #: declares no contract — unknown, never "any shape is valid". Failure bodies
    #: are diagnostics, not data, and are never validated against this schema.
    output_schema: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", own_json_object(self.input_schema))
        if self.output_schema is not None:
            object.__setattr__(self, "output_schema", own_json_object(self.output_schema))
        if not isinstance(self.effects, frozenset):
            object.__setattr__(self, "effects", frozenset(ToolEffect(e) if isinstance(e, str) else e for e in self.effects))
        if not isinstance(self.path_argument_keys, tuple):
            object.__setattr__(self, "path_argument_keys", tuple(self.path_argument_keys))
        if self.replay_policy == "safe" and any(
            e in (ToolEffect.WRITE, ToolEffect.EXECUTE, ToolEffect.SPAWN, ToolEffect.SESSION) for e in self.effects
        ):
            raise ValueError("mutating tools cannot declare safe replay")

    @property
    def effective_replay_policy(self) -> ToolReplayPolicy:
        if self.replay_policy is not None:
            return self.replay_policy
        if self.effects == frozenset({ToolEffect.READ}):
            return "safe"
        return "never"


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_name: str
    arguments: Mapping[str, object] = field(default_factory=dict)
    tool_call_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "arguments", own_json_object(self.arguments))

    def __deepcopy__(self, memo: dict[int, object]) -> ToolCall:
        return self


@dataclass(frozen=True, slots=True)
class OutputBounds:
    """Resource bounds and recovery identity; ``reference`` is not a page cursor.

    ``TextOutput.text`` is the selected content already chosen by its producer, so hosts must
    preserve it rather than re-reading ``reference`` from byte zero. A typed textual
    ``ArtifactReference`` on ``EmptyOutput`` instead identifies content intentionally kept out
    of line; a host may page that resource into its model projection while retaining
    ``presentation`` as its summary.
    """

    truncated: bool = False
    partial: bool = False
    reference: ArtifactReference | str | None = None
    source: str | None = None
    fallback_reason: str | None = None


@dataclass(frozen=True, slots=True)
class TextOutput:
    text: str
    presentation: str | None = None
    bounds: OutputBounds = OutputBounds()


@dataclass(frozen=True, slots=True)
class EmptyOutput:
    presentation: str | None = None
    bounds: OutputBounds = OutputBounds()


@dataclass(frozen=True, slots=True)
class AttachmentOutput:
    mime: str
    data_uri: str | None
    presentation: str | None = None
    bounds: OutputBounds = OutputBounds()


@dataclass(frozen=True, slots=True)
class TextPart:
    """One ordered inline text segment; ``reference`` is spilled full text when compacted."""

    text: str
    reference: ArtifactReference | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str):
            raise ValueError("text part text must be a string")
        if self.reference is not None and not isinstance(self.reference, ArtifactReference):
            raise ValueError("text part reference must be an artifact reference or null")


@dataclass(frozen=True, slots=True)
class MediaPart:
    """One ordered media segment; ``presentation`` is the required readable fallback."""

    reference: ArtifactReference
    mime: str
    presentation: str

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ArtifactReference):
            raise ValueError("media part requires an artifact reference")
        if not isinstance(self.mime, str) or not self.mime:
            raise ValueError("media part mime must be a non-empty string")
        if not isinstance(self.presentation, str) or not self.presentation:
            raise ValueError("media part presentation must be a non-empty string")


@dataclass(frozen=True, slots=True)
class TablePart:
    """One ordered string-matrix segment; truncation requires a complete-data reference."""

    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...] = ()
    total_rows: int | None = None
    reference: ArtifactReference | None = None
    presentation: str | None = None

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
        if self.presentation is not None and not isinstance(self.presentation, str):
            raise ValueError("table presentation must be a string or null")


@dataclass(frozen=True, slots=True)
class JsonPart:
    """One ordered JSON segment; complete JSON lives at ``reference``, ``preview`` is inline."""

    reference: ArtifactReference
    preview: str
    media_type: str = "application/json"

    def __post_init__(self) -> None:
        if not isinstance(self.reference, ArtifactReference):
            raise ValueError("json part requires an artifact reference")
        if not isinstance(self.preview, str) or not self.preview:
            raise ValueError("json preview must be a non-empty string")
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("json media_type must be a non-empty string")


type ContentPart = TextPart | MediaPart | TablePart | JsonPart


@dataclass(frozen=True, slots=True)
class CompositeOutput:
    """Ordered mixed content; ``presentation`` is the overall readable summary."""

    parts: tuple[ContentPart, ...]
    presentation: str | None = None
    bounds: OutputBounds = OutputBounds()

    def __post_init__(self) -> None:
        if not isinstance(self.parts, tuple):
            try:
                object.__setattr__(self, "parts", tuple(self.parts))
            except TypeError:
                raise ValueError("composite parts must be a sequence of parts") from None
        if not self.parts:
            raise ValueError("composite output requires at least one part")
        for part in self.parts:
            if not isinstance(part, (TextPart, MediaPart, TablePart, JsonPart)):
                raise ValueError(f"composite part has invalid type: {type(part).__name__}")
        if self.presentation is not None and not isinstance(self.presentation, str):
            raise ValueError("composite presentation must be a string or null")
        if not isinstance(self.bounds, OutputBounds):
            raise ValueError("composite bounds must be OutputBounds")


type ToolOutput = TextOutput | EmptyOutput | AttachmentOutput | CompositeOutput


def content_part_text(part: ContentPart) -> str:
    """One part's ordered readable text; references stay as URIs, never inlined bytes."""
    if isinstance(part, TextPart):
        if part.reference is not None:
            suffix = f"[full text: {part.reference.uri}]"
            return f"{part.text}\n{suffix}" if part.text else suffix
        return part.text
    if isinstance(part, MediaPart):
        return f"{part.presentation} [{part.mime}, {part.reference.bytes} bytes, {part.reference.uri}]"
    if isinstance(part, TablePart):
        lines = ["\t".join(part.columns)]
        lines.extend("\t".join(row) for row in part.rows)
        table_text = "\n".join(lines)
        if part.presentation:
            table_text = f"{part.presentation}\n{table_text}"
        if part.total_rows is not None and part.total_rows > len(part.rows) and part.reference is not None:
            table_text += f"\n[showing {len(part.rows)}/{part.total_rows} rows; full table: {part.reference.uri}]"
        return table_text
    return f"{part.preview}\n[full JSON: {part.reference.uri}]"


def composite_ordered_text(output: CompositeOutput) -> str:
    """All parts in order, joined for model projection, history, and compaction."""
    rendered = [content_part_text(part) for part in output.parts]
    return "\n\n".join(text for text in rendered if text)


class ToolBody(Protocol):
    """A tool owner's typed payload; not a source of control or call authority."""

    def as_payload(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class OpaqueToolBody:
    """JSON returned by an externally shaped MCP or installed-tool endpoint."""

    structured_content: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "structured_content", own_json_object(self.structured_content))

    def as_payload(self) -> dict[str, object]:
        return json_wire_object(self.structured_content)

    def __deepcopy__(self, memo: dict[int, object]) -> OpaqueToolBody:
        return self


@dataclass(frozen=True, slots=True)
class ConfirmedStop:
    cancellation_signalled: bool
    side_effect_state: ClassVar[Literal["settled"]] = "settled"
    execution_stopped: ClassVar[Literal[True]] = True


@dataclass(frozen=True, slots=True)
class UnconfirmedStop:
    cancellation_signalled: bool
    side_effect_state: ClassVar[Literal["unknown"]] = "unknown"
    execution_stopped: ClassVar[Literal[False]] = False


type ExecutionObservation = ConfirmedStop | UnconfirmedStop

_SHA256_TEXT = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReadObservation:
    """One file version a bounded tool result made visible to the model.

    A read-shaped tool declares these on its own result, so the runtime can tell a later call
    (a write, an edit, a structural rewrite) which exact bytes this session's model has already
    seen — without the runtime knowing any product's read semantics. An observation is a claim
    about *this* result: it grants coverage only while it is not clipped, and the runtime never
    merges the lines of two different hashes of the same path.
    """

    path: str
    content_hash: str
    lines: tuple[int, ...] = ()
    line_count: int = 0
    #: The result showed the whole file version, not only a window of it.
    whole_file: bool = False
    #: Something in this observation was cut (a truncated line, a clipped read): it grants
    #: nothing at all, rather than granting a partial truth a guard could rely on.
    clipped: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("read observation path must be a non-empty string")
        if not isinstance(self.content_hash, str) or _SHA256_TEXT.match(self.content_hash) is None:
            raise ValueError("read observation content_hash must be a lowercase sha256 digest")
        if not isinstance(self.lines, tuple):
            object.__setattr__(self, "lines", tuple(self.lines))
        seen: set[int] = set()
        for line in self.lines:
            if type(line) is not int or line < 1:
                raise ValueError("read observation lines must be positive integers")
            seen.add(line)
        object.__setattr__(self, "lines", tuple(sorted(seen)))
        if type(self.line_count) is not int or self.line_count < 0:
            raise ValueError("read observation line_count must be a non-negative integer")
        if type(self.whole_file) is not bool:
            raise ValueError("read observation whole_file must be a boolean")
        if type(self.clipped) is not bool:
            raise ValueError("read observation clipped must be a boolean")
        if any(line > self.line_count for line in self.lines):
            raise ValueError("read observation lines must lie within the observed line count")

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.path,
            "content_hash": self.content_hash,
            "lines": list(self.lines),
            "line_count": self.line_count,
            "whole_file": self.whole_file,
            "clipped": self.clipped,
        }


@dataclass(frozen=True, slots=True)
class ToolSuccess[Body: ToolBody]:
    tool_name: str
    output: ToolOutput = EmptyOutput()
    body: Body | None = None
    #: File versions this success made visible; empty for a result that observed no file.
    coverage: tuple[ReadObservation, ...] = ()
    changes: tuple[Change, ...] = ()
    steering_context: str | None = None
    result_reference: ArtifactReference | None = None
    status: ClassVar[Literal["ok"]] = "ok"

    def __post_init__(self) -> None:
        if not isinstance(self.coverage, tuple):
            object.__setattr__(self, "coverage", tuple(self.coverage))


@dataclass(frozen=True, slots=True)
class ToolFailure[Body: ToolBody]:
    tool_name: str
    error: str
    output: ToolOutput = EmptyOutput()
    body: Body | None = None
    diagnostics: ToolDiagnostics | None = None
    execution: ExecutionObservation | None = None
    timeout_seconds: int | None = None
    #: Files committed before this invocation failed or was cancelled.
    changes: tuple[Change, ...] = ()
    steering_context: str | None = None
    result_reference: ArtifactReference | None = None
    status: ClassVar[Literal["error"]] = "error"


type ToolResult = ToolSuccess[Any] | ToolFailure[Any]


@runtime_checkable
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult: ...
