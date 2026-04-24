from __future__ import annotations

import operator
from dataclasses import dataclass, field
from typing import Annotated, Protocol, TypedDict, runtime_checkable

from ..provider.protocol import ProviderContextWindow
from ..runtime.events import EventSource
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
    context_window: ProviderContextWindow | None = None
    metadata: dict[str, object] = field(default_factory=dict)


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


@runtime_checkable
class RuntimeGraph(Protocol):
    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep: ...
