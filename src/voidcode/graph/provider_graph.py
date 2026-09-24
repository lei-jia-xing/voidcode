from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal, TypedDict, cast

from ..provider.errors import parse_provider_stream_error
from ..provider.model_catalog import static_catalog_metadata
from ..provider.models import ResolvedProviderModel
from ..provider.pricing_rules import usage_cost_usd
from ..provider.protocol import (
    ProviderAbortSignal,
    ProviderExecutionError,
    ProviderStreamEvent,
    ProviderTokenUsage,
    ProviderTurnRequest,
    StreamableTurnProvider,
    TurnProvider,
)
from ..runtime.context.window import ToolResultView
from ..tools.contracts import ToolCall, ToolResult
from .contracts import (
    GRAPH_LOOP_STEP,
    GRAPH_MODEL_TURN,
    GRAPH_PROVIDER_STREAM,
    GRAPH_RESPONSE_READY,
    GRAPH_TOOL_CALL_DELTA,
    GRAPH_TOOL_CALL_END,
    GRAPH_TOOL_CALL_START,
    GraphEvent,
    GraphRunRequest,
    GraphSession,
    GraphStreamItem,
    ToolCallPreviewBuilder,
)


def _run_id_from_graph_metadata(
    request_metadata: Mapping[str, object],
    session_metadata: Mapping[str, object],
) -> str | None:
    """Resolve run correlation without importing runtime-owned metadata helpers."""
    for metadata in (request_metadata, session_metadata):
        run_id = metadata.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    runtime_state = session_metadata.get("runtime_state")
    if isinstance(runtime_state, Mapping):
        run_id = runtime_state.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return None


_PREVIEW_ARGUMENT_MAX_CHARS = 64 * 1024

_WRITE_PREVIEW_TOOLS = frozenset({"write", "edit", "multi_edit", "apply_patch"})

logger = logging.getLogger(__name__)

# A provider turn that declared a terminal outcome without a finish reason we
# recognize — or omitted the reason entirely — is still a well-formed response:
# the upstream ended the turn, so it maps to a stop-equivalent completed state
# rather than a user-visible provider failure. The provider's own raw token stays
# on the event/turn-result metadata for debug diagnostics. Only a genuinely
# non-completed reason (``error`` / ``cancelled``) still fails the turn.
_COMPLETED_DONE_REASONS: Final[frozenset[str]] = frozenset(
    {"stop", "tool_calls", "function_call", "length", "content_filter", "completed", "unknown"}
)


class _StreamedToolCallState(TypedDict):
    """Graph-local accumulator for one tool call streamed by the provider turn.

    Every key is written when the accumulator is created; ``parsed_arguments``
    is only present once the provider emits structured arguments.
    """

    tool_call_id: str
    tool_name: str | None
    ordinal: int
    stream_order: int
    fragments: list[str]
    preview_fragments: list[str]
    preview_chars: int
    ended: bool


class _StreamedToolCallWithArguments(_StreamedToolCallState, total=False):
    parsed_arguments: dict[str, object]


def _finish_reason_diagnostics(*, done_reason: str, reported: bool) -> dict[str, object]:
    """Persisted terminal-reason diagnostics for a completed provider turn.

    ``finish_reason_reported`` is false only when the provider declared a terminal
    outcome without a reason we could read. The turn still completes (see
    ``_COMPLETED_DONE_REASONS``), but the transcript records the missing reason so
    a silently truncated stream stays visible to session inspection.
    """
    return {
        "finish_reason": done_reason,
        "finish_reason_reported": done_reason != "unknown" or reported,
    }


def _log_unrecognized_finish_reason(
    *,
    source: str,
    provider_name: str,
    model_name: str | None,
    reported: bool,
    raw_finish_reason: str | None,
) -> None:
    """Record how a terminal reason we could not map was resolved."""
    if reported and raw_finish_reason is not None:
        logger.debug(
            "provider %s/%s reported an unrecognized finish reason %r (source=%s); treating the turn as completed",
            provider_name,
            model_name or "unknown",
            raw_finish_reason,
            source,
        )
        return
    logger.warning(
        "provider %s/%s ended the turn without reporting a finish reason (source=%s); the response is treated as completed and may be truncated",
        provider_name,
        model_name or "unknown",
        source,
    )


