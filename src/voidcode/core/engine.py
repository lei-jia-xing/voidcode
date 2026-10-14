from __future__ import annotations

from collections import deque
from collections.abc import Generator, Sequence
from dataclasses import dataclass, field, replace
from typing import Literal, Protocol
from uuid import uuid4

from ..common.json_values import json_wire_object
from ..provider.protocol import ProviderTokenUsage
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
    tool_batch_id: str | None = None


@dataclass(frozen=True, slots=True)
class TurnObservation:
    continue_run: bool
    tool_batch_id: str | None = None


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

    def prepare_batch(self, plan: ToolTurn) -> tuple[ToolTurn, TurnBatch]:
        if self.pending:
            raise RuntimeError("cannot replace an unfinished tool batch")
        calls = tuple(call if call.tool_call_id is not None else replace(call, tool_call_id=f"call-{uuid4().hex}") for call in plan.calls)
        if len({call.tool_call_id for call in calls}) != len(calls):
            raise ValueError("native batch contains duplicate tool call identities")
        normalized = replace(plan, calls=calls)
        return normalized, TurnBatch(calls, plan.reasoning, plan.provider_usage, self.request.run_step)

    def append_batch(self, plan: ToolTurn, batch: TurnBatch, *, tool_batch_id: str | None) -> None:
        if self.pending:
            raise RuntimeError("cannot replace an unfinished tool batch")
        if batch.calls != plan.calls or batch.run_step != self.request.run_step:
            raise ValueError("observed native batch no longer matches its normalized plan")
        batch.tool_batch_id = tool_batch_id
        self.plan = plan
        self.batches.append(batch)
        self.transcript.append(batch)
        self.pending.extend(batch.calls)

    def advance(self, call: ToolCall, report: ReportedCall) -> None:
        if not self.pending or self.pending[0] != call:
            raise RuntimeError("tool result does not advance the active batch")
        if report.tool_call_id != call.tool_call_id:
            raise ValueError("reported result does not match the active native identity")
        batch_id = self.batches[-1].tool_batch_id
        if report.tool_batch_id is not None and report.tool_batch_id != batch_id:
            raise ValueError("reported result does not match the active native batch")
        owned = replace(report, tool_batch_id=batch_id)
        self.results.append(owned)
        self.batches[-1].results.append(owned)
        self.pending.popleft()
        self.request = replace(self.request, run_step=self.request.run_step + 1)

    def transcript_segments(self, tool_results: Sequence[ToolResultView] | None = None) -> tuple[ContextSegment, ...]:
        views = None
        if tool_results is not None:
            views = {}
            for result in tool_results:
                key = (result.tool_batch_id, result.tool_call_id)
                if key in views:
                    raise ValueError("tool result views duplicate a native batch member")
                views[key] = result
        segments: list[ContextSegment] = []
        for entry in self.transcript:
            if isinstance(entry, ContextSegment):
                segments.append(entry)
                continue
            batch = entry
            if len(batch.results) != len(batch.calls):
                continue
            projected_batch: list[tuple[ToolCall, ToolResultView]] = []
            for call, report in zip(batch.calls, batch.results, strict=True):
                key = (batch.tool_batch_id, call.tool_call_id)
                projected = project_report(report) if views is None else views.get(key)
                if projected is None:
                    projected_batch.clear()
                    break
                projected_batch.append((call, projected))
            if len(projected_batch) != len(batch.calls):
                continue
            batch_id = batch.tool_batch_id
            for call, _projected in projected_batch:
                segments.append(
                    ContextSegment(
                        "assistant",
                        None,
                        call.tool_call_id,
                        call.tool_name,
                        json_wire_object(call.arguments),
                        tool_batch_id=batch_id,
                    )
                )
            for call, projected in projected_batch:
                data: dict[str, object] = {
                    "tool_call_id": call.tool_call_id,
                    "arguments": json_wire_object(call.arguments),
                }
                if batch.reasoning is not None:
                    data["reasoning_content"] = batch.reasoning
                if isinstance(projected.output, AttachmentOutput):
                    data["mime"] = projected.output.mime
                    if projected.output.data_uri is not None:
                        data["data_uri"] = projected.output.data_uri
                    else:
                        data["attachment_unavailable"] = True
                        if projected.output.bounds.reference is not None:
                            data["attachment_reference"] = projected.output.bounds.reference.uri
                content = tool_result_output(projected)
                bounds = projected.output.bounds
                segments.append(
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
                        tool_batch_id=batch_id,
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

    def observe(self, plan: TurnPlan, state: EngineState, batch: TurnBatch | None) -> Generator[T, None, TurnObservation]: ...

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
            if seed.run_step is not None:
                state.request = replace(state.request, run_step=seed.run_step)
            plan, batch = state.prepare_batch(ToolTurn(calls=seed.calls, reasoning=seed.reasoning))
            state.append_batch(plan, batch, tool_batch_id=seed.tool_batch_id)
            for report in seed.completed_reports:
                if not state.pending or report.tool_call_id != state.pending[0].tool_call_id:
                    raise ValueError("restored results must be the completed prefix of the original batch")
                key = (seed.tool_batch_id, report.tool_call_id)
                if any((item.tool_batch_id, item.tool_call_id) == key for item in state.results):
                    raise ValueError("restored results are already present for this native batch member")
                owned = replace(report, tool_batch_id=seed.tool_batch_id)
                state.batches[-1].results.append(owned)
                state.results.append(owned)
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
                batch = state.batches[-1]
                new_batch = False
            else:
                plan = yield from host.invoke(self.producer, state)
                if plan is None:
                    return self._result(state, "stopped")
                new_batch = isinstance(plan, ToolTurn)
                if new_batch:
                    plan, batch = state.prepare_batch(plan)
                else:
                    batch = None
            observation = yield from host.observe(plan, state, batch)
            if not observation.continue_run:
                return self._result(state, "stopped")
            if new_batch:
                assert batch is not None and isinstance(plan, ToolTurn)
                state.append_batch(plan, batch, tool_batch_id=observation.tool_batch_id)
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
