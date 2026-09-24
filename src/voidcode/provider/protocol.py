from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..runtime.context.window import ToolResultView
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult
from .model_catalog import ProviderModelMetadata

type ProviderMessageRole = Literal["system", "user", "assistant", "tool"]
type ProviderStreamEventKind = Literal["delta", "content", "tool_call_start", "tool_call_delta", "tool_call_end", "error", "done"]
type ProviderStreamChannel = Literal["text", "tool", "reasoning", "error"]
type ProviderCacheRetention = Literal["none", "short", "long"]
# Provider-native finish reasons are intentionally preserved at the adapter boundary.
# ``unknown`` means the upstream omitted or used an unrecognized reason; the graph maps
# it to a completed, stop-equivalent terminal state rather than a failure.
type ProviderDoneReason = Literal["stop", "tool_calls", "length", "content_filter", "function_call", "cancelled", "error", "unknown"]
type ProviderErrorKind = Literal[
    "missing_auth",
    "invalid_model",
    "not_configured",
    "rate_limit",
    "transient_failure",
    "context_limit",
    "unsupported_feature",
    "stream_tool_feedback_shape",
    "cancelled",
]


@runtime_checkable
class ProviderContextWindow(Protocol):
    @property
    def prompt(self) -> str: ...

    @property
    def tool_results(self) -> tuple[ToolResult | ToolResultView, ...]: ...

    @property
    def compacted(self) -> bool: ...

    @property
    def retained_tool_result_count(self) -> int: ...

    @property
    def continuity_state(self) -> object | None: ...


@dataclass(frozen=True, slots=True)
class ProviderTurnRequest:
    assembled_context: ProviderAssembledContext
    bounded_context_window: ProviderContextWindow | None = None
    available_tools: tuple[ToolDefinition, ...] = ()
    raw_model: str | None = None
    provider_name: str | None = None
    model_name: str | None = None
    agent_preset: dict[str, object] | None = None
    model_metadata: ProviderModelMetadata | None = None
    session_id: str | None = None
    reasoning_effort: str | None = None
    cache_retention: ProviderCacheRetention | None = None
    attempt: int = 0
    abort_signal: ProviderAbortSignal | None = None

    @property
    def prompt(self) -> str:
        return self.assembled_context.prompt

    @property
    def tool_results(self) -> tuple[ToolResult | ToolResultView, ...]:
        return self.assembled_context.tool_results

    @property
    def context_window(self) -> ProviderContextWindow:
        if self.bounded_context_window is not None:
            return self.bounded_context_window
        payload = self.assembled_context.metadata
        retained_raw = payload.get("retained_tool_result_count")
        retained_count = retained_raw if isinstance(retained_raw, int) else len(self.assembled_context.tool_results)
        original_tool_result_count = payload.get("original_tool_result_count")
        compaction_reason = payload.get("compaction_reason")
        summary_anchor = payload.get("summary_anchor")
        summary_source = payload.get("summary_source")
        return _DerivedContextWindow(
            prompt=self.assembled_context.prompt,
            tool_results=self.assembled_context.tool_results,
            continuity_state=self.assembled_context.continuity_state,
            compacted=bool(payload.get("compacted", False)),
            retained_tool_result_count=retained_count,
            original_tool_result_count=original_tool_result_count if isinstance(original_tool_result_count, int) else None,
            compaction_reason=compaction_reason if isinstance(compaction_reason, str) else None,
            summary_anchor=summary_anchor if isinstance(summary_anchor, str) else None,
            summary_source=summary_source if isinstance(summary_source, dict) else None,
        )


@dataclass(frozen=True, slots=True)
class _DerivedContextWindow:
    prompt: str
    tool_results: tuple[ToolResult | ToolResultView, ...]
    continuity_state: object | None = None
    compacted: bool = False
    retained_tool_result_count: int = 0
    original_tool_result_count: int | None = None
    compaction_reason: str | None = None
    summary_anchor: str | None = None
    summary_source: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.retained_tool_result_count == 0:
            object.__setattr__(self, "retained_tool_result_count", len(self.tool_results))


@dataclass(frozen=True, slots=True)
class ProviderContextSegment:
    role: ProviderMessageRole
    content: str | None
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_arguments: dict[str, object] | None = None
    metadata: dict[str, object] | None = None


@runtime_checkable
class ProviderContextSegmentLike(Protocol):
    @property
    def role(self) -> ProviderMessageRole: ...

    @property
    def content(self) -> str | None: ...

    @property
    def tool_call_id(self) -> str | None: ...

    @property
    def tool_name(self) -> str | None: ...

    @property
    def tool_arguments(self) -> dict[str, object] | None: ...

    @property
    def metadata(self) -> dict[str, object] | None: ...


@runtime_checkable
class ProviderAssembledContext(Protocol):
    @property
    def prompt(self) -> str: ...

    @property
    def tool_results(self) -> tuple[ToolResult | ToolResultView, ...]: ...

    @property
    def continuity_state(self) -> object | None: ...

    @property
    def segments(self) -> tuple[ProviderContextSegmentLike, ...]: ...

    @property
    def metadata(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ProviderTokenUsage:
    # ``None`` means the provider did not report that metric; zero is an
    # observed zero. Keeping this distinction is required for cache telemetry.
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    uncached_input_tokens: int | None = None
    #: USD for this usage, computed once from the catalog rates + the pricing
    #: policy tier at the moment the turn reported it (never repriced later).
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
    tool_call: ToolCall | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    output: str | None = None
    usage: ProviderTokenUsage | None = None
    # Non-streaming reasoning content (e.g. message.reasoning_content /
    # message.reasoning / thinking_blocks) so non-streaming turns can persist
    # the same runtime.reasoning_part the streaming path aggregates.
    reasoning: str | None = None
    done_reason: ProviderDoneReason = "unknown"
    finish_reason_reported: bool = False
    metadata: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if self.tool_call is not None and not self.tool_calls:
            object.__setattr__(self, "tool_calls", (self.tool_call,))
        elif self.tool_call is None and self.tool_calls:
            object.__setattr__(self, "tool_call", self.tool_calls[0])


@runtime_checkable
class ProviderAbortSignal(Protocol):
    """Cancellation handle a run hands to providers and tools.

    ``set_cancelled`` is part of the contract because a resumed run must be able
    to re-assert a cancellation that was recorded before the turn started;
    ``reason`` carries the operator- or timeout-supplied cause to the surfaces
    that report it (interrupt payloads, tool results).
    """

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
    # arguments delta is an opaque JSON fragment; only the graph/provider may
    # aggregate and validate it into a ToolCall at the end of a turn.
    tool_call_id: str | None = None
    tool_name: str | None = None
    arguments_delta: str | None = None
    tool_call_ordinal: int | None = None
    fragment_ordinal: int | None = None
    parsed_arguments: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class WirePrefixDescriptor:
    """Identity of a final wire prefix, not a cache-hit claim.

    ``materialized_message_count`` counts every final wire message, while the
    canonical bytes only include its stable system prefix and tool schemas.
    """

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


@dataclass(frozen=True, slots=True, eq=False)
class ProviderExecutionError(ValueError):
    kind: ProviderErrorKind
    provider_name: str
    model_name: str
    message: str
    # ``None`` means the adapter did not make a recovery decision. Runtime
    # fallback policy may then apply its kind defaults; explicit False is a
    # provider veto and must not be overridden by those defaults.
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
