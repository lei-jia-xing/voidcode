from __future__ import annotations

from collections import deque
from collections.abc import Generator
from dataclasses import dataclass, field, replace
from typing import Literal
from uuid import uuid4

from ..provider.protocol import ProviderAbortSignal
from ..tools.contracts import Tool, ToolCall, ToolResult
from .engine import CallOutcome, EngineState, TurnBatch
from .event_store import FactStore, MemoryEventStore
from .tool_context import ToolContext
from .transcript import ContextSegment, ToolResultView
from .turns import (
    CallSeed,
    StreamFact,
    StreamingTurnProducer,
    ToolCompletedFact,
    ToolRequestedFact,
    TurnFact,
    TurnPlan,
    TurnProducer,
    TurnRequest,
    TurnSessionSnapshot,
    normalize_call_result,
)


@dataclass(slots=True)
class MemoryAbortSignal:
    cancelled: bool = False
    reason: str | None = None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self.cancelled = value
        if value and reason is not None:
            self.reason = reason


@dataclass(frozen=True, slots=True)
class MemoryContext:
    prompt: str
    tool_results: tuple[ToolResultView, ...] = ()
    segments: tuple[ContextSegment, ...] = ()
    continuity_state: object | None = None
    metadata: dict[str, object] = field(default_factory=dict)


class MemoryHost:
    """An actual transient tool/context host; it owns no runtime or durable state."""

    def __init__(self, *, tools: tuple[Tool, ...], abort_signal: ProviderAbortSignal | None = None, event_store: FactStore | None = None) -> None:
        self._tools = {tool.definition.name: tool for tool in tools}
        self.abort_signal = abort_signal or MemoryAbortSignal()
        self.session = TurnSessionSnapshot(f"memory-{uuid4().hex}")
        self.facts: list[TurnFact] = []
        self.event_store = MemoryEventStore() if event_store is None else event_store
        self._steering: deque[str] = deque()
        self._followup: deque[str] = deque()
        self._current_batch: TurnBatch | None = None
        self._batch_seed: CallSeed | None = None

    def _record(self, fact: TurnFact) -> None:
        self.facts.append(fact)
        if not isinstance(fact, StreamFact):
            self.event_store.append((fact,))

    def request(self, prompt: str, *, streaming: bool = False) -> TurnRequest:
        return TurnRequest(
            session=self.session,
            prompt=prompt,
            assembled_context=MemoryContext(prompt),
            available_tools=tuple(tool.definition for tool in self._tools.values()),
            metadata={"provider_stream": streaming},
            abort_signal=self.abort_signal,
            run_id=f"memory-run-{uuid4().hex}",
        )

    def steer(self, message: str) -> None:
        self._steering.append(message)

    def followup(self, message: str) -> None:
        self._followup.append(message)

    def drain_messages(self, *, kind: Literal["steering", "followup"]) -> tuple[str, ...]:
        queue = self._steering if kind == "steering" else self._followup
        messages = tuple(queue)
        queue.clear()
        return messages

    def prepare(self, state: EngineState) -> Generator[TurnFact, None, TurnRequest]:
        context = MemoryContext(
            state.request.prompt,
            tuple(ToolResultView(result=result, content=result.content) for result in state.results),
            state.transcript_segments(),
        )
        yield from ()
        return replace(state.request, assembled_context=context, abort_signal=self.abort_signal)

    def invoke(self, producer: TurnProducer, state: EngineState) -> Generator[TurnFact, None, TurnPlan]:
        request = state.request
        if request.metadata.get("provider_stream") is True and isinstance(producer, StreamingTurnProducer):
            plan: TurnPlan | None = None
            for item in producer.stream_produce(request, tuple(state.results), session=request.session):
                if isinstance(item, TurnFact):
                    self._record(item)
                    yield item
                else:
                    plan = item
            if plan is None:
                raise RuntimeError("provider stream ended without a complete turn")
            return plan
        return producer.produce(request, tuple(state.results), session=request.session)

    def observe(self, plan: TurnPlan, state: EngineState) -> Generator[TurnFact, None, bool]:
        if state.batches and self._current_batch is not state.batches[-1]:
            self._current_batch = state.batches[-1]
            self._batch_seed = CallSeed(self._current_batch.calls, reasoning=self._current_batch.reasoning, run_step=self._current_batch.run_step)
        for fact in plan.facts:
            self._record(fact)
            yield fact
        return not state.cancelled

    def execute(self, call: ToolCall, state: EngineState) -> Generator[TurnFact, None, CallOutcome]:
        requested = ToolRequestedFact(call)
        self._record(requested)
        yield requested
        tool = self._tools.get(call.tool_name)
        if tool is None:
            result = ToolResult(tool_name=call.tool_name, status="error", error=f"unknown tool: {call.tool_name}")
        else:
            result = tool.invoke(
                call,
                context=ToolContext(
                    session_id=self.session.session_id,
                    run_id=state.request.run_id,
                    invocation_id=call.tool_call_id,
                    abort_signal=self.abort_signal,
                ),
            )
        result = normalize_call_result(call, result, final_arguments=call.arguments)
        completed = ToolCompletedFact(call, result, batch=self._batch_seed)
        self._record(completed)
        yield completed
        return CallOutcome("result", result)

    def finish(self, _plan: TurnPlan, _state: EngineState) -> Generator[TurnFact, None, str | None]:
        yield from ()
        return None
