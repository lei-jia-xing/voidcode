from __future__ import annotations

import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from ..tools.contracts import ToolCall, ToolDefinition, ToolResult

type AppliedSkill = dict[str, str]
type ProviderStreamEventKind = Literal["delta", "content", "error", "done"]
type ProviderStreamChannel = Literal["text", "tool", "reasoning", "error"]
type ProviderDoneReason = Literal["completed", "cancelled", "error"]

READ_REQUEST_PATTERN = re.compile(r"^(read|show)\s+(?P<path>.+)$", re.IGNORECASE)
GREP_REQUEST_PATTERN = re.compile(r"^grep\s+(?P<pattern>.+?)\s+(?P<path>\S+)$", re.IGNORECASE)
RUN_REQUEST_PATTERN = re.compile(r"^run\s+(?P<command>.+)$", re.IGNORECASE)
WRITE_REQUEST_PATTERN = re.compile(r"^write\s+(?P<path>\S+)\s+(?P<content>.+)$", re.IGNORECASE)


@runtime_checkable
class SingleAgentContextWindow(Protocol):
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
class SingleAgentTurnRequest:
    prompt: str
    available_tools: tuple[ToolDefinition, ...]
    tool_results: tuple[ToolResult, ...]
    context_window: SingleAgentContextWindow
    applied_skills: tuple[AppliedSkill, ...]
    raw_model: str | None
    provider_name: str | None
    model_name: str | None
    skill_prompt_context: str = ""
    agent_preset: dict[str, object] | None = None
    attempt: int = 0
    abort_signal: SingleAgentAbortSignal | None = None


@dataclass(frozen=True, slots=True)
class SingleAgentTurnResult:
    tool_call: ToolCall | None = None
    output: str | None = None


@runtime_checkable
class SingleAgentAbortSignal(Protocol):
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
        )
    return event


def wrap_provider_stream(
    events: Iterator[ProviderStreamEvent],
    *,
    provider_name: str,
    model_name: str,
    abort_signal: SingleAgentAbortSignal | None,
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
class SingleAgentProvider(Protocol):
    @property
    def name(self) -> str: ...

    def propose_turn(self, request: SingleAgentTurnRequest) -> SingleAgentTurnResult: ...


@runtime_checkable
class StreamableSingleAgentProvider(Protocol):
    @property
    def name(self) -> str: ...

    def stream_turn(self, request: SingleAgentTurnRequest) -> Iterator[ProviderStreamEvent]: ...


@runtime_checkable
class ModelProvider(Protocol):
    @property
    def name(self) -> str: ...

    def single_agent_provider(self) -> SingleAgentProvider: ...


@dataclass(frozen=True, slots=True)
class StubSingleAgentProvider:
    name: str

    def propose_turn(self, request: SingleAgentTurnRequest) -> SingleAgentTurnResult:
        commands = [line.strip() for line in request.prompt.splitlines() if line.strip()]
        if not commands:
            raise ValueError("request must not be empty")

        step_index = len(request.tool_results)
        if step_index >= len(commands):
            if not request.context_window.tool_results:
                raise ValueError("request must contain at least one actionable command")
            last_result = request.context_window.tool_results[-1]
            return SingleAgentTurnResult(output=last_result.content if last_result.content else "")

        trimmed_prompt = commands[step_index]

        read_match = READ_REQUEST_PATTERN.match(trimmed_prompt)
        if read_match is not None:
            path_text = read_match.group("path").strip()
            if not path_text:
                raise ValueError("request path must not be empty")
            self._ensure_tool(request.available_tools, "read_file", read_only=True)
            return SingleAgentTurnResult(tool_call=ToolCall("read_file", {"path": path_text}))

        grep_match = GREP_REQUEST_PATTERN.match(trimmed_prompt)
        if grep_match is not None:
            pattern_text = grep_match.group("pattern").strip()
            path_text = grep_match.group("path").strip()
            if not pattern_text:
                raise ValueError("request pattern must not be empty")
            if not path_text:
                raise ValueError("request path must not be empty")
            self._ensure_tool(request.available_tools, "grep", read_only=True)
            return SingleAgentTurnResult(
                tool_call=ToolCall("grep", {"pattern": pattern_text, "path": path_text})
            )

        run_match = RUN_REQUEST_PATTERN.match(trimmed_prompt)
        if run_match is not None:
            command_text = run_match.group("command").strip()
            if not command_text:
                raise ValueError("request command must not be empty")
            self._ensure_tool(request.available_tools, "shell_exec", read_only=False)
            return SingleAgentTurnResult(
                tool_call=ToolCall("shell_exec", {"command": command_text})
            )

        write_match = WRITE_REQUEST_PATTERN.match(trimmed_prompt)
        if write_match is not None:
            path_text = write_match.group("path").strip()
            content_text = write_match.group("content")
            if not path_text:
                raise ValueError("request path must not be empty")
            if not content_text:
                raise ValueError("request content must not be empty")
            self._ensure_tool(request.available_tools, "write_file", read_only=False)
            return SingleAgentTurnResult(
                tool_call=ToolCall("write_file", {"path": path_text, "content": content_text})
            )

        msg = (
            "unsupported request: use 'read <relative-path>', 'show <relative-path>', "
            "'grep <pattern> <relative-path>', 'run <command>', or "
            "'write <relative-path> <content>'"
        )
        raise ValueError(msg)

    @staticmethod
    def _ensure_tool(tools: tuple[ToolDefinition, ...], tool_name: str, *, read_only: bool) -> None:
        if any(tool.name == tool_name and tool.read_only is read_only for tool in tools):
            return
        raise ValueError(f"{tool_name} tool is not registered for single-agent execution")
