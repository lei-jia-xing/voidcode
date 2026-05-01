from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, cast

from ..provider.errors import parse_provider_stream_error
from ..provider.models import ResolvedProviderModel
from ..provider.protocol import (
    ProviderAbortSignal,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    StreamableTurnProvider,
    TurnProvider,
)
from ..runtime.events import GRAPH_LOOP_STEP, GRAPH_MODEL_TURN, GRAPH_RESPONSE_READY
from ..runtime.session import SessionState
from ..tools.contracts import ToolCall, ToolResult
from .contracts import GraphEvent, GraphRunRequest


@dataclass(frozen=True, slots=True)
class ProviderStep:
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    output: str | None = None
    is_finished: bool = False
    provider_usage: ProviderTokenUsage | None = None

    def __post_init__(self) -> None:
        if self.is_finished:
            if self.tool_call is not None:
                raise ValueError("finished graph steps must not include a tool call")
            if self.output is None:
                raise ValueError("finished graph steps must include output")
            return
        if self.tool_call is None:
            raise ValueError("non-finished graph steps must include a tool call")
        if self.output is not None:
            raise ValueError("non-finished graph steps must not include output")


@dataclass(slots=True)
class _GraphAbortSignal:
    _cancelled: bool = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_cancelled(self, value: bool) -> None:
        self._cancelled = value


