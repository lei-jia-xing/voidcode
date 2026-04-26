from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..command.resolver import resolve_tool_instruction
from ..runtime.context_window import normalize_read_file_output
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult

type AppliedSkill = dict[str, str]
type ProviderStreamEventKind = Literal["delta", "content", "error", "done"]
type ProviderStreamChannel = Literal["text", "tool", "reasoning", "error"]
type ProviderDoneReason = Literal["completed", "cancelled", "error"]


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
    prompt: str
    available_tools: tuple[ToolDefinition, ...]
    tool_results: tuple[ToolResult, ...]
    context_window: ProviderContextWindow
    applied_skills: tuple[AppliedSkill, ...]
    raw_model: str | None
    provider_name: str | None
    model_name: str | None
    skill_prompt_context: str = ""
    agent_preset: dict[str, object] | None = None
    attempt: int = 0
    abort_signal: ProviderAbortSignal | None = None


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
    output: str | None = None
    usage: ProviderTokenUsage | None = None


@runtime_checkable
class ProviderAbortSignal(Protocol):
    @property
    def cancelled(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ProviderStreamEvent:
    kind: ProviderStreamEventKind
    channel: ProviderStreamChannel = "text"
    text: str | None = None
    error: str | None = None
    error_kind: (
        Literal[
            "rate_limit",
            "context_limit",
            "invalid_model",
            "transient_failure",
            "cancelled",
        ]
        | None
    ) = None
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
    kind: Literal[
        "rate_limit",
        "context_limit",
        "invalid_model",
        "transient_failure",
        "cancelled",
    ]
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
        commands = [line.strip() for line in request.prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")

        step_index = len(request.tool_results)
        if step_index >= len(commands):
            if not request.context_window.tool_results:
                raise ValueError("request must contain at least one actionable command")
            last_result = request.context_window.tool_results[-1]
            return ProviderTurnResult(output=_normalize_tool_output(last_result.content))

        resolution = resolve_tool_instruction(
            commands[step_index],
            request.available_tools,
            unavailable_message_suffix="single-agent execution",
        )
        return ProviderTurnResult(tool_call=resolution.tool_call)


def _normalize_tool_output(content: str | None) -> str:
    normalized = normalize_read_file_output(content)
    return "" if normalized is None else normalized
