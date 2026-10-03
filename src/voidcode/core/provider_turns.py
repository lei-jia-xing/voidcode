from __future__ import annotations

import json
import logging
import queue
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from typing import Final, Literal, TypedDict, cast

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
from ..tools.contracts import ToolCall, ToolResult
from .transcript import ToolResultView
from .turns import (
    LoopStepFact,
    ModelTurnFact,
    ResponseReadyFact,
    StreamFact,
    ToolCallPreviewBuilder,
    TurnFact,
    TurnPlan,
    TurnRequest,
    TurnSession,
    TurnStreamItem,
)

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
    """Accumulator for one normalized tool call streamed by the provider turn.

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
        tool_results: tuple[ToolResult | ToolResultView, ...],
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
        tool_results: tuple[ToolResult | ToolResultView, ...],
        *,
        session: TurnSession,
    ) -> TurnPlan:
        _ = session, tool_results
        current_turn = request.run_step
        if current_turn < 1:
            raise ValueError("run_step must be a positive integer")

        session_id = request.session.session_id
        streaming_enabled = request.metadata.get("provider_stream") is True

        planning_events = (
            LoopStepFact(current_turn, "plan"),
            ModelTurnFact(
                current_turn,
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
                current_turn=current_turn,
                fact_sink=request.fact_sink,
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
            return TurnPlan(
                facts=planning_events,
                tool_calls=turn_result.tool_calls,
                provider_usage=self._priced_usage(turn_result.usage),
                reasoning=turn_result.reasoning,
            )

        if turn_result.output is not None and turn_result.output.strip():
            finalize_events = planning_events + (
                LoopStepFact(current_turn + 1, "finalize"),
                ResponseReadyFact(
                    turn_result.output,
                    turn_result.done_reason,
                    turn_result.done_reason != "unknown" or turn_result.finish_reason_reported,
                ),
            )
            return TurnPlan(
                facts=finalize_events,
                output=turn_result.output,
                is_finished=True,
                provider_usage=self._priced_usage(turn_result.usage),
                reasoning=turn_result.reasoning,
            )

        raise self._provider_execution_error(
            kind="transient_failure",
            model_name=turn_request.model_name,
            message="provider turn produced neither output nor tool calls (output was empty)",
            details={
                "source": "graph_nonstream",
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
        current_turn: int,
        fact_sink: Callable[[TurnFact], None] | None = None,
        tool_call_preview: ToolCallPreviewBuilder | None = None,
    ) -> TurnPlan:
        stream_events: list[TurnFact] = []
        output_parts: list[str] = []
        reasoning_parts: list[str] = []
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
            return TurnPlan(
                facts=planning_events + tuple(stream_events),
                tool_calls=streamed_tool_calls,
                provider_usage=self._priced_usage(provider_usage),
                reasoning="".join(reasoning_parts) or None,
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
                LoopStepFact(current_turn + 1, "finalize"),
                ResponseReadyFact(output, done_reason, done_reason != "unknown" or raw_finish_reason is not None),
            )
        )
        return TurnPlan(
            facts=finalize_events,
            output=output,
            is_finished=True,
            provider_usage=self._priced_usage(provider_usage),
            reasoning="".join(reasoning_parts) or None,
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
