from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Final, Literal, TypedDict, cast

from ..formats.json_values import json_wire_object
from ..provider.errors import parse_provider_stream_error
from ..provider.model_catalog import static_catalog_metadata
from ..provider.models import ResolvedProviderModel
from ..provider.pricing_rules import usage_cost_usd
from ..provider.protocol import (
    ProviderAbortSignal,
    ProviderExecutionError,
    ProviderTokenUsage,
    ProviderTurnRequest,
    StreamableTurnProvider,
    TurnProvider,
)
from ..tools.contracts import ToolCall
from .transcript import ToolResultView
from .turns import (
    FinalTurn,
    LoopStepFact,
    ModelTurnFact,
    ResponseReadyFact,
    StreamFact,
    ToolCallPreviewBuilder,
    ToolTurn,
    TurnFact,
    TurnPlan,
    TurnRequest,
    TurnSession,
    TurnStreamItem,
)

_PREVIEW_ARGUMENT_MAX_CHARS = 64 * 1024

_WRITE_PREVIEW_TOOLS = frozenset({"write", "edit"})

logger = logging.getLogger(__name__)

# Only explicit terminal reasons complete non-streaming provider results.
# Streaming adapters emit ``done`` only after consuming their native terminal marker.
_TERMINAL_DONE_REASONS: Final[frozenset[str]] = frozenset({"stop", "tool_calls", "function_call", "length", "content_filter", "completed"})


class _StreamedToolCallState(TypedDict):
    """Accumulator for one normalized tool call streamed by the provider turn.

    Every key is written when the accumulator is created; ``parsed_arguments``
    is only present once the provider emits structured arguments.
    """

    tool_call_id: str
    tool_name: str | None
    ordinal: int
    fragments: list[str]
    preview_fragments: list[str]
    preview_chars: int
    ended: bool
    started: bool


class _StreamedToolCallWithArguments(_StreamedToolCallState, total=False):
    parsed_arguments: dict[str, object]


