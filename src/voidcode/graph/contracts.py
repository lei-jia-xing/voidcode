from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Any, Protocol, TypedDict, runtime_checkable

from ..runtime.context_window import RuntimeContextWindow
from ..runtime.events import EventEnvelope, EventSource
from ..runtime.session import SessionState
from ..tools.contracts import ToolCall, ToolDefinition, ToolResult

type AppliedSkill = dict[str, str]


def _update_or_replace(current: object, new: object) -> object:
    return new if new is not None else current


@dataclass(frozen=True, slots=True)
class GraphEvent:
    event_type: str
    source: EventSource
    payload: dict[str, object] = field(default_factory=dict)


class GraphLoopState(TypedDict):
    prompt: str
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
    session: SessionState
    prompt: str
    available_tools: tuple[ToolDefinition, ...] = ()
    applied_skills: tuple[AppliedSkill, ...] = ()
    skill_prompt_context: str = ""
    context_window: RuntimeContextWindow | None = None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GraphRunResult:
    session: SessionState
    events: tuple[EventEnvelope, ...] = ()
    tool_results: tuple[ToolResult, ...] = ()
    output: str | None = None


@runtime_checkable
class GraphStep(Protocol):
    @property
    def tool_call(self) -> ToolCall | None: ...

    @property
    def events(self) -> tuple[Any, ...]: ...

    @property
    def output(self) -> str | None: ...

    @property
    def is_finished(self) -> bool: ...


@runtime_checkable
class RuntimeGraph(Protocol):
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep: ...


@runtime_checkable
class GraphRunner(Protocol):
    def run(self, request: GraphRunRequest) -> GraphRunResult: ...
