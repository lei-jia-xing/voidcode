from __future__ import annotations

from collections import deque
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol
from uuid import uuid4

from ..provider.protocol import ProviderTokenUsage
from ..security.json_values import json_wire_object
from ..tools.contracts import AttachmentOutput, ToolCall
from . import turns
from .transcript import ContextSegment, ToolResultView, project_report, tool_result_output
from .turns import FinalTurn, ReportedCall, ToolTurn, TurnPlan, TurnProducer, TurnRequest


@dataclass(frozen=True, slots=True)
class CallReported:
    report: ReportedCall


@dataclass(frozen=True, slots=True)
class CallPaused:
    """Pause without publishing a completion for the active native call."""


@dataclass(frozen=True, slots=True)
class CallStoppedBeforeReport:
    """Stop before the active native call has a report."""


@dataclass(frozen=True, slots=True)
class CallStoppedAfterReport:
    report: ReportedCall


type CallOutcome = CallReported | CallPaused | CallStoppedBeforeReport | CallStoppedAfterReport


@dataclass(slots=True)
class TurnBatch:
    calls: tuple[ToolCall, ...]
    reasoning: str | None = None
    provider_usage: ProviderTokenUsage | None = None
    run_step: int = 1
    results: list[ReportedCall] = field(default_factory=list)


@dataclass(slots=True)
class EngineState:
    request: TurnRequest
    results: list[ReportedCall]
    pending: deque[ToolCall] = field(default_factory=deque)
    batches: list[TurnBatch] = field(default_factory=list)
    plan: TurnPlan | None = None
    output: str | None = None
    transcript: list[TurnBatch | ContextSegment] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.transcript.append(ContextSegment("user", self.request.prompt))

    @property
    def at_safe_boundary(self) -> bool:
        return not self.pending

    @property
    def cancelled(self) -> bool:
        return self.request.abort_signal is not None and self.request.abort_signal.cancelled

    def begin_batch(self, plan: ToolTurn) -> None:
        if self.pending:
            raise RuntimeError("cannot replace an unfinished tool batch")
        calls = tuple(call if call.tool_call_id is not None else replace(call, tool_call_id=f"call-{uuid4().hex}") for call in plan.calls)
        if len({call.tool_call_id for call in calls}) != len(calls):
            raise ValueError("native batch contains duplicate tool call identities")
        self.plan = replace(plan, calls=calls)
        batch = TurnBatch(calls, plan.reasoning, plan.provider_usage, self.request.run_step)
        self.batches.append(batch)
        self.transcript.append(batch)
        self.pending.extend(calls)

    def advance(self, call: ToolCall, report: ReportedCall) -> None:
        if not self.pending or self.pending[0] != call:
            raise RuntimeError("tool result does not advance the active batch")
        if report.tool_call_id != call.tool_call_id:
            raise ValueError("reported result does not match the active native identity")
        owned = replace(report)
        self.results.append(owned)
        self.batches[-1].results.append(owned)
        self.pending.popleft()
        self.request = replace(self.request, run_step=self.request.run_step + 1)

    def transcript_segments(self, tool_results: Sequence[ToolResultView] | None = None) -> tuple[ContextSegment, ...]:
        views = None if tool_results is None else {result.tool_call_id: result for result in tool_results}
        segments: list[ContextSegment] = []
        for entry in self.transcript:
            if isinstance(entry, ContextSegment):
                segments.append(entry)
                continue
            batch = entry
            if len(batch.results) != len(batch.calls):
                continue
            for call, report in zip(batch.calls, batch.results, strict=True):
                if views is not None:
                    projected = views.get(report.tool_call_id)
                    if projected is None:
                        continue
                else:
                    projected = project_report(report)
                data: dict[str, object] = {
                    "tool_call_id": report.tool_call_id,
                    "arguments": json_wire_object(report.authorized_arguments),
                }
                if batch.reasoning is not None:
                    data["reasoning_content"] = batch.reasoning
                if isinstance(projected.output, AttachmentOutput):
                    data["mime"] = projected.output.mime
                    data["data_uri"] = projected.output.data_uri
                content = tool_result_output(projected)
                bounds = projected.output.bounds
                segments.extend(
                    (
                        ContextSegment("assistant", None, call.tool_call_id, call.tool_name, json_wire_object(call.arguments)),
                        ContextSegment(
                            "tool",
                            projected.error if content is None else content,
                            call.tool_call_id,
                            call.tool_name,
                            metadata={
                                "status": projected.status,
                                "error": projected.error,
                                "data": data,
                                "truncated": bounds.truncated or projected.clipped or projected.pruned,
                                "partial": bounds.partial or projected.clipped or projected.pruned,
                                "reference": bounds.reference.uri if bounds.reference is not None else None,
                            },
                        ),
                    )
                )
        return tuple(segments)


@dataclass(frozen=True, slots=True)
class EngineResult:
    status: Literal["completed", "paused", "stopped", "aborted"]
    output: str | None
    tool_results: tuple[ReportedCall, ...]
    pending_calls: tuple[ToolCall, ...] = ()


