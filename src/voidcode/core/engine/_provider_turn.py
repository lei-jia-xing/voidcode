"""Provider turn streaming: aggregate events, decode tool calls, retry before first byte."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace

from ...sdk.provider import ProviderDoneReason, ProviderExecutionError, ProviderTurnRequest, ProviderTurnResult
from ...sdk.tools import ToolCall
from ..execution import _Execution
from ..retry import MAX_ATTEMPTS, _backoff_or_raise, _retry_delay_seconds, should_retry
from ._turn_contracts import TurnDriverState, TurnExecutionSnapshot


def _tool_calls_from_content(content: str, *, provider_name: str, model_name: str) -> tuple[ToolCall, ...]:
    """Decode one whole-message tool content event into turn tool calls."""
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream tool content was not JSON",
            retryable=False,
        ) from exc
    if isinstance(payload, Mapping) and isinstance(payload.get("tool_calls"), list):
        items = payload["tool_calls"]
    elif isinstance(payload, Mapping):
        items = [payload]
    else:
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name=provider_name,
            model_name=model_name,
            message="provider stream tool content was not an object",
            retryable=False,
        )
    calls: list[ToolCall] = []
    for item in items:
        if not isinstance(item, Mapping) or not isinstance(item.get("tool_name"), str) or not item["tool_name"]:
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content was malformed",
                retryable=False,
            )
        arguments = item.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content arguments were not an object",
                retryable=False,
            )
        call_id = item.get("tool_call_id")
        if call_id is not None and (not isinstance(call_id, str) or not call_id):
            raise ProviderExecutionError(
                kind="provider_error",
                provider_name=provider_name,
                model_name=model_name,
                message="provider stream tool content id was malformed",
                retryable=False,
            )
        calls.append(ToolCall(tool_name=item["tool_name"], arguments=dict(arguments), tool_call_id=call_id))
    return tuple(calls)


def _tool_call_from_entry(entry: Mapping[str, object]) -> ToolCall:
    """One aggregated lifecycle entry into its turn tool call; malformed entries fail the turn."""
    name = entry.get("tool_name")
    arguments = entry.get("arguments")
    call_id = entry.get("tool_call_id")
    if not isinstance(name, str) or not name or not isinstance(arguments, Mapping):
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name="",
            model_name="",
            message="provider stream ended with an incomplete tool call",
            retryable=False,
        )
    if call_id is not None and (not isinstance(call_id, str) or not call_id):
        raise ProviderExecutionError(
            kind="provider_error",
            provider_name="",
            model_name="",
            message="provider stream tool call id was malformed",
            retryable=False,
        )
    return ToolCall(tool_name=name, arguments=dict(arguments), tool_call_id=call_id)


class TurnStreamMixin(TurnDriverState):
    """Provider stream aggregation, decode, usage, and pre-first-byte retry."""

    def stream_provider_turn(
        self,
        snapshot: TurnExecutionSnapshot,
        execution: _Execution,
        request: ProviderTurnRequest,
        *,
        provider_name: str,
        dispatch_id: str,
        dispatch_sequence: int,
    ) -> ProviderTurnResult:
        """Aggregate one provider stream into propose_turn's turn shape, with progress.

        Pre-first-byte retries are supported when the adapter/network failure is retryable.
        Once any bytes or tokens flow, retries are forbidden to prevent duplicate outputs.
        """
        session_id = execution.session.session_id
        for attempt in range(1, MAX_ATTEMPTS + 1):
            numbered = replace(request, attempt=attempt)
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            calls: dict[int, dict[str, object]] = {}
            contents: list[str] = []
            metadata: dict[str, object] = {}
            usage = None
            done_reason: ProviderDoneReason = "unknown"
            finish_reason_reported = False
            saw_done = False
            bytes_flowed = False
            try:
                for event in snapshot.plugin_runtime.stream_turn(numbered, provider=provider_name):
                    if event.kind == "delta" and event.text:
                        bytes_flowed = True
                        if event.channel == "reasoning":
                            reasoning_parts.append(event.text)
                            self._host.report_provider_progress(
                                session_id,
                                run_id=execution.run_id,
                                dispatch_id=dispatch_id,
                                dispatch_sequence=dispatch_sequence,
                                channel="reasoning",
                                text_delta=event.text,
                            )
                        else:
                            text_parts.append(event.text)
                            self._host.report_provider_progress(
                                session_id,
                                run_id=execution.run_id,
                                dispatch_id=dispatch_id,
                                dispatch_sequence=dispatch_sequence,
                                channel="text",
                                text_delta=event.text,
                            )
                    elif event.kind == "tool_call_start":
                        bytes_flowed = True
                        ordinal = event.tool_call_ordinal if event.tool_call_ordinal is not None else len(calls)
                        calls[ordinal] = {
                            "tool_call_id": event.tool_call_id,
                            "tool_name": event.tool_name,
                            "arguments": {},
                        }
                    elif event.kind == "tool_call_end":
                        bytes_flowed = True
                        ordinal = event.tool_call_ordinal if event.tool_call_ordinal is not None else len(calls)
                        entry = calls.setdefault(ordinal, {})
                        if event.tool_call_id is not None:
                            entry["tool_call_id"] = event.tool_call_id
                        if event.tool_name is not None:
                            entry["tool_name"] = event.tool_name
                        entry["arguments"] = dict(event.parsed_arguments or {})
                    elif event.kind == "content" and event.channel == "tool" and event.text:
                        bytes_flowed = True
                        contents.append(event.text)
                    elif event.kind == "error":
                        raise ProviderExecutionError(
                            kind=event.error_kind or "provider_error",
                            provider_name=provider_name,
                            model_name=request.model_name or "unknown",
                            message=event.error or "provider stream failed",
                            retryable=event.retryable,
                            retry_after=event.retry_after,
                        )
                    elif event.kind == "done":
                        saw_done = True
                        if event.done_reason is not None:
                            done_reason = event.done_reason
                        if event.usage is not None:
                            usage = event.usage
                        if event.metadata:
                            metadata.update(event.metadata)
                            finish_reason_reported = bool(event.metadata.get("finish_reason_reported", finish_reason_reported))
                if not saw_done:
                    raise ProviderExecutionError(
                        kind="provider_error",
                        provider_name=provider_name,
                        model_name=request.model_name or "unknown",
                        message="provider stream ended without a terminal event",
                        retryable=not bytes_flowed,
                    )
                try:
                    tool_calls = tuple(_tool_call_from_entry(entry) for _, entry in sorted(calls.items()))
                except ProviderExecutionError as exc:
                    raise ProviderExecutionError(
                        kind=exc.kind,
                        provider_name=provider_name,
                        model_name=request.model_name or "unknown",
                        message=exc.message,
                        retryable=False,
                    ) from exc
                for content in contents:
                    tool_calls += _tool_calls_from_content(content, provider_name=provider_name, model_name=request.model_name or "unknown")
                return ProviderTurnResult(
                    tool_calls=tool_calls,
                    output="".join(text_parts),
                    reasoning="".join(reasoning_parts) or None,
                    usage=usage,
                    done_reason=done_reason,
                    finish_reason_reported=finish_reason_reported,
                    metadata=metadata or None,
                )
            except ProviderExecutionError as exc:
                if bytes_flowed or not should_retry(exc) or attempt >= MAX_ATTEMPTS:
                    raise
                _backoff_or_raise(
                    request.abort_signal,
                    _retry_delay_seconds(exc, attempt),
                    provider=provider_name,
                    model=request.model_name or "unknown",
                )
        raise AssertionError("unreachable: retry loop always returns or raises")
