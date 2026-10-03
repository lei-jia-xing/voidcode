from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import ClassVar, Literal, Protocol, runtime_checkable

from ..provider.protocol import ProviderAbortSignal, ProviderStreamEvent, ProviderTokenUsage
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult
from .transcript import AssembledContext, ContextWindow, ToolResultView

type TurnFactKind = Literal[
    "loop_step",
    "model_turn",
    "response_ready",
    "provider_stream",
    "tool_call_start",
    "tool_call_delta",
    "tool_call_end",
    "tool_requested",
    "tool_completed",
]
type ToolCallPreviewBuilder = Callable[[str, tuple[str, ...], dict[str, object] | None], dict[str, object] | None]


@dataclass(frozen=True, slots=True)
class CallSeed:
    """An authentic host-supplied continuation, not an authorization grant."""

    calls: tuple[ToolCall, ...]
    reasoning: str | None = None
    completed_results: tuple[ToolResult, ...] = ()
    run_step: int | None = None


def normalize_call_result(call: ToolCall, result: ToolResult, *, final_arguments: Mapping[str, object] | None = None) -> ToolResult:
    """Validate native identity before publication; retain authorized arguments on advancement."""
    call_id = call.tool_call_id
    if not call_id:
        raise ValueError("tool outcome has no original normalized call identity")
    result_id = result.data.get("tool_call_id")
    if result_id is not None and result_id != call_id:
        raise ValueError("tool result does not match the active call identity")
    arguments = result.data.get("arguments") if final_arguments is None else final_arguments
    if arguments is None:
        arguments = call.arguments
    if not isinstance(arguments, Mapping):
        raise ValueError("tool outcome arguments must be an object")
    recorded_arguments = result.data.get("arguments")
    if result_id == call_id and (recorded_arguments is arguments or recorded_arguments == arguments):
        return result
    return replace(result, data={**result.data, "tool_call_id": call_id, "arguments": dict(arguments)})


@dataclass(frozen=True, slots=True)
class LoopStepFact:
    step: int
    phase: Literal["plan", "finalize"]
    kind: ClassVar[Literal["loop_step"]] = "loop_step"


@dataclass(frozen=True, slots=True)
class ModelTurnFact:
    turn: int
    mode: Literal["deterministic", "provider"]
    prompt: str
    provider: str | None = None
    model: str | None = None
    attempt: int = 0
    streaming: bool = False
    kind: ClassVar[Literal["model_turn"]] = "model_turn"


@dataclass(frozen=True, slots=True)
class ResponseReadyFact:
    output_preview: str
    finish_reason: str | None = None
    finish_reason_reported: bool | None = None
    kind: ClassVar[Literal["response_ready"]] = "response_ready"


@dataclass(frozen=True, slots=True)
class StreamFact:
    event: ProviderStreamEvent
    diff_preview: dict[str, object] | None = None
    tool_name: str | None = None

    @property
    def kind(self) -> Literal["provider_stream", "tool_call_start", "tool_call_delta", "tool_call_end"]:
        return self.event.kind if self.event.kind in {"tool_call_start", "tool_call_delta", "tool_call_end"} else "provider_stream"


@dataclass(frozen=True, slots=True)
class ToolRequestedFact:
    call: ToolCall
    diff_preview: dict[str, object] | None = None
    kind: ClassVar[Literal["tool_requested"]] = "tool_requested"


@dataclass(frozen=True, slots=True)
class ToolCompletedFact:
    call: ToolCall
    result: ToolResult
    batch: CallSeed | None = None
    kind: ClassVar[Literal["tool_completed"]] = "tool_completed"


TurnFact = LoopStepFact | ModelTurnFact | ResponseReadyFact | StreamFact | ToolRequestedFact | ToolCompletedFact


@runtime_checkable
class TurnSession(Protocol):
    @property
    def session_id(self) -> str: ...

    @property
    def metadata(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class TurnSessionSnapshot:
    session_id: str
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class TurnRequest:
    session: TurnSession
    prompt: str
    assembled_context: AssembledContext
    available_tools: tuple[ToolDefinition, ...] = ()
    context_window: ContextWindow | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    abort_signal: ProviderAbortSignal | None = None
    fact_sink: Callable[[TurnFact], None] | None = None
    tool_call_preview: ToolCallPreviewBuilder | None = None
    run_step: int = 1
    run_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnPlan:
    facts: tuple[TurnFact, ...] = ()
    tool_calls: tuple[ToolCall, ...] = ()
    output: str | None = None
    is_finished: bool = False
    provider_usage: ProviderTokenUsage | None = None
    reasoning: str | None = None

    def __post_init__(self) -> None:
        if self.is_finished:
            if self.tool_calls or self.output is None:
                raise ValueError("finished turns require output and no tool calls")
        elif not self.tool_calls or self.output is not None:
            raise ValueError("continuing turns require tool calls and no final output")


type TurnStreamItem = TurnFact | TurnPlan


@runtime_checkable
class TurnProducer(Protocol):
    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> TurnPlan: ...


@runtime_checkable
class StreamingTurnProducer(Protocol):
    def stream_produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> Iterator[TurnStreamItem]: ...
