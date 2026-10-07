from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass, replace
from types import MappingProxyType
from typing import ClassVar, Literal, Protocol, runtime_checkable

from ..provider.protocol import ProviderAbortSignal, ProviderStreamEvent, ProviderTokenUsage
from ..security.json_values import own_json_object
from ..tools.contracts import ToolBody, ToolCall, ToolDefinition, ToolResult
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


def _deepcopy_tool_body[BodyT: ToolBody](body: BodyT) -> BodyT:
    # own_json_object creates detached immutable mapping proxies, which deepcopy cannot pickle.
    memo: dict[int, object] = {}
    seen: set[int] = set()
    pending = [body]
    while pending:
        value = pending.pop()
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if isinstance(value, MappingProxyType):
            memo[identity] = value
            pending.extend(value.values())
        elif isinstance(value, Mapping):
            pending.extend(value.values())
        elif isinstance(value, tuple | list | set | frozenset):
            pending.extend(value)
        elif is_dataclass(value) and not isinstance(value, type):
            pending.extend(getattr(value, item.name) for item in fields(value))
        elif hasattr(value, "__dict__"):
            pending.extend(vars(value).values())
        else:
            slots = getattr(type(value), "__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            pending.extend(getattr(value, name) for name in slots if hasattr(value, name))
    return deepcopy(body, memo)


@dataclass(frozen=True, slots=True)
class ReportedCall:
    """Host-owned pairing and authorized arguments for one genuine native call."""

    tool_call_id: str
    final_tool_name: str
    authorized_arguments: Mapping[str, object]
    result: ToolResult

    def __post_init__(self) -> None:
        if not self.tool_call_id:
            raise ValueError("reported call requires the original native identity")
        if self.result.tool_name != self.final_tool_name:
            raise ValueError("reported result does not match the authorized tool")
        object.__setattr__(self, "authorized_arguments", own_json_object(self.authorized_arguments))
        if self.result.body is not None:
            object.__setattr__(self, "result", replace(self.result, body=_deepcopy_tool_body(self.result.body)))

    def __deepcopy__(self, memo: dict[int, object]) -> ReportedCall:
        return replace(self)


@dataclass(frozen=True, slots=True)
class CallSeed:
    """An authentic host-supplied continuation, not an authorization grant."""

    calls: tuple[ToolCall, ...]
    reasoning: str | None = None
    completed_reports: tuple[ReportedCall, ...] = ()
    run_step: int | None = None


def report_call(
    call: ToolCall,
    result: ToolResult,
    *,
    final_arguments: Mapping[str, object],
    final_tool_name: str,
) -> ReportedCall:
    if call.tool_call_id is None:
        raise ValueError("tool outcome has no original normalized call identity")
    return ReportedCall(call.tool_call_id, final_tool_name, final_arguments, result)


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
    report: ReportedCall
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
class ToolTurn:
    calls: tuple[ToolCall, ...]
    facts: tuple[TurnFact, ...] = ()
    provider_usage: ProviderTokenUsage | None = None
    reasoning: str | None = None

    def __post_init__(self) -> None:
        if not self.calls:
            raise ValueError("tool turns require at least one call")


@dataclass(frozen=True, slots=True)
class FinalTurn:
    output: str
    facts: tuple[TurnFact, ...] = ()
    provider_usage: ProviderTokenUsage | None = None
    reasoning: str | None = None


type TurnPlan = ToolTurn | FinalTurn


type TurnStreamItem = TurnFact | TurnPlan


@runtime_checkable
class TurnProducer(Protocol):
    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> TurnPlan: ...


@runtime_checkable
class StreamingTurnProducer(Protocol):
    def stream_produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> Iterator[TurnStreamItem]: ...