class TurnHost[T](Protocol):
    def prepare(self, state: EngineState) -> Generator[T, None, TurnRequest | None]: ...

    def invoke(self, producer: TurnProducer, state: EngineState) -> Generator[T, None, TurnPlan | None]: ...

    def observe(self, plan: TurnPlan, state: EngineState) -> Generator[T, None, bool]: ...

    def execute(self, call: ToolCall, state: EngineState) -> Generator[T, None, CallOutcome]: ...

    def finish(self, plan: FinalTurn, state: EngineState) -> Generator[T, None, str | None]: ...

    def drain_messages(self, *, kind: Literal["steering", "followup"]) -> tuple[str, ...]: ...


class TurnEngine:
    """Drive complete turns and tool batches through one explicit host boundary."""

    def __init__(self, producer: TurnProducer) -> None:
        self.producer = producer

    def run[T](
        self,
        request: TurnRequest,
        *,
        host: TurnHost[T],
        tool_results: Sequence[ReportedCall] = (),
        seed: turns.CallSeed | None = None,
    ) -> Generator[T, None, EngineResult]:
        state = EngineState(request, list(tool_results))
        if seed is not None:
            completed_result_ids = {report.tool_call_id for report in state.results}
            if seed.run_step is not None:
                state.request = replace(state.request, run_step=seed.run_step)
            state.begin_batch(ToolTurn(calls=seed.calls, reasoning=seed.reasoning))
            for report in seed.completed_reports:
                if not state.pending or report.tool_call_id != state.pending[0].tool_call_id:
                    raise ValueError("restored results must be the completed prefix of the original batch")
                owned = replace(report)
                state.batches[-1].results.append(owned)
                if report.tool_call_id not in completed_result_ids:
                    state.results.append(owned)
                    completed_result_ids.add(report.tool_call_id)
                state.pending.popleft()
            state.request = replace(state.request, run_step=state.request.run_step + len(seed.completed_reports))
            state.plan = ToolTurn(calls=tuple(state.pending), reasoning=seed.reasoning) if state.pending else None
        while True:
            if state.cancelled:
                return self._result(state, "aborted")
            if state.at_safe_boundary:
                steering = host.drain_messages(kind="steering")
                if steering:
                    state.request = replace(state.request, prompt=self._append_messages(state.request.prompt, steering))
                    state.transcript.append(ContextSegment("user", "\n\n".join(steering)))
            prepared = yield from host.prepare(state)
            if prepared is None:
                return self._result(state, "stopped")
            state.request = prepared
            if state.cancelled:
                return self._result(state, "aborted")
            if state.pending:
                plan = state.plan
                assert plan is not None
            else:
                plan = yield from host.invoke(self.producer, state)
                if plan is None:
                    return self._result(state, "stopped")
                if isinstance(plan, ToolTurn):
                    state.begin_batch(plan)
                    plan = state.plan
                    assert plan is not None
            if not (yield from host.observe(plan, state)):
                return self._result(state, "stopped")
            if state.cancelled:
                return self._result(state, "aborted")
            if isinstance(plan, FinalTurn):
                state.transcript.append(ContextSegment("assistant", plan.output))
                followup = host.drain_messages(kind="followup")
                if followup:
                    state.transcript.append(ContextSegment("user", "\n\n".join(followup)))
                    state.request = replace(
                        state.request,
                        prompt=self._append_messages(state.request.prompt, followup),
                        run_step=state.request.run_step + 1,
                    )
                    continue
                continuation = yield from host.finish(plan, state)
                if state.cancelled:
                    return self._result(state, "aborted")
                if continuation is not None:
                    if continuation != state.request.prompt:
                        state.transcript.append(ContextSegment("user", continuation))
                    state.request = replace(state.request, prompt=continuation, run_step=state.request.run_step + 1)
                    continue
                state.output = plan.output
                return self._result(state, "completed")
            call = state.pending[0]
            outcome = yield from host.execute(call, state)
            if isinstance(outcome, CallPaused):
                return self._result(state, "paused")
            if isinstance(outcome, (CallReported, CallStoppedAfterReport)):
                state.advance(call, outcome.report)
            if state.cancelled:
                return self._result(state, "aborted")
            if isinstance(outcome, (CallStoppedBeforeReport, CallStoppedAfterReport)):
                return self._result(state, "stopped")
            if state.pending:
                state.plan = ToolTurn(
                    calls=(state.pending[0],),
                    reasoning=state.batches[-1].reasoning,
                )
            else:
                state.plan = None

    @staticmethod
    def _append_messages(prompt: str, messages: tuple[str, ...]) -> str:
        return "\n\n".join((prompt, *messages)) if prompt else "\n\n".join(messages)

    @staticmethod
    def _result(state: EngineState, status: Literal["completed", "paused", "stopped", "aborted"]) -> EngineResult:
        return EngineResult(status, state.output, tuple(state.results), tuple(state.pending))
