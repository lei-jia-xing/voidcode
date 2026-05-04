from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, cast, runtime_checkable

from ..runtime.context_window import normalize_read_file_output
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult
from .model_catalog import ProviderModelMetadata

type ProviderMessageRole = Literal["system", "user", "assistant", "tool"]
type ProviderStreamEventKind = Literal["delta", "content", "error", "done"]
type ProviderStreamChannel = Literal["text", "tool", "reasoning", "error"]
type ProviderDoneReason = Literal["completed", "cancelled", "error"]
type ProviderErrorKind = Literal[
    "missing_auth",
    "invalid_model",
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
    def tool_results(self) -> tuple[ToolResult, ...]: ...

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
    reasoning_effort: str | None = None
    attempt: int = 0
    abort_signal: ProviderAbortSignal | None = None

    @property
    def prompt(self) -> str:
        return self.assembled_context.prompt

    @property
    def tool_results(self) -> tuple[ToolResult, ...]:
        return self.assembled_context.tool_results

    @property
    def context_window(self) -> ProviderContextWindow:
        if self.bounded_context_window is not None:
            return self.bounded_context_window
        payload = self.assembled_context.metadata
        retained_raw = payload.get("retained_tool_result_count")
        retained_count = (
            retained_raw
            if isinstance(retained_raw, int)
            else len(self.assembled_context.tool_results)
        )
        return _DerivedContextWindow(
            prompt=self.assembled_context.prompt,
            tool_results=self.assembled_context.tool_results,
            continuity_state=self.assembled_context.continuity_state,
            compacted=bool(payload.get("compacted", False)),
            retained_tool_result_count=retained_count,
            token_budget=cast(int | None, payload.get("token_budget")),
            token_estimate_source=cast(str | None, payload.get("token_estimate_source")),
            original_tool_result_tokens=cast(
                int | None, payload.get("original_tool_result_tokens")
            ),
            retained_tool_result_tokens=cast(
                int | None, payload.get("retained_tool_result_tokens")
            ),
            dropped_tool_result_tokens=cast(int | None, payload.get("dropped_tool_result_tokens")),
            original_tool_result_count=cast(int | None, payload.get("original_tool_result_count")),
            compaction_reason=cast(str | None, payload.get("compaction_reason")),
            summary_anchor=cast(str | None, payload.get("summary_anchor")),
            summary_source=cast(dict[str, object] | None, payload.get("summary_source")),
        )

    @property
    def applied_skills(self) -> tuple[dict[str, str], ...]:
        return ()


@dataclass(frozen=True, slots=True)
class _DerivedContextWindow:
    prompt: str
    tool_results: tuple[ToolResult, ...]
    continuity_state: object | None = None
    compacted: bool = False
    retained_tool_result_count: int = 0
    token_budget: int | None = None
    token_estimate_source: str | None = None
    original_tool_result_tokens: int | None = None
    retained_tool_result_tokens: int | None = None
    dropped_tool_result_tokens: int | None = None
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
    def tool_results(self) -> tuple[ToolResult, ...]: ...

    @property
    def continuity_state(self) -> object | None: ...

    @property
    def segments(self) -> tuple[ProviderContextSegmentLike, ...]: ...

    @property
    def metadata(self) -> dict[str, object]: ...


@dataclass(frozen=True, slots=True)
class ProviderTokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def metadata_payload(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + self.cache_read_tokens
        )


@dataclass(frozen=True, slots=True)
class ProviderTurnResult:
    tool_call: ToolCall | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    output: str | None = None
    usage: ProviderTokenUsage | None = None

    def __post_init__(self) -> None:
        if self.tool_call is not None and not self.tool_calls:
            object.__setattr__(self, "tool_calls", (self.tool_call,))
        elif self.tool_call is None and self.tool_calls:
            object.__setattr__(self, "tool_call", self.tool_calls[0])


@runtime_checkable
class ProviderAbortSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


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


def normalize_provider_stream_event(event: ProviderStreamEvent) -> ProviderStreamEvent:
    if event.kind in {"delta", "content"} and event.text is None:
        raise ValueError(f"provider stream event '{event.kind}' requires text")
    if event.kind == "error" and event.error is None:
        raise ValueError("provider stream event 'error' requires error")
    if event.kind == "done" and event.done_reason is None:
        return ProviderStreamEvent(
            kind="done",
            channel=event.channel,
            done_reason="completed",
            usage=event.usage,
        )
    return event


def wrap_provider_stream(
    events: Iterator[ProviderStreamEvent],
    *,
    provider_name: str,
    model_name: str,
    abort_signal: ProviderAbortSignal | None,
    chunk_timeout_seconds: float,
) -> Iterator[ProviderStreamEvent]:
    if chunk_timeout_seconds <= 0:
        raise ValueError("provider stream chunk timeout must be greater than 0")

    if abort_signal is not None and abort_signal.cancelled:
        yield ProviderStreamEvent(
            kind="error",
            channel="error",
            error="provider stream cancelled",
            error_kind="cancelled",
        )
        yield ProviderStreamEvent(kind="done", done_reason="cancelled")
        return

    previous_chunk_at = time.monotonic()
    done_seen = False
    for event in events:
        now = time.monotonic()
        if now - previous_chunk_at > chunk_timeout_seconds:
            raise ProviderExecutionError(
                kind="transient_failure",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream chunk timeout exceeded",
            )
        previous_chunk_at = now

        if abort_signal is not None and abort_signal.cancelled:
            yield ProviderStreamEvent(
                kind="error",
                channel="error",
                error="provider stream cancelled",
                error_kind="cancelled",
            )
            yield ProviderStreamEvent(kind="done", done_reason="cancelled")
            return

        normalized = normalize_provider_stream_event(event)
        yield normalized
        if normalized.kind == "done":
            done_seen = True
            break

    if not done_seen:
        yield ProviderStreamEvent(kind="done", done_reason="completed")


@dataclass(frozen=True, slots=True)
class ProviderExecutionError(ValueError):
    kind: ProviderErrorKind
    provider_name: str
    model_name: str
    message: str
    retryable: bool = False
    details: dict[str, object] | None = None

    def __str__(self) -> str:
        return self.message


@runtime_checkable
class TurnProvider(Protocol):
    @property
    def name(self) -> str: ...

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult: ...


@runtime_checkable
class StreamableTurnProvider(Protocol):
    @property
    def name(self) -> str: ...

    def stream_turn(self, request: ProviderTurnRequest) -> Iterator[ProviderStreamEvent]: ...


@runtime_checkable
class ModelTurnProvider(Protocol):
    @property
    def name(self) -> str: ...

    def turn_provider(self) -> TurnProvider: ...


ModelProvider = ModelTurnProvider


@dataclass(frozen=True, slots=True)
class StubTurnProvider:
    name: str

    def propose_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        assembled_context = request.assembled_context
        commands = [line.strip() for line in assembled_context.prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")

        step_index = len(assembled_context.tool_results)
        if step_index >= len(commands):
            if not assembled_context.tool_results:
                raise ValueError("request must contain at least one actionable command")
            last_result = assembled_context.tool_results[-1]
            return ProviderTurnResult(output=_normalize_tool_output(last_result.content))

        from ..command.resolver import resolve_tool_instruction  # lazy to avoid circular import

        resolution = resolve_tool_instruction(
            commands[step_index],
            request.available_tools,
            unavailable_message_suffix="single-agent execution",
        )
        return ProviderTurnResult(tool_call=resolution.tool_call)


def _normalize_tool_output(content: str | None) -> str:
    normalized = normalize_read_file_output(content)
    return "" if normalized is None else normalized