def _canonical_tool_arguments(arguments: Mapping[str, object]) -> str:
    return json.dumps(
        json_wire_object(arguments),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _log_unrecognized_finish_reason(
    *,
    source: str,
    provider_name: str,
    model_name: str | None,
    raw_finish_reason: str | None,
) -> None:
    if raw_finish_reason is not None:
        logger.debug(
            "provider %s/%s reported an unrecognized finish reason %r (source=%s)",
            provider_name,
            model_name or "unknown",
            raw_finish_reason,
            source,
        )


@dataclass(slots=True)
class _TurnAbortSignal:
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


class ProviderTurnProducer:
    def __init__(
        self,
        *,
        provider: TurnProvider,
        provider_model: ResolvedProviderModel,
    ) -> None:
        self._provider = provider
        self._provider_model = provider_model

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

    def stream_produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> Iterator[TurnStreamItem]:
        """Yield normalized live facts before the complete provider turn."""
        facts: queue.Queue[TurnFact] = queue.Queue()
        result: list[TurnPlan] = []
        errors: list[BaseException] = []

        def push(event: TurnFact) -> None:
            facts.put(event)

        def execute() -> None:
            try:
                result.append(self.produce(replace(request, fact_sink=push), tool_results, session=session))
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=execute, daemon=True)
        thread.start()
        while thread.is_alive() or not facts.empty():
            try:
                yield facts.get(timeout=0.02)
            except queue.Empty:
                continue
        thread.join()
        if errors:
            raise errors[0]
        if not result:
            raise RuntimeError("provider stream ended without a terminal turn")
        yield result[0]

    def produce(
        self,
        request: TurnRequest,
        tool_results: tuple[ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> TurnPlan:
        _ = session, tool_results
        run_step = request.run_step
        if run_step < 1:
            raise ValueError("run_step must be a positive integer")

        session_id = request.session.session_id
        streaming_enabled = request.metadata.get("provider_stream") is True

        planning_events = (
            LoopStepFact(run_step, "plan"),
            ModelTurnFact(
                run_step,
                "provider",
                request.assembled_context.prompt,
                provider=self._provider.name,
                model=self._provider_model.selection.model,
                attempt=cast(int, request.metadata.get("provider_attempt", 0)),
                streaming=streaming_enabled,
            ),
        )

        abort_signal = request.abort_signal or _TurnAbortSignal()
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
                run_step=run_step,
                fact_sink=request.fact_sink,
                tool_call_preview=request.tool_call_preview,
            )

        turn_result = self._provider.propose_turn(turn_request)
        missing_terminal_marker = turn_result.done_reason == "unknown" and not turn_result.finish_reason_reported
        if missing_terminal_marker or (turn_result.done_reason not in _TERMINAL_DONE_REASONS and turn_result.done_reason != "unknown"):
            raw_finish_reason = (turn_result.metadata or {}).get("finish_reason_raw")
            details: dict[str, object] = {
                "source": "turn_nonstream",
                "reason": "missing_terminal_event" if missing_terminal_marker else "unsupported_done_reason",
                "done_reason": turn_result.done_reason,
            }
            if isinstance(raw_finish_reason, str) and raw_finish_reason:
                details["finish_reason_raw"] = raw_finish_reason
            message = (
                "provider turn ended without a reported finish reason"
                if missing_terminal_marker
                else f"provider turn ended with unsupported finish reason: {raw_finish_reason or turn_result.done_reason}"
            )
            raise self._provider_execution_error(
                kind="provider_error",
                model_name=turn_request.model_name,
                message=message,
                details=details,
            )
        if turn_result.done_reason == "unknown":
            raw_finish_reason = (turn_result.metadata or {}).get("finish_reason_raw")
            _log_unrecognized_finish_reason(
                source="turn_nonstream",
                provider_name=self._provider.name,
                model_name=turn_request.model_name,
                raw_finish_reason=raw_finish_reason if isinstance(raw_finish_reason, str) else None,
            )
        if turn_result.tool_calls:
            calls = self._reject_duplicate_tool_call_ids(
                turn_result.tool_calls,
                model_name=turn_request.model_name,
                source="turn_nonstream",
            )
            return ToolTurn(
                facts=planning_events,
                calls=calls,
                provider_usage=self._priced_usage(turn_result.usage),
                reasoning=turn_result.reasoning,
            )
        if turn_result.output is not None:
            finalize_events = planning_events + (
                LoopStepFact(run_step + 1, "finalize"),
                ResponseReadyFact(
                    turn_result.output,
                    turn_result.done_reason,
                    turn_result.done_reason != "unknown" or turn_result.finish_reason_reported,
                ),
            )
            return FinalTurn(
                facts=finalize_events,
                output=turn_result.output,
                provider_usage=self._priced_usage(turn_result.usage),
                reasoning=turn_result.reasoning,
            )

        raise self._provider_execution_error(
            kind="provider_error",
            model_name=turn_request.model_name,
            message="provider turn produced neither output nor tool calls (output was empty)",
            details={
                "source": "turn_nonstream",
                "reason": "missing_terminal_outcome",
            },
        )

    def _build_provider_turn_request(
        self,
        *,
        request: TurnRequest,
        session_id: str,
        abort_signal: ProviderAbortSignal,
    ) -> ProviderTurnRequest:
        # Host-supplied model configuration stays opaque to the turn lifecycle;
        # these casts recover the declared optional provider parameter shapes.
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
        planning_events: tuple[TurnFact, ...],
        turn_request: ProviderTurnRequest,
        run_step: int,
        fact_sink: Callable[[TurnFact], None] | None = None,
        tool_call_preview: ToolCallPreviewBuilder | None = None,
    ) -> TurnPlan:
        stream_events: list[TurnFact] = []
        output_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_payload_parts: list[str] = []
        lifecycle_tool_calls: dict[str, _StreamedToolCallWithArguments] = {}
        lifecycle_tool_call_ids_by_ordinal: dict[int, str] = {}
        done_reason: str | None = None
        raw_finish_reason: str | None = None
        finish_reason_reported = False
        provider_usage: ProviderTokenUsage | None = None
        for stream_event in stream_provider.stream_turn(turn_request):
            preview: dict[str, object] | None = None
            preview_tool_name: str | None = stream_event.tool_name
            if stream_event.kind in {"tool_call_start", "tool_call_delta", "tool_call_end"}:
                tool_call_id = stream_event.tool_call_id
                if not isinstance(tool_call_id, str) or not tool_call_id:
                    raise self._provider_execution_error(
                        kind="provider_error",
                        model_name=turn_request.model_name,
                        message="provider stream tool lifecycle event has no call id",
                        details={"source": "core_stream", "reason": "missing_tool_call_id"},
                    )
                event_ordinal = stream_event.tool_call_ordinal
                if event_ordinal is not None and (isinstance(event_ordinal, bool) or not isinstance(event_ordinal, int) or event_ordinal < 0):
                    raise self._provider_execution_error(
                        kind="provider_error",
                        model_name=turn_request.model_name,
                        message="provider stream tool call has an invalid ordinal",
                        details={"source": "core_stream", "reason": "invalid_tool_call_ordinal"},
                    )
                state = lifecycle_tool_calls.get(tool_call_id)
                if state is None:
                    ordinal = event_ordinal if event_ordinal is not None else len(lifecycle_tool_calls)
                    previous_id = lifecycle_tool_call_ids_by_ordinal.get(ordinal)
                    if previous_id is not None and previous_id != tool_call_id:
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream assigned one tool-call ordinal to multiple identities",
                            details={"source": "core_stream", "reason": "duplicate_tool_call_id"},
                        )
                    lifecycle_tool_call_ids_by_ordinal[ordinal] = tool_call_id
                    state = cast(
                        _StreamedToolCallWithArguments,
                        {
                            "tool_call_id": tool_call_id,
                            "tool_name": stream_event.tool_name,
                            "ordinal": ordinal,
                            "fragments": [],
                            "preview_fragments": [],
                            "preview_chars": 0,
                            "ended": False,
                            "started": False,
                        },
                    )
                    lifecycle_tool_calls[tool_call_id] = state
                elif event_ordinal is not None and event_ordinal != state["ordinal"]:
                    raise self._provider_execution_error(
                        kind="provider_error",
                        model_name=turn_request.model_name,
                        message="provider stream assigned one tool-call identity to multiple ordinals",
                        details={"source": "core_stream", "reason": "conflicting_tool_call_id"},
                    )
                if stream_event.kind == "tool_call_start":
                    if state["started"] or state["ended"]:
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream repeated a tool-call start id",
                            details={"source": "core_stream", "reason": "duplicate_tool_call_id"},
                        )
                    state["started"] = True
                elif state["ended"]:
                    raise self._provider_execution_error(
                        kind="provider_error",
                        model_name=turn_request.model_name,
                        message="provider stream emitted tool-call data after its end event",
                        details={"source": "core_stream", "reason": "tool_call_after_end"},
                    )
                if stream_event.tool_name is not None:
                    if not isinstance(stream_event.tool_name, str) or not stream_event.tool_name.strip():
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream tool call has an invalid name",
                            details={"source": "core_stream", "reason": "invalid_tool_name"},
                        )
                    existing_name = state["tool_name"]
                    if existing_name is not None and existing_name != stream_event.tool_name:
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream reused a tool call id with a different name",
                            details={"source": "core_stream", "reason": "conflicting_tool_call_id"},
                        )
                    state["tool_name"] = stream_event.tool_name
                preview_tool_name = state.get("tool_name")
                fragments = state["fragments"]
                if stream_event.arguments_delta is not None:
                    if not isinstance(stream_event.arguments_delta, str):
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream tool arguments delta was not text",
                            details={"source": "core_stream", "reason": "invalid_tool_arguments"},
                        )
                    fragments.append(stream_event.arguments_delta)
                    preview_fragments = state["preview_fragments"]
                    preview_chars = state["preview_chars"]
                    if preview_chars < _PREVIEW_ARGUMENT_MAX_CHARS:
                        fragment = stream_event.arguments_delta[: _PREVIEW_ARGUMENT_MAX_CHARS - preview_chars]
                        preview_fragments.append(fragment)
                        state["preview_chars"] = preview_chars + len(fragment)
                if stream_event.parsed_arguments is not None:
                    if not isinstance(stream_event.parsed_arguments, dict):
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream tool arguments were not an object",
                            details={"source": "core_stream", "reason": "invalid_tool_arguments"},
                        )
                    previous_arguments = state.get("parsed_arguments")
                    if previous_arguments is not None:
                        try:
                            same_arguments = _canonical_tool_arguments(previous_arguments) == _canonical_tool_arguments(stream_event.parsed_arguments)
                        except (TypeError, ValueError) as exc:
                            raise self._provider_execution_error(
                                kind="provider_error",
                                model_name=turn_request.model_name,
                                message="provider stream contained invalid tool arguments",
                                details={"source": "core_stream", "reason": "invalid_tool_arguments"},
                            ) from exc
                        if not same_arguments:
                            raise self._provider_execution_error(
                                kind="provider_error",
                                model_name=turn_request.model_name,
                                message="provider stream reused a tool call id with conflicting arguments",
                                details={"source": "core_stream", "reason": "conflicting_tool_call_id"},
                            )
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
            fact = StreamFact(stream_event, diff_preview=preview, tool_name=preview_tool_name)
            if fact_sink is None:
                stream_events.append(fact)
            else:
                fact_sink(fact)
            provider_usage = stream_event.usage or provider_usage
            if stream_event.kind in {"delta", "content"} and stream_event.channel == "text":
                if stream_event.text is not None:
                    output_parts.append(stream_event.text)
            if stream_event.kind in {"delta", "content"} and stream_event.channel == "reasoning" and stream_event.text is not None:
                reasoning_parts.append(stream_event.text)
            if stream_event.kind in {"delta", "content"} and stream_event.channel == "tool" and stream_event.text is not None:
                tool_payload_parts.append(stream_event.text)
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
                terminal_metadata = stream_event.metadata or {}
                raw_token = terminal_metadata.get("finish_reason_raw")
                raw_finish_reason = raw_token if isinstance(raw_token, str) and raw_token else None
                reported = terminal_metadata.get("finish_reason_reported")
                finish_reason_reported = reported if isinstance(reported, bool) else stream_event.done_reason is not None
                break

        if done_reason is None:
            raise self._provider_execution_error(
                kind="provider_error",
                model_name=turn_request.model_name,
                message="provider stream ended without a done event",
                details={"source": "core_stream", "reason": "missing_done_event"},
            )
        if done_reason == "cancelled":
            raise self._provider_execution_error(
                kind="cancelled",
                model_name=turn_request.model_name,
                message="provider stream cancelled",
                details={"source": "core_stream", "reason": "done_cancelled"},
            )
        if done_reason == "error":
            raise self._provider_execution_error(
                kind="provider_error",
                model_name=turn_request.model_name,
                message="provider stream ended with error",
                details={"source": "core_stream", "reason": "done_error"},
            )
        if done_reason not in _TERMINAL_DONE_REASONS and done_reason != "unknown":
            details: dict[str, object] = {"source": "core_stream", "reason": "unsupported_done_reason", "done_reason": done_reason}
            if raw_finish_reason is not None:
                details["finish_reason_raw"] = raw_finish_reason
            raise self._provider_execution_error(
                kind="provider_error",
                model_name=turn_request.model_name,
                message=f"provider stream ended with unsupported finish reason: {raw_finish_reason or done_reason}",
                details=details,
            )
        if done_reason == "unknown":
            _log_unrecognized_finish_reason(
                source="core_stream",
                provider_name=stream_provider.name,
                model_name=turn_request.model_name,
                raw_finish_reason=raw_finish_reason,
            )
        if tool_payload_parts:
            streamed_tool_calls = self._parse_streamed_tool_calls(
                tool_payload_parts,
                model_name=turn_request.model_name,
            )
        else:
            lifecycle_calls: list[ToolCall] = []
            for state in sorted(lifecycle_tool_calls.values(), key=lambda item: item["ordinal"]):
                if state.get("ended") is not True:
                    raise self._provider_execution_error(
                        kind="provider_error",
                        model_name=turn_request.model_name,
                        message="provider stream ended with an incomplete tool call",
                        details={"source": "core_stream", "reason": "incomplete_tool_call"},
                    )
                tool_name = state["tool_name"]
                if not isinstance(tool_name, str) or not tool_name.strip():
                    raise self._provider_execution_error(
                        kind="provider_error",
                        model_name=turn_request.model_name,
                        message="provider stream tool call has no tool name",
                        details={"source": "core_stream", "reason": "missing_tool_name"},
                    )
                parsed_arguments = state.get("parsed_arguments")
                if state["fragments"]:
                    try:
                        fragmented_arguments = json.loads("".join(state["fragments"]))
                    except json.JSONDecodeError as exc:
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream emitted malformed tool payload",
                            details={"source": "core_stream", "reason": "malformed_tool_payload"},
                        ) from exc
                    if not isinstance(fragmented_arguments, dict):
                        raise self._provider_execution_error(
                            kind="provider_error",
                            model_name=turn_request.model_name,
                            message="provider stream tool call arguments must be a JSON object",
                            details={"source": "core_stream", "reason": "invalid_tool_arguments"},
                        )
                    if isinstance(parsed_arguments, dict):
                        try:
                            same_arguments = _canonical_tool_arguments(parsed_arguments) == _canonical_tool_arguments(fragmented_arguments)
                        except (TypeError, ValueError) as exc:
                            raise self._provider_execution_error(
                                kind="provider_error",
                                model_name=turn_request.model_name,
                                message="provider stream contained invalid tool arguments",
                                details={"source": "core_stream", "reason": "invalid_tool_arguments"},
                            ) from exc
                        if not same_arguments:
                            raise self._provider_execution_error(
                                kind="provider_error",
                                model_name=turn_request.model_name,
                                message="provider stream reused a tool call id with conflicting arguments",
                                details={"source": "core_stream", "reason": "conflicting_tool_call_id"},
                            )
                    parsed_arguments = fragmented_arguments
                if not isinstance(parsed_arguments, dict):
                    raise self._provider_execution_error(
                        kind="provider_error",
                        model_name=turn_request.model_name,
                        message="provider stream tool call arguments are missing",
                        details={"source": "core_stream", "reason": "missing_tool_arguments"},
                    )
                lifecycle_calls.append(
                    ToolCall(
                        tool_name=tool_name,
                        arguments=parsed_arguments,
                        tool_call_id=state["tool_call_id"],
                    )
                )
            streamed_tool_calls = tuple(lifecycle_calls)
        streamed_tool_calls = self._reject_duplicate_tool_call_ids(
            streamed_tool_calls,
            model_name=turn_request.model_name,
            source="core_stream",
        )
        output = "".join(output_parts)

        if not streamed_tool_calls and not output:
            raise self._provider_execution_error(
                kind="provider_error",
                model_name=turn_request.model_name,
                message="provider stream produced neither output nor tool calls",
                details={"source": "core_stream", "reason": "missing_terminal_outcome"},
            )

        if streamed_tool_calls:
            return ToolTurn(
                facts=planning_events + tuple(stream_events),
                calls=streamed_tool_calls,
                provider_usage=self._priced_usage(provider_usage),
                reasoning="".join(reasoning_parts) or None,
            )

        finalize_events = (
            planning_events
            + tuple(stream_events)
            + (
                LoopStepFact(run_step + 1, "finalize"),
                ResponseReadyFact(output, done_reason, finish_reason_reported),
            )
        )
        return FinalTurn(
            facts=finalize_events,
            output=output,
            provider_usage=self._priced_usage(provider_usage),
            reasoning="".join(reasoning_parts) or None,
        )

    def _reject_duplicate_tool_call_ids(
        self,
        calls: tuple[ToolCall, ...],
        *,
        model_name: str | None,
        source: str,
    ) -> tuple[ToolCall, ...]:
        seen: set[str] = set()
        for call in calls:
            tool_call_id = call.tool_call_id
            if tool_call_id is None:
                continue
            if tool_call_id in seen:
                raise self._provider_execution_error(
                    kind="provider_error",
                    model_name=model_name,
                    message="provider response reused a tool call id",
                    details={"source": source, "reason": "duplicate_tool_call_id"},
                )
            seen.add(tool_call_id)
        return calls

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
                    kind="provider_error",
                    model_name=model_name,
                    message="provider stream emitted malformed tool payload",
                    details={
                        "source": "core_stream",
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
                kind="provider_error",
                model_name=model_name,
                message="provider stream tool payload must be a JSON object",
                details={
                    "source": "core_stream",
                    "reason": "tool_payload_not_object",
                },
            )

        parsed_tool_calls: list[ToolCall] = []
        for payload_obj in tool_payloads:
            if not isinstance(payload_obj, dict):
                raise self._provider_execution_error(
                    kind="provider_error",
                    model_name=model_name,
                    message="provider stream tool payload entries must be JSON objects",
                    details={"source": "core_stream", "reason": "tool_payload_entry_not_object"},
                )
            tool_payload = payload_obj
            tool_call_id_obj = tool_payload.get("tool_call_id")
            tool_name_obj = tool_payload.get("tool_name")
            arguments_obj = tool_payload.get("arguments")
            if not isinstance(tool_name_obj, str) or not tool_name_obj.strip():
                raise self._provider_execution_error(
                    kind="provider_error",
                    model_name=model_name,
                    message="provider stream tool payload must include a non-empty tool_name",
                    details={
                        "source": "core_stream",
                        "reason": "missing_tool_name",
                    },
                )
            if not isinstance(arguments_obj, dict):
                raise self._provider_execution_error(
                    kind="provider_error",
                    model_name=model_name,
                    message="provider stream tool payload must include an arguments object",
                    details={
                        "source": "core_stream",
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

    def _provider_execution_error(
        self,
        *,
        kind: Literal[
            "rate_limit",
            "context_limit",
            "invalid_model",
            "not_configured",
            "provider_error",
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
            retryable=False if kind == "provider_error" else None,
            fallback_allowed=kind != "cancelled",
            details=details,
        )
