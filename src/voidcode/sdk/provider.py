"""Wire-agnostic provider contracts shared by kernel, host, and plugins.

Providers consume neutral messages and tool definitions only. The kernel owns
context assembly and catalog metadata; opaque provider metadata travels as a
plain mapping, never as a catalog type.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .json_values import own_json_object
from .tools import ToolCall, ToolDefinition

type ProviderStreamEventKind = Literal["delta", "content", "tool_call_start", "tool_call_delta", "tool_call_end", "error", "done"]
type ProviderStreamChannel = Literal["text", "tool", "reasoning", "error"]
type ProviderCacheRetention = Literal["none", "short", "long"]
# Provider-native finish reasons are preserved at the adapter boundary.
# ``unknown`` means the upstream omitted or used an unrecognized reason.
type ProviderDoneReason = Literal["stop", "tool_calls", "length", "content_filter", "function_call", "cancelled", "error", "unknown", "completed"]
type ProviderErrorKind = Literal[
    "missing_auth",
    "invalid_model",
    "not_configured",
    "rate_limit",
    "transient_failure",
    "context_limit",
    "provider_error",
    "unsupported_feature",
    "stream_tool_feedback_shape",
    "cancelled",
]


@dataclass(frozen=True, slots=True)
class ProviderMessage:
    """One neutral conversation message handed to a provider adapter."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    is_error: bool = False
    reasoning: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.tool_calls, tuple):
            object.__setattr__(self, "tool_calls", tuple(self.tool_calls))
        if not isinstance(self.metadata, Mapping):
            raise ValueError("message metadata must be a mapping")
        object.__setattr__(self, "metadata", own_json_object(self.metadata))


@dataclass(frozen=True, slots=True)
class ProviderTurnRequest:
    messages: tuple[ProviderMessage, ...]
    available_tools: tuple[ToolDefinition, ...] = ()
    raw_model: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    reasoning_effort: str | None = None
    cache_retention: ProviderCacheRetention | None = None
    attempt: int = 0
    abort_signal: ProviderAbortSignal | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", own_json_object(self.metadata))


@dataclass(frozen=True, slots=True)
class ProviderTokenUsage:
    # ``None`` means the provider did not report that metric; zero is an
    # observed zero. ``input_tokens`` is the *inclusive* prompt total: cache
    # reads are already inside it. Never add ``cache_read_tokens`` on top.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    uncached_input_tokens: int | None = None
    #: USD computed once from catalog rates at report time (never repriced).
    cost_usd: float | None = None

    def metadata_payload(self) -> dict[str, int | float | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cost_usd": self.cost_usd,
        }

    @property
    def cache_hit_rate(self) -> float | None:
        if self.cache_read_tokens is None or self.uncached_input_tokens is None:
            return None
        denominator = self.cache_read_tokens + self.uncached_input_tokens
        if denominator <= 0:
            return None
        return self.cache_read_tokens / denominator


@dataclass(frozen=True, slots=True)
class ProviderTurnResult:
    tool_calls: tuple[ToolCall, ...] = ()
    output: str | None = None
    usage: ProviderTokenUsage | None = None
    # Non-streaming reasoning content so non-streaming turns persist the same
    # reasoning part the streaming path aggregates.
    reasoning: str | None = None
    # Explicit terminal response for simple providers; native adapters use
    # ``unknown`` when the wire omitted its terminal marker.
    done_reason: ProviderDoneReason = "completed"
    finish_reason_reported: bool = False
    metadata: dict[str, object] | None = None


@runtime_checkable
class ProviderAbortSignal(Protocol):
    """Cancellation handle a run hands to providers and tools."""

    @property
    def cancelled(self) -> bool: ...

    @property
    def reason(self) -> str | None: ...

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None: ...


@dataclass(frozen=True, slots=True)
class ProviderStreamEvent:
    kind: ProviderStreamEventKind
    channel: ProviderStreamChannel = "text"
    text: str | None = None
    metadata: dict[str, object] | None = None
    error: str | None = None
    error_kind: ProviderErrorKind | None = None
    done_reason: ProviderDoneReason | None = None
    usage: ProviderTokenUsage | None = None
    # Tool-call lifecycle fields are deliberately separate from ``text``. An
    # arguments delta is an opaque JSON fragment; only the turn producer may
    # aggregate and validate it into a ToolCall at the end of a turn.
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    tool_call_ordinal: int | None = None
    fragment_ordinal: int | None = None
    parsed_arguments: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WirePrefixDescriptor:
    """Identity of a final wire prefix, not a cache-hit claim."""

    canonical_bytes: bytes
    canonical_hash: str
    materialized_message_count: int
    tool_generation: str
    assembly_version: int


@dataclass(frozen=True, slots=True)
class ProviderWireMaterialization:
    messages: list[dict[str, object]]
    tools: list[dict[str, object]]
    prefix: WirePrefixDescriptor


@dataclass(slots=True, eq=False)
class ProviderExecutionError(ValueError):
    kind: ProviderErrorKind
    provider_name: str
    model_name: str
    message: str
    # ``None`` means the adapter did not make a recovery decision. Explicit
    # False is a provider veto and must not be overridden by kind defaults.
    retryable: bool | None = None
    fallback_allowed: bool | None = None
    retry_after: float | None = None
    details: dict[str, object] | None = None

    def __str__(self) -> str:
        return self.message


@runtime_checkable
class ProviderTransport(Protocol):
    """Transport seam between provider wire adapters and HTTP/SDK clients."""

    def request(self, payload: dict[str, object]) -> object: ...


@runtime_checkable
class TurnProvider(Protocol):
    @property
    def name(self) -> str: ...

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult: ...


@runtime_checkable
class StreamableTurnProvider(TurnProvider, Protocol):
    """A turn provider that can also answer the same turn as an event stream."""

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]: ...


@runtime_checkable
class ModelTurnProvider(Protocol):
    @property
    def name(self) -> str: ...

    def turn_provider(self) -> TurnProvider: ...