@dataclass(frozen=True, slots=True)
class ProviderStep:
    events: tuple[GraphEvent, ...] = ()
    tool_call: ToolCall | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    output: str | None = None
    is_finished: bool = False
    provider_usage: ProviderTokenUsage | None = None
    # Non-streaming reasoning content carried from the provider turn result so
    # the run loop can persist it as runtime.reasoning_part (mirrors the
    # aggregated streamed reasoning deltas on the streaming path).
    reasoning: str | None = None

    def __post_init__(self) -> None:
        if self.tool_call is not None and not self.tool_calls:
            object.__setattr__(self, "tool_calls", (self.tool_call,))
        elif self.tool_call is None and self.tool_calls:
            object.__setattr__(self, "tool_call", self.tool_calls[0])
        if self.is_finished:
            if self.tool_calls:
                raise ValueError("finished graph steps must not include tool calls")
            if self.output is None:
                raise ValueError("finished graph steps must include output")
            return
        if not self.tool_calls:
            raise ValueError("non-finished graph steps must include at least one tool call")
        if self.output is not None:
            raise ValueError("non-finished graph steps must not include output")


@dataclass(slots=True)
class _GraphAbortSignal:
    _cancelled: bool = False
    _reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def reason(self) -> str | None:
        return self._reason

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self._cancelled = value
        if value and reason is not None:
            self._reason = reason


