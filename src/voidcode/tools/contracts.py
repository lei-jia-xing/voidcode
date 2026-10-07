from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, runtime_checkable

from ..security.json_values import json_wire_object, own_json_object, own_json_value
from ..security.redaction import (
    DIAGNOSTIC_DEPTH as _MAX_DIAGNOSTIC_DEPTH,
)
from ..security.redaction import (
    DIAGNOSTIC_ITEMS as _MAX_DIAGNOSTIC_ITEMS,
)
from ..security.redaction import (
    DIAGNOSTIC_TEXT_CHARS as _MAX_DIAGNOSTIC_TEXT_CHARS,
)
from ..security.redaction import (
    REDACTED_PLACEHOLDER as _REDACTED,
)
from ..security.redaction import (
    is_sensitive_key,
    redact_text,
)

if TYPE_CHECKING:
    from ..core.questions import PendingQuestionPrompt, QuestionResponse
    from ..core.tool_context import ToolContext


type SideEffectState = Literal["settled", "unknown"]
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
_SESSION_COMMAND_EFFECTS = frozenset({ToolEffect.SESSION, ToolEffect.SPAWN})


def has_external_mutations(effects: frozenset[ToolEffect]) -> bool:
    return not effects.isdisjoint(EXTERNAL_MUTATION_EFFECTS)


def is_read_tier(effects: frozenset[ToolEffect]) -> bool:
    """Declared external read scope, independent of execution/resource effects."""
    if not effects or ToolEffect.WRITE in effects:
        return False
    return ToolEffect.READ in effects or not has_external_mutations(effects) or ToolEffect.SESSION in effects and effects <= _SESSION_COMMAND_EFFECTS


#: Side-effect state the runtime may claim for a timed-out execution. ``settled``
#: means the runtime confirmed the execution stopped; it never means the side
#: effects already performed were rolled back. ``unknown`` means the execution
#: may still be in flight.
type ToolSideEffectState = Literal["settled", "unknown"]
#: Appended to a timeout message when the runtime could not confirm that the
#: execution stopped, so no surface reports a plain failure.
UNCONFIRMED_STOP_CLAUSE = (
    "the runtime stopped waiting for it without confirming that the execution stopped, "
    "so it may still be running and the state of its side effects is unknown"
)


def _redact_diagnostic_text(value: str) -> str:
    return redact_text(value[:_MAX_DIAGNOSTIC_TEXT_CHARS])


def _sanitize_diagnostic_value(value: object, *, depth: int = 0, key: str | None = None) -> object:
    if depth > _MAX_DIAGNOSTIC_DEPTH:
        raise ValueError("diagnostics details exceed maximum nesting depth")
    if isinstance(value, str):
        return _REDACTED if key is not None and is_sensitive_key(key) else _redact_diagnostic_text(value)
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
        return {
            str(item_key): _sanitize_diagnostic_value(item_value, depth=depth + 1, key=str(item_key).lower())
            for item_key, item_value in value.items()
        }
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
                object.__setattr__(self, name, _redact_diagnostic_text(value))
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


