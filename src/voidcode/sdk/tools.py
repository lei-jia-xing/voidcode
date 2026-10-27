"""Tool contracts shared by core, runtime, and plugins.

Self-contained: the only internal imports are sibling SDK modules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from .invocation import ToolContext
from .json_values import json_wire_object, own_json_object, own_json_value
from .limits import (
    DIAGNOSTIC_DEPTH as _MAX_DIAGNOSTIC_DEPTH,
)
from .limits import (
    DIAGNOSTIC_ITEMS as _MAX_DIAGNOSTIC_ITEMS,
)
from .limits import (
    DIAGNOSTIC_TEXT_CHARS as _MAX_DIAGNOSTIC_TEXT_CHARS,
)
from .services import ArtifactReference, PluginServices

type ToolDiagnosticsDetails = Mapping[str, object]
type ToolReplayPolicy = Literal["safe", "never"]


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


#: Appended to a timeout message when the runtime could not confirm that the
#: execution stopped, so no surface reports a plain failure.
UNCONFIRMED_STOP_CLAUSE = (
    "the runtime stopped waiting for it without confirming that the execution stopped, "
    "so it may still be running and the state of its side effects is unknown"
)


def _diagnostic_text(value: str) -> str:
    return value[:_MAX_DIAGNOSTIC_TEXT_CHARS]


def _sanitize_diagnostic_value(value: object, *, depth: int = 0) -> object:
    if depth > _MAX_DIAGNOSTIC_DEPTH:
        raise ValueError("diagnostics details exceed maximum nesting depth")
    if isinstance(value, str):
        return _diagnostic_text(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        import math

        if not math.isfinite(value):
            raise ValueError("diagnostics details must contain finite JSON numbers")
        return value
    if isinstance(value, Mapping):
        if len(value) > _MAX_DIAGNOSTIC_ITEMS:
            raise ValueError("diagnostics details contain too many entries")
        return {str(item_key): _sanitize_diagnostic_value(item_value, depth=depth + 1) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple)):
        if len(value) > _MAX_DIAGNOSTIC_ITEMS:
            raise ValueError("diagnostics details contain too many items")
        return [_sanitize_diagnostic_value(item, depth=depth + 1) for item in value]
    raise ValueError(f"diagnostics details contain non-JSON value: {type(value).__name__}")


@dataclass(frozen=True, slots=True)
class ToolDiagnostics:
    """Runtime-wide diagnostic slot shared by every tool result."""

    kind: str | None = None
    summary: str | None = None
    details: ToolDiagnosticsDetails = field(default_factory=dict)
    guidance: str | None = None

    def __post_init__(self) -> None:
        for name, value in (("kind", self.kind), ("summary", self.summary), ("guidance", self.guidance)):
            if value is not None and not isinstance(value, str):
                raise ValueError(f"diagnostics {name} must be a string or null")
            if isinstance(value, str):
                object.__setattr__(self, name, _diagnostic_text(value))
        if not isinstance(self.details, Mapping):
            raise ValueError("diagnostics details must be an object")
        sanitized = _sanitize_diagnostic_value(self.details)
        object.__setattr__(self, "details", own_json_value(sanitized))

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


@dataclass(frozen=True, slots=True)
class ResourceChange:
    """A successful tool's actual resource change, attributed by the core at commit."""

    path: str
    operation: Literal["create", "update", "delete", "move"] = "update"
    old_path: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.path, str) or not self.path:
            raise ValueError("resource change requires a non-empty path")
        if self.operation not in {"create", "update", "delete", "move"}:
            raise ValueError("invalid resource change operation")
        if self.old_path is not None and (not isinstance(self.old_path, str) or not self.old_path):
            raise ValueError("old_path must be a non-empty string or None")
        if self.operation == "move" and not self.old_path:
            raise ValueError("resource move requires old_path")


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

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_schema", own_json_object(self.input_schema))
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


type ToolOutput = TextOutput | EmptyOutput | AttachmentOutput


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
    changes: tuple[ResourceChange, ...] = ()

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
    changes: tuple[ResourceChange, ...] = ()
    status: ClassVar[Literal["error"]] = "error"


type ToolResult = ToolSuccess[Any] | ToolFailure[Any]


@runtime_checkable
class Tool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, call: ToolCall, *, context: ToolContext, services: PluginServices) -> ToolResult: ...