class ProviderGraph:
    def __init__(
        self,
        *,
        provider: TurnProvider,
        provider_model: ResolvedProviderModel,
    ) -> None:
        self._provider = provider
        self._provider_model = provider_model
        self._abort_signal = _GraphAbortSignal(_cancelled=False)
        self._pending_tool_calls: list[ToolCall] = []
        self._pending_tool_calls_session_id: str | None = None
        self._pending_tool_calls_run_id: str | None = None
        self._pending_tool_calls_min_tool_result_count: int | None = None

    def cancel_current_turn(self) -> None:
        self._abort_signal.set_cancelled(True)

    def _priced_usage(self, usage: ProviderTokenUsage | None) -> ProviderTokenUsage | None:
        """The turn's usage with its cost attached, from the catalog rates + policy tier.

        Money is computed here, once, from the usage this turn reported and the
        model the runtime resolved: a persisted usage record is never repriced.
        """
        if usage is None or usage.cost_usd is not None:
            return usage
        selection = self._provider_model.selection
        provider_name = selection.provider
        model_name = selection.model
        if not provider_name or not model_name:
            return usage
        return replace(
            usage,
            cost_usd=usage_cost_usd(
                provider_id=provider_name,
                model_id=model_name,
                usage=usage,
                # The SHIPPED catalog, deliberately: the request metadata is the
                # discovery-merged entry, and a gateway refresh that drops a row's
                # pricing would silently zero the cost. A price that moves with a
                # listing refresh is worse than a slightly stale one -- the same
                # reason the compaction budget reads the shipped row.
                metadata=static_catalog_metadata(provider_name, model_name),
            ),
        )

    def stream_step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        *,
        session: GraphSession,
    ) -> Iterator[GraphStreamItem]:
        """Native graph streaming surface; yields events before the final step."""
        events: queue.Queue[GraphEvent] = queue.Queue()
        result: list[ProviderStep] = []
        errors: list[BaseException] = []

        def push(event: GraphEvent) -> None:
            events.put(event)

        def execute() -> None:
            try:
                result.append(self.step(replace(request, stream_event_sink=push), tool_results, session=session))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        while thread.is_alive() or not events.empty():
            try:
                yield events.get(timeout=0.02)
            except queue.Empty:
                continue
        thread.join()
        if errors:
            raise errors[0]
        if not result:
            raise RuntimeError("graph stream ended without a terminal step")
        yield result[0]

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        *,
        session: GraphSession,
    ) -> ProviderStep:
        _ = session
        current_turn = request.run_step
        if current_turn < 1:
            raise ValueError("run_step must be a positive integer")

        session_id = request.session.session_id
        run_id = _run_id_from_graph_metadata(request.metadata, session.metadata)
        pending_step = self._consume_pending_tool_call(
            session_id=session_id,
            run_id=run_id,
            request_metadata=request.metadata,
            tool_result_count=len(tool_results),
        )
        if pending_step is not None:
            return pending_step
        if self._pending_tool_calls:
            self._clear_pending_tool_calls()

        # ``validate_runtime_request_metadata`` rejects a non-boolean
        # ``provider_stream`` before the runtime builds this request.
        streaming_enabled = request.metadata.get("provider_stream") is True

        planning_events = (
            self._graph_event(
                GRAPH_LOOP_STEP,
                {"step": current_turn, "phase": "plan"},
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

        abort_requested = request.metadata.get("abort_requested") is True

        if request.abort_signal is None:
            self._abort_signal.set_cancelled(abort_requested)
            abort_signal = self._abort_signal
        else:
            abort_signal = request.abort_signal
            if abort_requested:
                abort_signal.set_cancelled(True)
        turn_request = self._build_provider_turn_request(
            request=request,
            session_id=session_id,
            abort_signal=abort_signal,
        )

        if streaming_enabled and isinstance(self._provider, StreamableTurnProvider):
            return self._step_streaming(
                stream_provider=self._provider,
                planning_events=planning_events,
                turn_request=turn_request,
                current_turn=current_turn,
                session_id=session_id,
                run_id=run_id,
                stream_event_sink=request.stream_event_sink,
                tool_call_preview=request.tool_call_preview,
            )

        turn_result = self._provider.propose_turn(turn_request)
        if turn_result.done_reason not in _COMPLETED_DONE_REASONS:
            # The provider's own token survives on metadata; the mapped reason alone
            # is not diagnosable.
            raw_finish_reason = (turn_result.metadata or {}).get("finish_reason_raw")
            details: dict[str, object] = {
                "source": "graph_nonstream",
                "reason": "unsupported_done_reason",
                "done_reason": turn_result.done_reason,
            }
            if isinstance(raw_finish_reason, str) and raw_finish_reason:
                details["finish_reason_raw"] = raw_finish_reason
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=turn_request.model_name,
                message=f"provider turn ended with unsupported finish reason: {raw_finish_reason or turn_result.done_reason}",
                details=details,
            )
        if turn_result.done_reason == "unknown":
            raw_finish_reason = (turn_result.metadata or {}).get("finish_reason_raw")
            _log_unrecognized_finish_reason(
                source="graph_nonstream",
                provider_name=self._provider.name,
                model_name=turn_request.model_name,
                reported=turn_result.finish_reason_reported,
                raw_finish_reason=raw_finish_reason if isinstance(raw_finish_reason, str) else None,
            )
        if turn_result.tool_calls:
            tool_calls = list(turn_result.tool_calls)
            first_tool_call = tool_calls.pop(0)
            self._pending_tool_calls.extend(tool_calls)
            if tool_calls:
                self._pending_tool_calls_session_id = session_id
                self._pending_tool_calls_run_id = run_id
                self._pending_tool_calls_min_tool_result_count = len(tool_results) + 1
            return ProviderStep(
                events=planning_events,
                tool_call=first_tool_call,
                tool_calls=turn_result.tool_calls,
                provider_usage=self._priced_usage(turn_result.usage),
                reasoning=turn_result.reasoning,
            )

        if turn_result.output is not None and turn_result.output.strip():
            finalize_events = planning_events + (
                self._graph_event(
                    GRAPH_LOOP_STEP,
                    {"step": current_turn + 1, "phase": "finalize"},
                ),
                self._graph_event(
                    GRAPH_RESPONSE_READY,
                    {
                        "output_preview": turn_result.output,
                        **_finish_reason_diagnostics(
                            done_reason=turn_result.done_reason,
                            reported=turn_result.finish_reason_reported,
                        ),
                    },
                ),
            )
            return ProviderStep(
                events=finalize_events,
                output=turn_result.output,
                is_finished=True,
                provider_usage=self._priced_usage(turn_result.usage),
                reasoning=turn_result.reasoning,
            )

        if not turn_result.tool_calls:
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=turn_request.model_name,
                message="provider turn produced neither output nor tool calls (output was empty)",
                details={
                    "source": "graph_nonstream",
                    "reason": "missing_terminal_outcome",
                },
            )

        return ProviderStep(
            events=planning_events,
            tool_calls=turn_result.tool_calls,
            provider_usage=self._priced_usage(turn_result.usage),
        )

    @property
    def pending_tool_call_count(self) -> int:
        return len(self._pending_tool_calls)

    def is_at_safe_boundary(self) -> bool:
        return self.pending_tool_call_count == 0

    def _consume_pending_tool_call(
        self,
        *,
        session_id: str,
        run_id: str | None,
        request_metadata: dict[str, object],
        tool_result_count: int,
    ) -> ProviderStep | None:
        if not (
            self._pending_tool_calls
            and self._pending_tool_calls_session_id == session_id
            and (self._pending_tool_calls_run_id == run_id or self._is_approval_resume(request_metadata))
            and self._pending_tool_calls_min_tool_result_count is not None
            and tool_result_count >= self._pending_tool_calls_min_tool_result_count
        ):
            return None
        next_tool_call = self._pending_tool_calls.pop(0)
        if not self._pending_tool_calls:
            self._clear_pending_tool_calls()
        else:
            self._pending_tool_calls_min_tool_result_count = tool_result_count + 1
        return ProviderStep(events=(), tool_call=next_tool_call)

    def _build_provider_turn_request(
        self,
        *,
        request: GraphRunRequest,
        session_id: str,
        abort_signal: ProviderAbortSignal,
    ) -> ProviderTurnRequest:
        # Boundary: these three read the runtime-owned request metadata blob
        # (``GraphRunRequest.metadata: dict[str, object]``); the runtime
        # validated the tokens before building the request, so the cast only
        # recovers the declared shape of an untyped heterogeneous dict.
        return ProviderTurnRequest(
            assembled_context=request.assembled_context,
            bounded_context_window=request.context_window,
            available_tools=request.available_tools,
            raw_model=self._provider_model.selection.raw_model,
            provider_name=self._provider_model.selection.provider,
            model_name=self._provider_model.selection.model,
            agent_preset=cast(dict[str, object] | None, request.metadata.get("agent_preset")),
            model_metadata=self._provider_model.metadata,
            session_id=session_id,
            reasoning_effort=cast(str | None, request.metadata.get("reasoning_effort")),
            attempt=cast(int, request.metadata.get("provider_attempt", 0)),
            abort_signal=abort_signal,
        )

    def _step_streaming(
        self,
        *,
        stream_provider: StreamableTurnProvider,
        planning_events: tuple[GraphEvent, ...],
        turn_request: ProviderTurnRequest,
        current_turn: int,
        session_id: str,
        run_id: str | None,
        stream_event_sink: Callable[[GraphEvent], None] | None = None,
        tool_call_preview: ToolCallPreviewBuilder | None = None,
    ) -> ProviderStep:
        stream_events: list[GraphEvent] = []
        output_parts: list[str] = []
        tool_payload_parts: list[str] = []
        complete_tool_payload_order: int | None = None
        lifecycle_tool_calls: dict[str, _StreamedToolCallWithArguments] = {}
        done_reason: str | None = None
        raw_finish_reason: str | None = None
        provider_usage: ProviderTokenUsage | None = None
        for stream_event_index, stream_event in enumerate(stream_provider.stream_turn(turn_request)):
            preview: dict[str, object] | None = None
            preview_tool_name: str | None = stream_event.tool_name
            if stream_event.kind in {"tool_call_start", "tool_call_delta", "tool_call_end"}:
                tool_call_id = stream_event.tool_call_id
                if tool_call_id is not None:
                    state = lifecycle_tool_calls.setdefault(
                        tool_call_id,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": stream_event.tool_name,
                            "ordinal": stream_event.tool_call_ordinal if stream_event.tool_call_ordinal is not None else len(lifecycle_tool_calls),
                            "stream_order": stream_event_index,
                            "fragments": [],
                            "preview_fragments": [],
                            "preview_chars": 0,
                            "ended": False,
                        },
                    )
                    if stream_event.tool_name is not None:
                        state["tool_name"] = stream_event.tool_name
                    preview_tool_name = state.get("tool_name")
                    fragments = state["fragments"]
                    if stream_event.arguments_delta is not None:
                        fragments.append(stream_event.arguments_delta)
                        preview_fragments = state["preview_fragments"]
                        preview_chars = state["preview_chars"]
                        if preview_chars < _PREVIEW_ARGUMENT_MAX_CHARS:
                            fragment = stream_event.arguments_delta[: _PREVIEW_ARGUMENT_MAX_CHARS - preview_chars]
                            preview_fragments.append(fragment)
                            state["preview_chars"] = preview_chars + len(fragment)
                    if stream_event.parsed_arguments is not None:
                        state["parsed_arguments"] = dict(stream_event.parsed_arguments)
                    if stream_event.kind == "tool_call_end":
                        state["ended"] = True
                    callback = tool_call_preview
                    if callback is not None and preview_tool_name is not None:
                        try:
                            preview = callback(
                                preview_tool_name,
                                tuple(state["preview_fragments"]),
                                state.get("parsed_arguments"),
                            )
                        except Exception:
                            # A preview is strictly observational. A client
                            # preview failure must never abort provider execution.
                            preview = None
                    elif preview_tool_name in _WRITE_PREVIEW_TOOLS:
                        preview = {
                            "schema_version": 1,
                            "phase": "partial",
                            "live_only": True,
                            "tool": preview_tool_name,
                            "status": "degraded",
                            "bounded": True,
                            "truncated": False,
                            "reason": "preview_callback_unavailable",
                        }
            graph_event = self._stream_event_to_graph_event(
                stream_event,
                diff_preview=preview,
                redact_arguments=preview_tool_name in _WRITE_PREVIEW_TOOLS,
            )
            if stream_event_sink is None:
                stream_events.append(graph_event)
            else:
                stream_event_sink(graph_event)
            provider_usage = stream_event.usage or provider_usage
            if stream_event.kind in {"delta", "content"} and stream_event.channel == "text":
                if stream_event.text is not None:
                    output_parts.append(stream_event.text)
            if stream_event.kind in {"delta", "content"} and stream_event.channel == "tool" and stream_event.text is not None:
                tool_payload_parts.append(stream_event.text)
                complete_tool_payload_order = stream_event_index
            if stream_event.kind == "error":
                if stream_event.error_kind == "cancelled":
                    raise ProviderExecutionError(
                        kind="cancelled",
                        provider_name=stream_provider.name,
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
                        error_payload = raw_payload if isinstance(raw_payload, dict) else {"message": stream_event.error}
                else:
                    error_payload = {"message": "provider stream error"}

                parsed = parse_provider_stream_error(error_payload)
                parsed_kind = parsed.kind
                if stream_event.error_kind in {
                    "missing_auth",
                    "not_configured",
                    "rate_limit",
                    "context_limit",
                    "invalid_model",
                    "unsupported_feature",
                    "stream_tool_feedback_shape",
                }:
                    parsed_kind = stream_event.error_kind
                raise ProviderExecutionError(
                    kind=parsed_kind,
                    provider_name=stream_provider.name,
                    model_name=turn_request.model_name or "unknown",
                    message=parsed.message,
                    details=parsed.details,
                )
            if stream_event.kind == "done":
                done_reason = stream_event.done_reason
                raw_token = (stream_event.metadata or {}).get("finish_reason_raw")
                raw_finish_reason = raw_token if isinstance(raw_token, str) and raw_token else None
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
        if done_reason not in _COMPLETED_DONE_REASONS:
            details: dict[str, object] = {"source": "graph_stream", "reason": "unsupported_done_reason", "done_reason": done_reason}
            if raw_finish_reason is not None:
                details["finish_reason_raw"] = raw_finish_reason
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=turn_request.model_name,
                message=f"provider stream ended with unsupported finish reason: {raw_finish_reason or done_reason}",
                details=details,
            )
        if done_reason == "unknown":
            _log_unrecognized_finish_reason(
                source="graph_stream",
                provider_name=stream_provider.name,
                model_name=turn_request.model_name,
                reported=raw_finish_reason is not None,
                raw_finish_reason=raw_finish_reason,
            )
        ordered_tool_calls: list[tuple[tuple[int, int, int], ToolCall]] = []
        for state in sorted(lifecycle_tool_calls.values(), key=lambda item: item["ordinal"]):
            tool_name = state["tool_name"]
            if state.get("ended") is not True or not isinstance(tool_name, str):
                continue
            parsed_arguments = state.get("parsed_arguments")
            if not isinstance(parsed_arguments, dict):
                raw_arguments = "".join(state["fragments"])
                try:
                    parsed_arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    raise self._provider_execution_error(
                        kind="transient_failure",
                        model_name=turn_request.model_name,
                        message="provider stream emitted malformed tool payload",
                        details={
                            "source": "graph_stream",
                            "reason": "malformed_tool_payload",
                        },
                    ) from exc
            if not isinstance(parsed_arguments, dict):
                continue
            explicit_call = ToolCall(
                tool_name=tool_name,
                arguments=parsed_arguments,
                tool_call_id=state["tool_call_id"],
            )
            ordered_tool_calls.append(
                (
                    (state["stream_order"], 0, state["ordinal"]),
                    explicit_call,
                )
            )
        complete_tool_calls = self._parse_streamed_tool_calls(
            tool_payload_parts,
            model_name=turn_request.model_name,
        )
        complete_order = complete_tool_payload_order if complete_tool_payload_order is not None else len(stream_events)
        for complete_index, complete_call in enumerate(complete_tool_calls):
            ordered_tool_calls.append(((complete_order, 1, complete_index), complete_call))
        ordered_tool_calls.sort(key=lambda item: item[0])
        seen_tool_call_ids: set[str] = set()
        merged_tool_calls: list[ToolCall] = []
        for _order, tool_call in ordered_tool_calls:
            tool_call_id = tool_call.tool_call_id
            if tool_call_id is not None:
                if tool_call_id in seen_tool_call_ids:
                    continue
                seen_tool_call_ids.add(tool_call_id)
            merged_tool_calls.append(tool_call)
        streamed_tool_calls = tuple(merged_tool_calls)
        output = "".join(output_parts)

        if streamed_tool_calls:
            tool_calls = list(streamed_tool_calls)
            first_tool_call = tool_calls.pop(0)
            self._pending_tool_calls.extend(tool_calls)
            if tool_calls:
                self._pending_tool_calls_session_id = session_id
                self._pending_tool_calls_run_id = run_id
                self._pending_tool_calls_min_tool_result_count = current_turn
            return ProviderStep(
                events=planning_events + tuple(stream_events),
                tool_call=first_tool_call,
                tool_calls=streamed_tool_calls,
                provider_usage=self._priced_usage(provider_usage),
            )

        if not output.strip():
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=turn_request.model_name,
                message="provider stream produced neither output nor tool calls (output was empty)",
                details={
                    "source": "graph_stream",
                    "reason": "missing_terminal_outcome",
                },
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
                    },
                ),
                self._graph_event(
                    GRAPH_RESPONSE_READY,
                    {
                        "output_preview": output,
                        **_finish_reason_diagnostics(
                            done_reason=done_reason,
                            reported=raw_finish_reason is not None,
                        ),
                    },
                ),
            )
        )
        return ProviderStep(
            events=finalize_events,
            output=output,
            is_finished=True,
            provider_usage=self._priced_usage(provider_usage),
        )

    def _parse_streamed_tool_calls(
        self,
        payload_parts: list[str],
        *,
        model_name: str | None,
    ) -> tuple[ToolCall, ...]:
        if not payload_parts:
            return ()
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

        if isinstance(raw_tool_payload, list):
            tool_payloads = raw_tool_payload
        elif isinstance(raw_tool_payload, dict):
            raw_payload = raw_tool_payload
            raw_tool_calls = raw_payload.get("tool_calls")
            if isinstance(raw_tool_calls, list):
                tool_payloads = raw_tool_calls
            else:
                tool_payloads = [raw_payload]
        else:
            raise self._provider_execution_error(
                kind="transient_failure",
                model_name=model_name,
                message="provider stream tool payload must be a JSON object",
                details={
                    "source": "graph_stream",
                    "reason": "tool_payload_not_object",
                },
            )

        parsed_tool_calls: list[ToolCall] = []
        for payload_obj in tool_payloads:
            if not isinstance(payload_obj, dict):
                raise self._provider_execution_error(
                    kind="transient_failure",
                    model_name=model_name,
                    message="provider stream tool payload entries must be JSON objects",
                    details={"source": "graph_stream", "reason": "tool_payload_entry_not_object"},
                )
            tool_payload = payload_obj
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
            parsed_tool_calls.append(
                ToolCall(
                    tool_name=tool_name_obj,
                    arguments=arguments_obj,
                    tool_call_id=tool_call_id_obj if isinstance(tool_call_id_obj, str) else None,
                )
            )
        return tuple(parsed_tool_calls)

    def _clear_pending_tool_calls(self) -> None:
        self._pending_tool_calls.clear()
        self._pending_tool_calls_session_id = None
        self._pending_tool_calls_run_id = None
        self._pending_tool_calls_min_tool_result_count = None

    @staticmethod
    def _is_approval_resume(metadata: dict[str, object]) -> bool:
        return metadata.get("resume_kind") == "approval"

    def _provider_execution_error(
        self,
        *,
        kind: Literal[
            "rate_limit",
            "context_limit",
            "invalid_model",
            "not_configured",
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
    def _stream_event_to_graph_event(
        stream_event: ProviderStreamEvent,
        *,
        diff_preview: dict[str, object] | None = None,
        redact_arguments: bool = False,
    ) -> GraphEvent:
        payload: dict[str, object] = {
            "kind": stream_event.kind,
            "channel": stream_event.channel,
        }
        if stream_event.text is not None:
            payload["text"] = stream_event.text
        if stream_event.tool_call_id is not None:
            payload["tool_call_id"] = stream_event.tool_call_id
        if stream_event.tool_name is not None:
            payload["tool_name"] = stream_event.tool_name
        if stream_event.arguments_delta is not None and not redact_arguments:
            payload["arguments_delta"] = stream_event.arguments_delta
        if stream_event.tool_call_ordinal is not None:
            payload["ordinal"] = stream_event.tool_call_ordinal
        if stream_event.fragment_ordinal is not None:
            payload["fragment_ordinal"] = stream_event.fragment_ordinal
        if stream_event.parsed_arguments is not None and not redact_arguments:
            payload["parsed_arguments"] = dict(stream_event.parsed_arguments)
        if diff_preview is not None:
            payload["diff_preview"] = diff_preview
        if stream_event.metadata is not None:
            payload["metadata"] = stream_event.metadata
        if stream_event.error is not None:
            payload["error"] = stream_event.error
        if stream_event.error_kind is not None:
            payload["diagnostics"] = {
                "kind": stream_event.error_kind,
                "summary": stream_event.error,
                "details": dict(stream_event.metadata or {}),
            }
        if stream_event.done_reason is not None:
            payload["done_reason"] = stream_event.done_reason
        if stream_event.usage is not None:
            payload["usage"] = stream_event.usage.metadata_payload()
        lifecycle_event_types = {
            "tool_call_start": GRAPH_TOOL_CALL_START,
            "tool_call_delta": GRAPH_TOOL_CALL_DELTA,
            "tool_call_end": GRAPH_TOOL_CALL_END,
        }
        event_type = lifecycle_event_types.get(stream_event.kind, GRAPH_PROVIDER_STREAM)
        return GraphEvent(event_type=event_type, payload=payload)

    @staticmethod
    def _graph_event(event_type: str, payload: dict[str, object]) -> GraphEvent:
        return GraphEvent(event_type=event_type, source="graph", payload=payload)