class RuntimeToolTimeoutError(TimeoutError):
    """Raised when the runtime-owned outer tool timeout wins.

    A timeout only says the runtime stopped waiting for the call; it never
    promises that the call stopped. The execution facts below carry what the
    runtime could actually verify, so every surface that reports the timeout
    (tool result, event payload, session state) can state the side-effect state
    instead of implying that nothing further will happen.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_result: object | None = None,
        cancellation_signalled: bool = False,
        execution_stopped: bool = True,
    ) -> None:
        super().__init__(message)
        self.partial_result = partial_result
        #: The runtime cancelled the invocation before it stopped waiting.
        self.cancellation_signalled = cancellation_signalled
        #: The runtime confirmed the execution stopped before it returned. The
        #: executor finalizes this once it has reaped (or failed to reap) the
        #: worker; tools that time themselves out are stopped by definition.
        self.execution_stopped = execution_stopped

    @property
    def side_effect_state(self) -> ToolSideEffectState:
        """``settled`` only when the runtime confirmed the execution stopped."""
        return "settled" if self.execution_stopped else "unknown"

    @property
    def error_message(self) -> str:
        """Runtime-facing message that states the verified side-effect state."""
        if self.execution_stopped:
            return str(self)
        return f"{self}; {UNCONFIRMED_STOP_CLAUSE}"

    def execution_facts(self) -> dict[str, object]:
        """Additive execution facts shared by every timeout surface."""
        return {
            "cancellation_signalled": self.cancellation_signalled,
            "execution_stopped": self.execution_stopped,
            "side_effect_state": self.side_effect_state,
        }


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

    def effective_replay_policy_for(self, arguments: Mapping[str, object] | None = None) -> ToolReplayPolicy:
        if self.name == "ast_grep" and arguments is not None and arguments.get("mode") in {"search", "preview"}:
            return "safe"
        return self.replay_policy or ("safe" if is_read_tier(self.effects) else "never")

    @property
    def effective_replay_policy(self) -> ToolReplayPolicy:
        return self.effective_replay_policy_for()


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
class ToolInvocation:
    """Runtime-owned boundary for one resolved tool call.

    Governance and execution metadata deliberately remain outside ``ToolCall``
    and ``ToolResult``. The runtime resolves the definition and constructs the
    invocation context before handing this immutable bundle to the executor.
    """

    tool_call: ToolCall
    tool_definition: ToolDefinition
    context: ToolContext

    def __post_init__(self) -> None:
        if self.tool_call.tool_name != self.tool_definition.name:
            raise ValueError("tool invocation call and definition must refer to the same tool")


@dataclass(frozen=True, slots=True)
class OutputReference:
    uri: str
    artifact: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.artifact is not None:
            object.__setattr__(self, "artifact", own_json_object(self.artifact))


@dataclass(frozen=True, slots=True)
class OutputBounds:
    truncated: bool = False
    partial: bool = False
    reference: OutputReference | None = None
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
    data_uri: str
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
class QuestionPrepared:
    prompts: tuple[PendingQuestionPrompt, ...]


@dataclass(frozen=True, slots=True)
class QuestionAnswered:
    responses: tuple[QuestionResponse, ...]


@dataclass(frozen=True, slots=True)
class ProgressYield:
    types: tuple[str, ...]
    result: str | None
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", own_json_object(self.data))

    def as_payload(self) -> dict[str, object]:
        return {"type": list(self.types), "result": self.result, "data": json_wire_object(self.data)}


@dataclass(frozen=True, slots=True)
class TerminalYield:
    summary: str
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", own_json_object(self.data))

    def as_payload(self) -> dict[str, object]:
        return {"summary": self.summary, "data": json_wire_object(self.data)}


@dataclass(frozen=True, slots=True)
class TerminalYieldFailure:
    data: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", own_json_object(self.data))


type SuccessControl = QuestionPrepared | QuestionAnswered | ProgressYield | TerminalYield
type ToolControl = SuccessControl | TerminalYieldFailure


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


@dataclass(frozen=True, slots=True)
class ToolSuccess[Body: ToolBody]:
    tool_name: str
    output: ToolOutput = EmptyOutput()
    body: Body | None = None
    control: SuccessControl | None = None
    status: ClassVar[Literal["ok"]] = "ok"


@dataclass(frozen=True, slots=True)
class ToolFailure[Body: ToolBody]:
    tool_name: str
    error: str
    output: ToolOutput = EmptyOutput()
    body: Body | None = None
    control: TerminalYieldFailure | None = None
    diagnostics: ToolDiagnostics | None = None
    execution: ExecutionObservation | None = None
    timeout_seconds: int | None = None
    status: ClassVar[Literal["error"]] = "error"


type ToolResult = ToolSuccess[Any] | ToolFailure[Any]


@runtime_checkable
class StaticTool(Protocol):
    definition: ClassVar[ToolDefinition]

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult: ...


@runtime_checkable
class DynamicTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult: ...


@runtime_checkable
class RuntimeTimeoutAwareTool(Protocol):
    def invoke_with_runtime_timeout(
        self,
        call: ToolCall,
        *,
        context: ToolContext,
        timeout_seconds: int,
    ) -> ToolResult: ...


type Tool = StaticTool | DynamicTool
