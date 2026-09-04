from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Annotated, Final, Literal, Protocol, TypedDict, runtime_checkable

from ..provider.protocol import ProviderAbortSignal, ProviderAssembledContext, ProviderContextWindow
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult

type GraphEventSource = Literal["graph"]
type GraphEventType = str
type AppliedSkill = dict[str, str]
type ToolCallPreviewBuilder = Callable[[str, tuple[str, ...], dict[str, object] | None], dict[str, object] | None]

GRAPH_LOOP_STEP: Final[GraphEventType] = "graph.loop_step"
GRAPH_MODEL_TURN: Final[GraphEventType] = "graph.model_turn"
GRAPH_RESPONSE_READY: Final[GraphEventType] = "graph.response_ready"
GRAPH_PROVIDER_STREAM: Final[GraphEventType] = "graph.provider_stream"
GRAPH_TOOL_CALL_START: Final[GraphEventType] = "graph.tool_call_start"
GRAPH_TOOL_CALL_DELTA: Final[GraphEventType] = "graph.tool_call_delta"
GRAPH_TOOL_CALL_END: Final[GraphEventType] = "graph.tool_call_end"


@runtime_checkable
class GraphSession(Protocol):
    """Minimal read-only session projection consumed by graph execution."""

    @property
    def session_id(self) -> str: ...

    @property
    def metadata(self) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class GraphSessionSnapshot:
    session_id: str
    metadata: Mapping[str, object] = field(default_factory=dict)


def _update_or_replace(current: object, new: object) -> object:
    return new if new is not None else current


@dataclass(frozen=True, slots=True)
class GraphEvent:
    event_type: GraphEventType
    source: GraphEventSource = "graph"
    payload: dict[str, object] = field(default_factory=dict)


class GraphLoopState(TypedDict):
    prompt: str
    metadata: dict[str, object]
    current_turn: Annotated[int, _update_or_replace]
    tool_calls: Annotated[list[ToolCall], operator.add]
    tool_results: Annotated[list[ToolResult], operator.add]
    available_tools: tuple[ToolDefinition, ...]
    events: Annotated[list[GraphEvent], operator.add]
    output: Annotated[str | None, _update_or_replace]
    error: Annotated[str | None, _update_or_replace]
    approval_request_id: Annotated[str | None, _update_or_replace]


@dataclass(frozen=True, slots=True)
class GraphRunRequest:
    session: GraphSession
    prompt: str
    assembled_context: ProviderAssembledContext
    available_tools: tuple[ToolDefinition, ...] = ()
    context_window: ProviderContextWindow | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    abort_signal: ProviderAbortSignal | None = None
    stream_event_sink: Callable[[GraphEvent], None] | None = None
    tool_call_preview: ToolCallPreviewBuilder | None = None


@runtime_checkable
class GraphStep(Protocol):
    @property
    def tool_call(self) -> ToolCall | None: ...

    @property
    def events(self) -> tuple[GraphEvent, ...]: ...

    @property
    def output(self) -> str | None: ...

    @property
    def is_finished(self) -> bool: ...


type GraphStreamItem = GraphEvent | GraphStep


@runtime_checkable
class RuntimeGraph(Protocol):
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: GraphSession,
    ) -> GraphStep: ...