class ProviderGraph:
    def __init__(
        self,
        *,
        provider: TurnProvider,
        provider_model: ResolvedProviderModel,
        max_steps: int | None = None,
    ) -> None:
        if max_steps is not None and max_steps < 1:
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
    ) -> ProviderStep:
        _ = session
        current_turn = len(tool_results) + 1
        if self._max_steps is not None and current_turn > self._max_steps:
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
                    "mode": "provider",
                    "provider": self._provider.name,
                    "model": self._provider_model.selection.model,
                    "attempt": request.metadata.get("provider_attempt", 0),
                    "streaming": streaming_enabled,
                    "prompt": request.assembled_context.prompt,
                },
            ),
        )

        abort_requested = request.metadata.get("abort_requested", False)
        if isinstance(abort_requested, bool):
            should_abort = abort_requested
        else:
            should_abort = str(abort_requested).strip().lower() not in {
                "false",
                "0",
                "no",
                "off",
                "",
            }

        if request.abort_signal is None:
            self._abort_signal.set_cancelled(should_abort)
            abort_signal = cast(ProviderAbortSignal, self._abort_signal)
        else:
            abort_signal = request.abort_signal
            if should_abort and hasattr(abort_signal, "set_cancelled"):
                cast(_GraphAbortSignal, abort_signal).set_cancelled(True)
        turn_request = ProviderTurnRequest(
            assembled_context=request.assembled_context,
            bounded_context_window=request.context_window,
            available_tools=request.available_tools,
            raw_model=self._provider_model.selection.raw_model,
            provider_name=self._provider_model.selection.provider,
            model_name=self._provider_model.selection.model,
            agent_preset=cast(dict[str, object] | None, request.metadata.get("agent_preset")),
            model_metadata=self._provider_model.metadata,
            reasoning_effort=cast(str | None, request.metadata.get("reasoning_effort")),
            attempt=cast(int, request.metadata.get("provider_attempt", 0)),
            abort_signal=abort_signal,
        )

        if streaming_enabled and isinstance(self._provider, StreamableTurnProvider):
            return self._step_streaming(
                planning_events=planning_events,
                turn_request=turn_request,
                current_turn=current_turn,
            )

        turn_result = self._provider.propose_turn(turn_request)
        if turn_result.tool_call is not None:
            return ProviderStep(
                events=planning_events,
                tool_call=turn_result.tool_call,
                provider_usage=turn_result.usage,
            )

        if turn_result.output is not None:
            finalize_events = planning_events + (
                self._graph_event(
                    GRAPH_LOOP_STEP,
                    {"step": current_turn + 1, "phase": "finalize", "max_steps": self._max_steps},
                ),
                self._graph_event(GRAPH_RESPONSE_READY, {"output_preview": turn_result.output}),
            )
            return ProviderStep(
                events=finalize_events,
                output=turn_result.output,
                is_finished=True,
                provider_usage=turn_result.usage,
            )

        if turn_result.tool_call is None:
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=turn_request.model_name,
                message="provider turn produced neither output nor a tool call",
                details={
                    "source": "graph_nonstream",
                    "reason": "missing_terminal_outcome",
                },
            )

        return ProviderStep(
            events=planning_events,
            tool_call=turn_result.tool_call,
            provider_usage=turn_result.usage,
        )

    def _step_streaming(
        self,
        *,
        planning_events: tuple[GraphEvent, ...],
        turn_request: ProviderTurnRequest,
        current_turn: int,
    ) -> ProviderStep:
        stream_provider = cast(StreamableTurnProvider, cast(object, self._provider))
        stream_events: list[GraphEvent] = []
        output_parts: list[str] = []
        tool_payload_parts: list[str] = []
        done_reason: str | None = None
        provider_usage: ProviderTokenUsage | None = None

        for stream_event in stream_provider.stream_turn(turn_request):
            stream_events.append(self._stream_event_to_graph_event(stream_event))
            provider_usage = stream_event.usage or provider_usage
            if stream_event.kind in {"delta", "content"} and stream_event.channel == "text":
                if stream_event.text is not None:
                    output_parts.append(stream_event.text)
            if (
                stream_event.kind in {"delta", "content"}
                and stream_event.channel == "tool"
                and stream_event.text is not None
            ):
                tool_payload_parts.append(stream_event.text)
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
                    "missing_auth",
                    "rate_limit",
                    "context_limit",
                    "invalid_model",
                    "unsupported_feature",
                    "stream_tool_feedback_shape",
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
                done_reason = stream_event.done_reason
                break

        if done_reason is None:
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=turn_request.model_name,
                message="provider stream ended without a done event",
                details={"source": "graph_stream", "reason": "missing_done_event"},
            )
        if done_reason == "cancelled":
            raise self._provider_execution_error(
                kind="cancelled",
                model_name=turn_request.model_name,
                message="provider stream cancelled",
                details={"source": "graph_stream", "reason": "done_cancelled"},
            )
        if done_reason == "error":
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=turn_request.model_name,
                message="provider stream ended with error",
                details={"source": "graph_stream", "reason": "done_error"},
            )

        streamed_tool_call = self._parse_streamed_tool_call(
            tool_payload_parts,
            model_name=turn_request.model_name,
        )
        output = "".join(output_parts)

        if streamed_tool_call is not None:
            return ProviderStep(
                events=planning_events + tuple(stream_events),
                tool_call=streamed_tool_call,
                provider_usage=provider_usage,
            )

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
        return ProviderStep(
            events=finalize_events,
            output=output,
            is_finished=True,
            provider_usage=provider_usage,
        )

    def _parse_streamed_tool_call(
        self,
        payload_parts: list[str],
        *,
        model_name: str | None,
    ) -> ToolCall | None:
        if not payload_parts:
            return None
        raw_payload_text = payload_parts[-1]
        try:
            raw_tool_payload = json.loads(raw_payload_text)
        except json.JSONDecodeError as exc:
            raw_payload_text = "".join(payload_parts)
            try:
                raw_tool_payload = json.loads(raw_payload_text)
            except json.JSONDecodeError:
                raise self._provider_execution_error(
                    kind="transient_failure",
                    model_name=model_name,
                    message="provider stream emitted malformed tool payload",
                    details={
                        "source": "graph_stream",
                        "reason": "malformed_tool_payload",
                    },
                ) from exc

        if not isinstance(raw_tool_payload, dict):
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=model_name,
                message="provider stream tool payload must be a JSON object",
                details={
                    "source": "graph_stream",
                    "reason": "tool_payload_not_object",
                },
            )

        tool_payload = cast(dict[str, Any], raw_tool_payload)
        tool_call_id_obj = tool_payload.get("tool_call_id")
        tool_name_obj = tool_payload.get("tool_name")
        arguments_obj = tool_payload.get("arguments")
        if not isinstance(tool_name_obj, str) or not tool_name_obj.strip():
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=model_name,
                message="provider stream tool payload must include a non-empty tool_name",
                details={
                    "source": "graph_stream",
                    "reason": "missing_tool_name",
                },
            )
        if not isinstance(arguments_obj, dict):
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=model_name,
                message="provider stream tool payload must include an arguments object",
                details={
                    "source": "graph_stream",
                    "reason": "invalid_tool_arguments",
                },
            )

        return ToolCall(
            tool_name=tool_name_obj,
            arguments=cast(dict[str, object], arguments_obj),
            tool_call_id=tool_call_id_obj if isinstance(tool_call_id_obj, str) else None,
        )

    def _provider_execution_error(
        self,
        *,
        kind: Literal[
            "rate_limit",
            "context_limit",
            "invalid_model",
            "transient_failure",
            "cancelled",
        ],
        model_name: str | None,
        message: str,
        details: dict[str, object] | None = None,
    ) -> ProviderExecutionError:
        return ProviderExecutionError(
            kind=kind,
            provider_name=self._provider.name,
            model_name=model_name or "unknown",
            message=message,
            details=details,
        )

    @staticmethod
    def _stream_event_to_graph_event(stream_event: ProviderStreamEvent) -> GraphEvent:
        payload: dict[str, object] = {
            "kind": stream_event.kind,
            "channel": stream_event.channel,
        }
        if stream_event.text is not None:
            payload["text"] = stream_event.text
        if stream_event.metadata is not None:
            payload["metadata"] = stream_event.metadata
        if stream_event.error is not None:
            payload["error"] = stream_event.error
        if stream_event.error_kind is not None:
            payload["error_kind"] = stream_event.error_kind
        if stream_event.done_reason is not None:
            payload["done_reason"] = stream_event.done_reason
        if stream_event.usage is not None:
            payload["usage"] = stream_event.usage.metadata_payload()
        return GraphEvent(event_type="graph.provider_stream", source="graph", payload=payload)

    @staticmethod
    def _graph_event(event_type: str, payload: dict[str, object]) -> GraphEvent:
        return GraphEvent(event_type=event_type, source="graph", payload=payload)
