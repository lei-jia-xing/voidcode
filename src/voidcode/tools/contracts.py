from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, Literal, Protocol, cast, runtime_checkable

if TYPE_CHECKING:
    from .runtime_context import RuntimeToolInvocationContext

type ToolResultStatus = Literal["ok", "error"]
type ToolDiagnosticsDetails = dict[str, object]
type ToolReplayPolicy = Literal["safe", "never"]

_MAX_DIAGNOSTIC_TEXT_CHARS = 4000
_MAX_DIAGNOSTIC_DEPTH = 8
_MAX_DIAGNOSTIC_ITEMS = 256
_SENSITIVE_DIAGNOSTIC_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "secret",
        "token",
    }
)


def _redact_diagnostic_text(value: str) -> str:
    """Bound and redact common credential forms before diagnostics persist."""
    import re

    bounded = value[:_MAX_DIAGNOSTIC_TEXT_CHARS]
    bounded = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", bounded)
    bounded = re.sub(r"(?i)(api[_-]?key|access[_-]?token|password|secret|token)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", bounded)
    bounded = re.sub(r"\bsk-[A-Za-z0-9_-]+", "sk-[REDACTED]", bounded)
    return bounded


def _sanitize_diagnostic_value(value: object, *, depth: int = 0, key: str | None = None) -> object:
    if depth > _MAX_DIAGNOSTIC_DEPTH:
        raise ValueError("diagnostics details exceed maximum nesting depth")
    if isinstance(value, str):
        return "[REDACTED]" if key in _SENSITIVE_DIAGNOSTIC_KEYS else _redact_diagnostic_text(value)
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
        for name in ("kind", "summary", "guidance"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"diagnostics {name} must be a string or null")
            if isinstance(value, str):
                object.__setattr__(self, name, _redact_diagnostic_text(value))
        if not isinstance(self.details, dict):
            raise ValueError("diagnostics details must be an object")
        sanitized = _sanitize_diagnostic_value(self.details)
        assert isinstance(sanitized, dict)
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
        return cls(details=cast(dict[str, object], details), **values)


class RuntimeToolTimeoutError(TimeoutError):
    """Raised when the runtime-owned outer tool timeout wins."""

    def __init__(self, message: str, *, partial_result: object | None = None) -> None:
        super().__init__(message)
        self.partial_result = partial_result


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
        if self.status not in ("ok", "error"):
            raise ValueError("tool result status must be 'ok' or 'error'")
        if not isinstance(self.tool_name, str) or not self.tool_name:
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
