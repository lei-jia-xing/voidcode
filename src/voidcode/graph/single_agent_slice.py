from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from ..provider.errors import parse_provider_stream_error
from ..provider.models import ResolvedProviderModel
from ..provider.protocol import (
    ProviderExecutionError,
    ProviderStreamEvent,
    SingleAgentAbortSignal,
    SingleAgentProvider,
    SingleAgentTurnRequest,
    StreamableSingleAgentProvider,
)
from ..runtime.context_window import RuntimeContextWindow
from ..runtime.events import GRAPH_LOOP_STEP, GRAPH_MODEL_TURN, GRAPH_RESPONSE_READY
from ..runtime.session import SessionState
from ..tools.contracts import ToolCall, ToolResult
from .contracts import GraphEvent, GraphRunRequest


@dataclass(frozen=True, slots=True)
class SingleAgentStep:
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False


@dataclass(slots=True)
class _GraphAbortSignal:
    _cancelled: bool = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_cancelled(self, value: bool) -> None:
        self._cancelled = value


class ProviderSingleAgentGraph:
    def __init__(
        self,
        *,
        provider: SingleAgentProvider,
        provider_model: ResolvedProviderModel,
        max_steps: int = 4,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        self._provider = provider
        self._provider_model = provider_model
        self._max_steps = max_steps
        self._abort_signal = _GraphAbortSignal(_cancelled=False)

    def cancel_current_turn(self) -> None:
        self._abort_signal.set_cancelled(True)

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> SingleAgentStep:
        _ = session
        current_turn = len(tool_results) + 1
        if current_turn > self._max_steps:
            raise ValueError(f"graph exceeded max steps: {self._max_steps}")

        provider_stream = request.metadata.get("provider_stream", False)
        if isinstance(provider_stream, bool):
            streaming_enabled = provider_stream
        else:
            streaming_enabled = str(provider_stream).strip().lower() not in {
                "false",
                "0",
                "no",
                "off",
                "",
            }

        planning_events = (
            self._graph_event(
                GRAPH_LOOP_STEP,
                {"step": current_turn, "phase": "plan", "max_steps": self._max_steps},
            ),
            self._graph_event(
                GRAPH_MODEL_TURN,
                {
                    "turn": current_turn,
                    "mode": "single_agent",
                    "provider": self._provider.name,
                    "model": self._provider_model.selection.model,
                    "attempt": request.metadata.get("provider_attempt", 0),
                    "streaming": streaming_enabled,
                    "prompt": request.prompt,
                },
            ),
        )

        abort_requested = request.metadata.get("abort_requested", False)
        if isinstance(abort_requested, bool):
            self._abort_signal.set_cancelled(abort_requested)
        else:
            self._abort_signal.set_cancelled(
                str(abort_requested).strip().lower() not in {"false", "0", "no", "off", ""}
            )
        turn_request = SingleAgentTurnRequest(
            prompt=request.prompt,
            available_tools=request.available_tools,
            tool_results=tool_results,
            context_window=request.context_window or RuntimeContextWindow(prompt=request.prompt),
            applied_skills=request.applied_skills,
            raw_model=self._provider_model.selection.raw_model,
            provider_name=self._provider_model.selection.provider,
            model_name=self._provider_model.selection.model,
            attempt=cast(int, request.metadata.get("provider_attempt", 0)),
            abort_signal=cast(SingleAgentAbortSignal, self._abort_signal),
        )

        if streaming_enabled and isinstance(self._provider, StreamableSingleAgentProvider):
            return self._step_streaming(
                planning_events=planning_events,
                turn_request=turn_request,
                current_turn=current_turn,
            )

        turn_result = self._provider.propose_turn(turn_request)

        if turn_result.output is not None:
            finalize_events = planning_events + (
                self._graph_event(
                    GRAPH_LOOP_STEP,
                    {"step": current_turn + 1, "phase": "finalize", "max_steps": self._max_steps},
                ),
                self._graph_event(GRAPH_RESPONSE_READY, {"output_preview": turn_result.output}),
            )
            return SingleAgentStep(
                events=finalize_events,
                output=turn_result.output,
                is_finished=True,
            )

        return SingleAgentStep(events=planning_events, tool_call=turn_result.tool_call)

    def _step_streaming(
        self,
        *,
        planning_events: tuple[GraphEvent, ...],
        turn_request: SingleAgentTurnRequest,
        current_turn: int,
    ) -> SingleAgentStep:
        stream_provider = cast(StreamableSingleAgentProvider, self._provider)
        stream_events: list[GraphEvent] = []
        output_parts: list[str] = []

        for stream_event in stream_provider.stream_turn(turn_request):
            stream_events.append(self._stream_event_to_graph_event(stream_event))
            if stream_event.kind in {"delta", "content"} and stream_event.channel == "text":
                if stream_event.text is not None:
                    output_parts.append(stream_event.text)
            if stream_event.kind == "error":
                if stream_event.error_kind == "cancelled":
                    raise ProviderExecutionError(
                        kind="cancelled",
                        provider_name=self._provider.name,
                        model_name=turn_request.model_name or "unknown",
                        message=stream_event.error or "provider stream cancelled",
                    )

                error_payload: dict[str, object]
                if stream_event.error is not None:
                    try:
                        raw_payload = json.loads(stream_event.error)
                    except json.JSONDecodeError:
                        error_payload = {"message": stream_event.error}
                    else:
                        error_payload = (
                            cast(dict[str, object], raw_payload)
                            if isinstance(raw_payload, dict)
                            else {"message": stream_event.error}
                        )
                else:
                    error_payload = {"message": "provider stream error"}

                parsed = parse_provider_stream_error(error_payload)
                parsed_kind = parsed.kind
                if stream_event.error_kind in {
                    "rate_limit",
                    "context_limit",
                    "invalid_model",
                    "transient_failure",
                }:
                    parsed_kind = stream_event.error_kind
                raise ProviderExecutionError(
                    kind=parsed_kind,
                    provider_name=self._provider.name,
                    model_name=turn_request.model_name or "unknown",
                    message=parsed.message,
                    details=parsed.details,
                )
            if stream_event.kind == "done":
                if stream_event.done_reason == "cancelled":
                    raise ProviderExecutionError(
                        kind="cancelled",
                        provider_name=self._provider.name,
                        model_name=turn_request.model_name or "unknown",
                        message="provider stream cancelled",
                    )
                if stream_event.done_reason == "error":
                    raise ProviderExecutionError(
                        kind="transient_failure",
                        provider_name=self._provider.name,
                        model_name=turn_request.model_name or "unknown",
                        message="provider stream ended with error",
                    )
                if output_parts:
                    output = "".join(output_parts)
                    finalize_events = (
                        planning_events
                        + tuple(stream_events)
                        + (
                            self._graph_event(
                                GRAPH_LOOP_STEP,
                                {
                                    "step": current_turn + 1,
                                    "phase": "finalize",
                                    "max_steps": self._max_steps,
                                },
                            ),
                            self._graph_event(GRAPH_RESPONSE_READY, {"output_preview": output}),
                        )
                    )
                    return SingleAgentStep(events=finalize_events, output=output, is_finished=True)
                break

        turn_result = self._provider.propose_turn(turn_request)
        if turn_result.output is not None:
            finalize_events = (
                planning_events
                + tuple(stream_events)
                + (
                    self._graph_event(
                        GRAPH_LOOP_STEP,
                        {
                            "step": current_turn + 1,
                            "phase": "finalize",
                            "max_steps": self._max_steps,
                        },
                    ),
                    self._graph_event(GRAPH_RESPONSE_READY, {"output_preview": turn_result.output}),
                )
            )
            return SingleAgentStep(
                events=finalize_events, output=turn_result.output, is_finished=True
            )

        return SingleAgentStep(
            events=planning_events + tuple(stream_events),
            tool_call=turn_result.tool_call,
        )

    @staticmethod
    def _stream_event_to_graph_event(stream_event: ProviderStreamEvent) -> GraphEvent:
        payload: dict[str, object] = {
            "kind": stream_event.kind,
            "channel": stream_event.channel,
        }
        if stream_event.text is not None:
            payload["text"] = stream_event.text
        if stream_event.error is not None:
            payload["error"] = stream_event.error
        if stream_event.error_kind is not None:
            payload["error_kind"] = stream_event.error_kind
        if stream_event.done_reason is not None:
            payload["done_reason"] = stream_event.done_reason
        return GraphEvent(event_type="graph.provider_stream", source="graph", payload=payload)

    @staticmethod
    def _graph_event(event_type: str, payload: dict[str, object]) -> GraphEvent:
        return GraphEvent(event_type=event_type, source="graph", payload=payload)
