from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, runtime_checkable

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
    from .runtime_context import RuntimeToolInvocationContext

type ToolResultStatus = Literal["ok", "error"]
type ToolDiagnosticsDetails = dict[str, object]
type ToolReplayPolicy = Literal["safe", "never"]
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
    if isinstance(value, dict):
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
        if not isinstance(self.details, dict):
            raise ValueError("diagnostics details must be an object")
        sanitized = _sanitize_diagnostic_value(self.details)
        object.__setattr__(self, "details", sanitized)

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {}
        if self.kind is not None:
            payload["kind"] = self.kind
        if self.summary is not None:
            payload["summary"] = self.summary
        if self.details:
            payload["details"] = dict(self.details)
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
    input_schema: dict[str, object] = field(default_factory=dict)
    read_only: bool = True
    path_argument_keys: tuple[str, ...] = ()
    # Safe read/query tools may be replayed after a process crash. Mutating
    # tools default to never replay unless they explicitly opt in.
    replay_policy: ToolReplayPolicy | None = None

    def effective_replay_policy_for(self, arguments: Mapping[str, object] | None = None) -> ToolReplayPolicy:
        if self.name == "ast_grep" and arguments is not None and arguments.get("mode") in {"search", "preview"}:
            return "safe"
        return self.replay_policy or ("safe" if self.read_only else "never")

    @property
    def effective_replay_policy(self) -> ToolReplayPolicy:
        return self.effective_replay_policy_for()


@dataclass(frozen=True, slots=True)
class ToolCall:
    tool_name: str
    arguments: dict[str, object] = field(default_factory=dict)
    tool_call_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    """Runtime-owned boundary for one resolved tool call.

    Governance and execution metadata deliberately remain outside ``ToolCall``
    and ``ToolResult``. The runtime resolves the definition and constructs the
    invocation context before handing this immutable bundle to the executor.
    """

    tool_call: ToolCall
    tool_definition: ToolDefinition
    context: RuntimeToolInvocationContext

    def __post_init__(self) -> None:
        if self.tool_call.tool_name != self.tool_definition.name:
            raise ValueError("tool invocation call and definition must refer to the same tool")


@dataclass(frozen=True, slots=True)
class ToolResult:
    tool_name: str
    status: ToolResultStatus
    content: str | None = None
    data: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    diagnostics: ToolDiagnostics | None = None
    truncated: bool = False
    partial: bool = False
    timeout_seconds: int | None = None
    source: str | None = None
    fallback_reason: str | None = None
    reference: str | None = None

    def __post_init__(self) -> None:
        if not self.tool_name:
            raise ValueError("tool results must include a tool name")
        if not isinstance(self.data, dict):
            raise ValueError("tool result data must be an object")
        if self.status == "error" and not isinstance(self.error, str):
            raise ValueError("error results must include an error message")
        if self.status == "ok" and self.error is not None:
            raise ValueError("successful results cannot include an error message")
        if self.status == "ok" and self.diagnostics is not None:
            raise ValueError("successful results cannot include diagnostics")
        if isinstance(self.diagnostics, dict):
            object.__setattr__(self, "diagnostics", ToolDiagnostics.from_payload(self.diagnostics))
        if self.diagnostics is not None and not isinstance(self.diagnostics, ToolDiagnostics):
            raise ValueError("tool result diagnostics must be ToolDiagnostics or null")
        if not isinstance(self.truncated, bool) or not isinstance(self.partial, bool):
            raise ValueError("tool result truncated and partial must be booleans")
        if self.timeout_seconds is not None and (
            not isinstance(self.timeout_seconds, int) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds < 0
        ):
            raise ValueError("tool result timeout_seconds must be a non-negative integer or null")


@runtime_checkable
class StaticTool(Protocol):
    definition: ClassVar[ToolDefinition]

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult: ...


@runtime_checkable
class DynamicTool(Protocol):
    @property
    def definition(self) -> ToolDefinition: ...

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult: ...


@runtime_checkable
class RuntimeTimeoutAwareTool(Protocol):
    def invoke_with_runtime_timeout(
        self,
        call: ToolCall,
        *,
        workspace: Path,
        timeout_seconds: int,
    ) -> ToolResult: ...


type Tool = StaticTool | DynamicTool
