from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast
from uuid import uuid4

from pydantic import ValidationError

from ..graph.contracts import GraphEvent, GraphRunRequest, RuntimeGraph
from ..hook.config import RuntimeHookSurface
from ..provider.errors import (
    ProviderExecutionError,
    SingleAgentContextLimitError,
    classify_provider_error,
)
from ..provider.protocol import (
    ProviderAbortSignal,
    ProviderAssembledContext,
)
from ..tools._pydantic_args import format_validation_error
from ..tools._repair import ToolDiagnosticError
from ..tools.contracts import (
    RuntimeToolTimeoutError,
    ToolCall,
    ToolDefinition,
    ToolErrorDetails,
    ToolResult,
)
from ..tools.guards import read_tracking_for_tool_results
from ..tools.invoke_tool import InvokeToolArgs
from ..tools.output import (
    cap_tool_result_output,
    sanitize_tool_arguments,
    sanitize_tool_result_data,
)
from ..tools.question import QuestionTool
from .config import RuntimeConfig
from .config_materializer import EffectiveRuntimeConfig
from .context_window import (
    ContextProjection,
    RuntimeContextSegment,
    RuntimeContextWindow,
    continuity_summary_metadata,
)
from .contracts import RuntimeProviderContextPolicyDecision, RuntimeStreamChunk
from .event_envelopes import (
    ReasoningCaptureState,
    envelopes_for_acp_events,
    envelopes_for_lsp_events,
    envelopes_for_mcp_events,
    renumber_events,
)
from .events import (
    REASONING_PERSISTED_LIMIT_CHARS,
    RUNTIME_CONTEXT_COMPACTED,
    RUNTIME_CONTEXT_TRANSFORM_APPLIED,
    RUNTIME_PROVIDER_FALLBACK,
    RUNTIME_PROVIDER_TRANSIENT_RETRY,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_REASONING_PART,
    RUNTIME_SKILL_LOADED,
    RUNTIME_TODO_UPDATED,
    RUNTIME_TOOL_PROGRESS,
    RUNTIME_TOOL_STARTED,
    EventEnvelope,
    EventSource,
    runtime_reasoning_part_from_provider_stream,
    runtime_reasoning_part_payload,
)
from .execution_seams import (
    RuntimeGraphSelection,
    fallback_graph_for_provider_error,
    select_graph_for_effective_config,
)
from .hook_runtime import (
    HOOK_RECURSION_ENV_VAR,
    hook_execution_policy_from_metadata,
    run_lifecycle_hooks_for_session,
    run_tool_hooks_for_session,
)
from .permission import PendingApproval, PermissionPolicy, PermissionResolution
from .provider_execution_metadata import (
    provider_attempt_from_metadata,
    provider_retry_attempt_from_metadata,
    run_id_from_session_metadata,
    session_with_provider_usage_metadata,
)
from .provider_fallback import (
    ProviderFallbackDecision,
    ProviderTerminalDecision,
    ProviderTransientRetryDecision,
    decide_provider_error_policy,
    provider_transient_retry_config,
)
from .question import PendingQuestion
from .session import SessionState, SessionStatus
from .session_metadata_helpers import (
    clear_tool_execution_intent,
    delegation_depth_from_metadata,
    persist_tool_execution_intent,
    remaining_spawn_budget_from_metadata,
    runtime_state_context_compacted,
    runtime_state_context_transform_applied,
    runtime_state_run_id,
    session_model_identity,
    session_with_context_compacted_state,
    session_with_context_transform_applied_state,
    session_with_context_window_metadata,
    session_with_context_window_payload_metadata,
    session_with_current_acp_metadata,
    session_with_plan_state,
    session_with_todo_state,
    todo_state_matches_payload,
)
from .storage import SessionStore
from .tool_display import build_tool_display, build_tool_status
from .tool_execution import RuntimeToolExecutor
from .tool_replay import ToolExecutionIntent
from .tool_scope import tool_policy_error

if TYPE_CHECKING:
    from .acp import AcpAdapter
    from .lsp import LspManager
    from .mcp import McpManager
    from .provider_catalog_query import RuntimeProviderCatalogQuery
    from .runtime_surface import RuntimeSurface
    from .tool_registry import ToolRegistry

from . import chunk_builders

logger = logging.getLogger(__name__)

_STUCK_DETECTED_MIN_TURN = 25
_STUCK_DETECTED_MIN_TOOL_RESULTS = 10


def _tool_error_content(tool_name: str, error: str) -> str:
    return f"{tool_name} failed: {error}. Please correct the tool arguments and retry."


def _reasoning_output_diagnostic(
    runtime: RuntimeSurface,
    provider_catalog_query: RuntimeProviderCatalogQuery,
    *,
    session: SessionState,
    capture_state: ReasoningCaptureState,
) -> dict[str, object] | None:
    if capture_state.output_diagnostic_emitted or not capture_state.stream_observed:
        return None
    capture_state.output_diagnostic_emitted = True
    effective_config = runtime.effective_runtime_config_from_metadata(session.metadata)
    if effective_config.execution_engine != "provider":
        return None
    active_target = effective_config.resolved_provider.active_target.selection
    provider_name = active_target.provider
    model_name = active_target.model
    metadata = provider_catalog_query.metadata_for_model(provider_name, model_name) if provider_name is not None and model_name is not None else None
    supports_reasoning = metadata.supports_reasoning if metadata is not None else None
    if capture_state.reasoning_observed:
        severity = "info"
        reason = "reasoning_output_observed"
    elif supports_reasoning is True:
        severity = "warning"
        reason = "reasoning_capable_model_returned_no_reasoning_output"
    else:
        severity = "info"
        reason = "no_reasoning_output_observed"
    return {
        "severity": severity,
        "category": "reasoning_output",
        "reason": reason,
        "provider": provider_name,
        "model": model_name,
        "reasoning_output_observed": capture_state.reasoning_observed,
        "supports_reasoning": supports_reasoning,
        "captured_part_count": capture_state.part_count,
        "captured_text_char_count": capture_state.text_char_count,
    }


def _tool_completed_identity_payload(session: SessionState) -> dict[str, str]:
    """Additive model/provider identity for ``runtime.tool_completed`` payloads.

    Merged into the payload before the existing keys so it never overrides
    result data; omitted entirely when the session metadata does not carry a
    model/provider.
    """
    model, provider = session_model_identity(session.metadata)
    identity: dict[str, str] = {}
    if model is not None:
        identity["model"] = model
    if provider is not None:
        identity["provider"] = provider
    return identity


def _normalized_tool_result(
    *,
    tool_result: ToolResult,
    session: SessionState,
    plan_tool_call: Any,
    sequence: int,
    tool_call_id: str,
) -> tuple[ToolResult, bool, dict[str, object]]:
    """Deduplicate todo writes and cap/sanitize a tool result before delivery.

    Pure projection of the tool pipeline's result-normalization chain
    (``todo_state_matches_payload`` -> ``cap_tool_result_output`` ->
    ``sanitize_tool_result_data``).
    """
    runtime_tool_result_data = dict(tool_result.data)
    duplicate_todo_write = (
        plan_tool_call.tool_name == "todo_write"
        and tool_result.status == "ok"
        and todo_state_matches_payload(
            session,
            raw_todos=runtime_tool_result_data.get("todos"),
            revision=sequence + 1,
        )
    )
    if duplicate_todo_write:
        tool_result = replace(
            tool_result,
            content="Updated 0 todos (unchanged)",
            data={**tool_result.data, "unchanged": True},
        )
        runtime_tool_result_data["unchanged"] = True
    tool_result = cap_tool_result_output(
        tool_result,
        session_id=session.session.id,
        tool_call_id=tool_call_id,
    )
    tool_result = replace(
        tool_result,
        data=sanitize_tool_result_data(tool_result.data),
    )
    return tool_result, duplicate_todo_write, runtime_tool_result_data


def _tool_completed_payload(
    *,
    session: SessionState,
    tool_result: ToolResult,
    tool_call_id: str,
    sanitized_arguments: dict[str, object],
) -> dict[str, object]:
    """Assemble the ``runtime.tool_completed`` payload for a delivered result."""
    completed_payload: dict[str, object] = {
        **_tool_completed_identity_payload(session),
        **tool_result.data,
        "tool_call_id": tool_call_id,
        "arguments": sanitized_arguments,
        "status": tool_result.status,
        "content": tool_result.content,
        "error": tool_result.error,
    }
    if tool_result.error_kind is not None:
        completed_payload["error_kind"] = tool_result.error_kind
    if tool_result.error_summary is not None:
        completed_payload["error_summary"] = tool_result.error_summary
    if tool_result.error_details is not None:
        completed_payload["error_details"] = tool_result.error_details
    if tool_result.retry_guidance is not None:
        completed_payload["retry_guidance"] = tool_result.retry_guidance
    completed_payload.setdefault("tool", tool_result.tool_name)

    completed_display = build_tool_display(
        tool_result.tool_name,
        sanitized_arguments,
        result_data=tool_result.data,
    )
    completed_status = build_tool_status(
        tool_result.tool_name,
        tool_call_id,
        phase="completed" if tool_result.status == "ok" else "failed",
        status="completed" if tool_result.status == "ok" else "failed",
        display=completed_display,
    )
    completed_payload["display"] = completed_display
    completed_payload["tool_status"] = completed_status
    return completed_payload


def _serialized_tool_results(tool_results: list[ToolResult]) -> tuple[dict[str, object], ...]:
    """Serialize in-flight tool results into the interrupted-checkpoint shape.

    Mirrors ``SqliteSessionStore._tool_results_from_events`` so the persisted
    checkpoint is accepted verbatim by ``tool_results_from_checkpoint`` in
    ``resume.py``: identity keys ``tool_name``/``status``/``data``/``content``/
    ``error`` plus, only when errored, the optional error detail fields.
    """
    serialized: list[dict[str, object]] = []
    for result in tool_results:
        is_err = result.status == "error"
        entry: dict[str, object] = {
            "tool_name": result.tool_name,
            "content": result.content if result.content is not None and not is_err else None,
            "status": "error" if is_err else "ok",
            "data": dict(result.data),
            "error": result.error if result.error is not None and is_err else None,
        }
        if is_err:
            if result.error_kind is not None:
                entry["error_kind"] = result.error_kind
            if result.error_summary is not None:
                entry["error_summary"] = result.error_summary
            if result.error_details is not None:
                entry["error_details"] = dict(result.error_details)
            if result.retry_guidance is not None:
                entry["retry_guidance"] = result.retry_guidance
        serialized.append(entry)
    return tuple(serialized)


def _context_transform_applied_payloads(
    *,
    context_metadata: Mapping[str, object],
    tool_result_count: int,
) -> tuple[tuple[str, dict[str, object]], ...]:
    raw_transforms = context_metadata.get("context_transforms")
    if not isinstance(raw_transforms, Mapping):
        return ()
    transforms = cast(Mapping[str, object], raw_transforms)
    raw_applied = transforms.get("applied")
    if not isinstance(raw_applied, list):
        return ()
    raw_failure_policy = transforms.get("failure_policy")
    failure_policy = raw_failure_policy if isinstance(raw_failure_policy, str) else "warn"
    payloads: list[tuple[str, dict[str, object]]] = []
    for raw_trace in raw_applied:
        if not isinstance(raw_trace, Mapping):
            continue
        trace = cast(Mapping[str, object], raw_trace)
        provider_id = trace.get("provider_id")
        if provider_id == "hook_preset_guidance":
            continue
        if not isinstance(provider_id, str) or not provider_id:
            continue
        payload: dict[str, object] = {
            "provider_id": provider_id,
            "failure_policy": failure_policy,
            "tool_result_count": tool_result_count,
        }
        for key in (
            "status",
            "priority",
            "execution_index",
            "injection_count",
            "provider_order",
            "sources",
            "diagnostics",
        ):
            value = trace.get(key)
            if value is not None:
                payload[key] = value
        fingerprint_payload = {key: value for key, value in payload.items() if key != "tool_result_count"}
        payloads.append((json.dumps(fingerprint_payload, sort_keys=True), payload))
    return tuple(payloads)


def _unseen_context_transform_payloads(
    *,
    session: SessionState,
    payloads: tuple[tuple[str, dict[str, object]], ...],
) -> tuple[tuple[str, dict[str, object]], ...]:
    current_run_id = runtime_state_run_id(session.metadata)
    transform_state = runtime_state_context_transform_applied(session.metadata) or {}
    last_run_id_raw = transform_state.get("last_emitted_run_id")
    last_run_id = last_run_id_raw if isinstance(last_run_id_raw, str) else None
    emitted_fingerprints: set[str] = set()
    if current_run_id is None or last_run_id == current_run_id:
        raw_fingerprints = transform_state.get("last_emitted_fingerprints")
        if isinstance(raw_fingerprints, list):
            emitted_fingerprints = {item for item in raw_fingerprints if isinstance(item, str) and item.strip()}
    return tuple((fingerprint, payload) for fingerprint, payload in payloads if fingerprint not in emitted_fingerprints)


def _tool_error_summary(error: str) -> str:
    cleaned = error.removeprefix("Error: ").strip()
    return cleaned or error


def _tool_error_retry_guidance(error: str) -> str | None:
    lowered = error.lower()
    if "validation error:" in lowered:
        return "Retry with corrected arguments that satisfy the tool schema."
    if "permission denied" in lowered:
        return "Adjust the request or approval settings, then retry."
    if "timed out" in lowered or "timeout" in lowered:
        return "Reduce the command scope, increase the timeout, or retry."
    return None


def _tool_error_details(
    *,
    tool_name: str,
    error: str,
    error_kind: str | None = None,
    extra: dict[str, object] | None = None,
) -> ToolErrorDetails:
    details: ToolErrorDetails = {
        "tool_name": tool_name,
        "message": error,
        "summary": _tool_error_summary(error),
    }
    if error_kind is not None:
        details["error_kind"] = error_kind
    if extra:
        details.update(extra)
    return details


def _tool_error_payload(
    *,
    tool_name: str,
    error: str,
    error_kind: str | None = None,
    extra_details: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error": error,
        "error_summary": _tool_error_summary(error),
        "error_details": _tool_error_details(
            tool_name=tool_name,
            error=error,
            error_kind=error_kind,
            extra=extra_details,
        ),
    }
    if error_kind is not None:
        payload["error_kind"] = error_kind
    retry_guidance = _tool_error_retry_guidance(error)
    if retry_guidance is not None:
        payload["retry_guidance"] = retry_guidance
    return payload


def _tool_diagnostic_payload(
    *,
    tool_name: str,
    error: ToolDiagnosticError,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "error_kind": error.error_kind,
        "error_summary": _tool_error_summary(str(error)),
        "error_details": _tool_error_details(
            tool_name=tool_name,
            error=str(error),
            error_kind=error.error_kind,
            extra=error.error_details,
        ),
    }
    if error.retry_guidance is not None:
        payload["retry_guidance"] = error.retry_guidance
    return payload


def _metadata_without_provider_attempt(metadata: Mapping[str, object]) -> dict[str, object]:
    clean_metadata = dict(metadata)
    clean_metadata.pop("provider_attempt", None)
    return clean_metadata


def _session_without_provider_attempt(session: SessionState) -> SessionState:
    return SessionState(
        session=session.session,
        status=session.status,
        turn=session.turn,
        metadata=_metadata_without_provider_attempt(session.metadata),
    )


def _finalized_step_session(
    *,
    session: SessionState,
    graph_step: Any,
    is_final_step: bool,
    provider_attempt: int,
) -> tuple[SessionState, int, SessionStatus]:
    """Final-step metadata resets and terminal-status derivation.

    Pure projection of the finalize chain: attach provider usage metadata,
    reset the provider retry/attempt cursors, and derive the terminal status
    (keep-alive turns park ``interrupted``; one-shot children ``completed``).
    """
    session = session_with_provider_usage_metadata(
        session,
        getattr(graph_step, "provider_usage", None),
    )
    if provider_retry_attempt_from_metadata(session.metadata) != 0:
        session = SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={**session.metadata, "provider_retry_attempt": 0},
        )
    if is_final_step and provider_attempt != 0:
        provider_attempt = 0
        session = _session_without_provider_attempt(session)
    final_step_status = "interrupted" if session.metadata.get("keep_alive_turn") is True else "completed"
    return session, provider_attempt, final_step_status


def _replayed_conversation_segments(
    request: GraphRunRequest,
) -> tuple[RuntimeContextSegment, ...]:
    assembled_context = request.assembled_context
    segments = getattr(assembled_context, "segments", ())
    replayed: list[RuntimeContextSegment] = []
    for segment in segments:
        metadata = getattr(segment, "metadata", None)
        if not isinstance(metadata, dict):
            continue
        if metadata.get("source") != "replayed_conversation":
            continue
        content = getattr(segment, "content", None)
        if content is not None and not isinstance(content, str):
            continue
        role = getattr(segment, "role", None)
        if role not in {"system", "user", "assistant", "tool"}:
            continue
        replayed.append(
            RuntimeContextSegment(
                role=role,
                content=content,
                tool_call_id=getattr(segment, "tool_call_id", None),
                tool_name=getattr(segment, "tool_name", None),
                tool_arguments=getattr(segment, "tool_arguments", None),
                metadata=dict(cast(dict[str, object], metadata)),
            )
        )
    return tuple(replayed)


def _graph_request_without_provider_attempt(
    request: GraphRunRequest,
    *,
    session: SessionState,
) -> GraphRunRequest:
    return GraphRunRequest(
        session=session,
        prompt=request.prompt,
        available_tools=request.available_tools,
        context_window=request.context_window,
        assembled_context=request.assembled_context,
        metadata=_metadata_without_provider_attempt(request.metadata),
        abort_signal=request.abort_signal,
    )


def _provider_attempt_reset_after_tool_result(
    *,
    provider_attempt: int,
    selection: RuntimeGraphSelection | None,
    graph_request: GraphRunRequest,
    session: SessionState,
) -> _ProviderAttemptReset | None:
    if provider_attempt == 0:
        return None
    if selection is None:
        return None
    clean_session = _session_without_provider_attempt(session)
    clean_request = _graph_request_without_provider_attempt(
        graph_request,
        session=clean_session,
    )
    return _ProviderAttemptReset(
        provider_attempt=selection.provider_attempt,
        graph=selection.graph,
        graph_request=clean_request,
        session=clean_session,
    )


@dataclass(frozen=True, slots=True)
class _ProviderAttemptReset:
    provider_attempt: int
    graph: RuntimeGraph
    graph_request: GraphRunRequest
    session: SessionState


def _is_tool_timeout_like_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).lower()
    return "timeout" in message or "timed out" in message


def _is_abort_requested(request: GraphRunRequest) -> bool:
    return bool(request.abort_signal is not None and request.abort_signal.cancelled)


def _is_abort_signal_requested(abort_signal: ProviderAbortSignal | None) -> bool:
    return bool(abort_signal is not None and abort_signal.cancelled)


def _abort_signal_reason(abort_signal: ProviderAbortSignal | None) -> str | None:
    reason = getattr(abort_signal, "reason", None)
    return reason if isinstance(reason, str) and reason else None


def _abort_reason(request: GraphRunRequest) -> str | None:
    return _abort_signal_reason(request.abort_signal)


class _ProviderErrorPolicyVerdict(TypedDict):
    action: Literal["exit", "reraise", "retry", "fallback"]
    exc: NotRequired[BaseException]
    provider_attempt: NotRequired[int]
    provider_retry_attempt: NotRequired[int]
    graph: NotRequired[RuntimeGraph]
    session: NotRequired[SessionState]
    graph_request: NotRequired[GraphRunRequest]


class RuntimeRunLoopCoordinator:
    def __init__(
        self,
        surface: RuntimeSurface,
        *,
        session_store: SessionStore,
        workspace: Path,
        config: RuntimeConfig,
        permission_policy: PermissionPolicy,
        acp_adapter: AcpAdapter,
        mcp_manager: McpManager,
        lsp_manager: LspManager,
        provider_catalog_query: RuntimeProviderCatalogQuery,
        tool_executor: RuntimeToolExecutor,
    ) -> None:
        self._surface = surface
        self._session_store = session_store
        self._workspace = workspace
        self._config = config
        self._permission_policy = permission_policy
        self._acp_adapter = acp_adapter
        self._mcp_manager = mcp_manager
        self._lsp_manager = lsp_manager
        self._provider_catalog_query = provider_catalog_query
        self._tool_executor = tool_executor

    def _persist_events(
        self,
        *,
        session_id: str,
        events: tuple[tuple[str, EventSource, dict[str, object], str | None], ...],
    ) -> tuple[EventEnvelope, ...]:
        return self._session_store.append_session_events(
            workspace=self._workspace,
            session_id=session_id,
            events=events,
        )

    def _persist_event(
        self,
        *,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope:
        return self._persist_events(
            session_id=session_id,
            events=((event_type, source, payload, dedupe_key),),
        )[0]

    def _persist_chunk(self, chunk: RuntimeStreamChunk) -> tuple[RuntimeStreamChunk, int]:
        event = chunk.event
        if event is None:
            return chunk, 0
        envelope = self._persist_event(
            session_id=event.session_id,
            event_type=event.event_type,
            source=event.source,
            payload=event.payload,
        )
        return RuntimeStreamChunk(kind="event", session=chunk.session, event=envelope), envelope.sequence

    def _persist_chunks(
        self,
        chunks: tuple[RuntimeStreamChunk, ...],
        *,
        fallback_sequence: int,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        sequence = fallback_sequence
        for chunk in chunks:
            if chunk.event is None:
                yield chunk
                continue
            envelope = self._persist_event(
                session_id=chunk.event.session_id,
                event_type=chunk.event.event_type,
                source=chunk.event.source,
                payload=chunk.event.payload,
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=chunk.session, event=envelope)
        return sequence

    def _capture_interrupted_checkpoint(
        self,
        *,
        session: SessionState,
        prompt: str,
        tool_results: list[ToolResult],
        last_event_sequence: int,
    ) -> None:
        self._session_store.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=session.session.id,
            prompt=prompt,
            session_metadata=session.metadata,
            tool_results=_serialized_tool_results(tool_results),
            last_event_sequence=last_event_sequence,
            output=None,
            create_if_missing=False,
            parent_session_id=session.session.parent_id,
        )

    def _started_tool_abort_chunks(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_call: ToolCall,
        tool_call_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> tuple[RuntimeStreamChunk, RuntimeStreamChunk]:
        sanitized_args = sanitize_tool_arguments(dict(tool_call.arguments))
        failed_display = build_tool_display(tool_call.tool_name, sanitized_args)
        failed_status = build_tool_status(
            tool_call.tool_name,
            tool_call_id,
            phase="failed",
            status="failed",
            display=failed_display,
        )
        completed_chunk, _ = self._persist_chunk(
            RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence + 1,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        **_tool_completed_identity_payload(session),
                        "tool": tool_call.tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": sanitized_args,
                        "status": "error",
                        "error": "run interrupted",
                        "display": failed_display,
                        "tool_status": failed_status,
                    },
                ),
            )
        )
        failed_chunk, _ = self._persist_chunk(
            chunk_builders.failed_chunk(
                session=session,
                sequence=sequence + 2,
                error="run interrupted",
                payload=chunk_builders.user_interrupted_payload(
                    run_id=run_id_from_session_metadata(session.metadata),
                    reason=_abort_signal_reason(abort_signal),
                ),
                status="interrupted",
            )
        )
        return completed_chunk, failed_chunk

    def _invoke_tool(
        self,
        *,
        tool: Any,
        tool_call: ToolCall,
        read_paths: frozenset[str],
        read_lines: Mapping[str, frozenset[int]],
        tool_timeout: int | None,
        session: SessionState,
        start_sequence: int,
        tool_call_id: str,
        abort_signal: ProviderAbortSignal | None,
        parent_session_id: str | None,
        delegation_depth: int,
        remaining_spawn_budget: int | None,
        model: str | None = None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolResult | Exception, int]]:
        sequence = start_sequence - 1
        execution = self._tool_executor.invoke(
            tool=tool,
            tool_call=tool_call,
            read_paths=read_paths,
            read_lines=read_lines,
            tool_timeout=tool_timeout,
            session_id=session.session.id,
            parent_session_id=parent_session_id,
            delegation_depth=delegation_depth,
            remaining_spawn_budget=remaining_spawn_budget,
            abort_signal=abort_signal,
            model=model,
        )
        while True:
            try:
                progress = next(execution)
            except StopIteration as completed:
                return completed.value, sequence
            envelope = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_TOOL_PROGRESS,
                source="tool",
                payload={"tool_call_id": tool_call_id, **progress.payload},
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

    def execute_approved_tool_call(
        self,
        *,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        tool_call: ToolCall,
        pending: PendingApproval,
        decision: PermissionResolution,
        tool_results: list[ToolResult],
        abort_signal: ProviderAbortSignal | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        runtime = self._surface
        permission_chunks = runtime.approval_resolution_outcome(
            session=session,
            pending=pending,
            decision=decision,
            sequence=sequence + 1,
        )
        if permission_chunks.chunks:
            session = permission_chunks.chunks[-1].session
        sequence = yield from self._persist_chunks(
            permission_chunks.chunks,
            fallback_sequence=permission_chunks.last_sequence,
        )
        if permission_chunks.denied:
            yield from self._permission_denied_tool_feedback_chunks(
                session=session,
                tool_call=tool_call,
                pending=permission_chunks.denied_approval or pending,
                tool_results=tool_results,
            )
            return

        tool_policy_denial = runtime.tool_policy_denial(
            session=session,
            tool_name=tool_call.tool_name,
        )
        if tool_policy_denial is not None:
            policy_error_message = tool_policy_error(tool_policy_denial)
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=policy_error_message,
                    payload={
                        "kind": "runtime_tool_policy_denied",
                        "tool": tool_call.tool_name,
                        "tool_policy": tool_policy_denial.metadata(),
                    },
                )
            )
            yield failed_chunk
            raise ValueError(policy_error_message)
        try:
            tool = tool_registry.resolve(tool_call.tool_name)
        except Exception as exc:
            failed_chunk, _ = self._persist_chunk(chunk_builders.failed_chunk(session=session, sequence=sequence + 1, error=str(exc)))
            yield failed_chunk
            raise

        pre_hook_outcome = run_tool_hooks_for_session(
            hooks=self._config.hooks,
            workspace=self._workspace,
            session=session,
            sequence=sequence,
            tool_name=tool_call.tool_name,
            phase="pre",
            recursion_env_var=HOOK_RECURSION_ENV_VAR,
            policy=hook_execution_policy_from_metadata(session.metadata),
        )
        sequence = yield from self._persist_chunks(
            pre_hook_outcome.chunks,
            fallback_sequence=pre_hook_outcome.last_sequence,
        )
        if pre_hook_outcome.failed_error is not None:
            failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                session=session,
                sequence=sequence,
                surface="pre_tool",
                error=pre_hook_outcome.failed_error,
                hooks=self._config.hooks,
            )
            if failed_chunk is not None:
                persisted_failed, _ = self._persist_chunk(failed_chunk)
                yield persisted_failed
                raise RuntimeError(pre_hook_outcome.failed_error)
        if pre_hook_outcome.action == "cancel":
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run cancelled by pre-tool hook",
                    payload={"kind": "hook_cancelled", "surface": "pre_tool"},
                )
            )
            yield failed_chunk
            return

        tool_timeout = runtime.effective_runtime_config_from_metadata(session.metadata).tool_timeout_seconds
        explicit_tool_call_id = tool_call.tool_call_id
        tool_call_id = explicit_tool_call_id or f"runtime-tool-{uuid4().hex}"
        start_args = dict(tool_call.arguments)
        started_display = build_tool_display(tool_call.tool_name, start_args)
        execution_intent = ToolExecutionIntent.from_call(
            tool_call,
            tool.definition,
            tool_call_id=tool_call_id,
        )
        persist_tool_execution_intent(self._session_store, self._workspace, session, execution_intent.metadata_payload())
        started_status = build_tool_status(
            tool_call.tool_name,
            tool_call_id,
            phase="running",
            status="running",
            display=started_display,
        )
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_TOOL_STARTED,
            source="runtime",
            payload={
                "tool": tool_call.tool_name,
                "tool_call_id": tool_call_id,
                "execution_intent": execution_intent.metadata_payload(),
                "display": started_display,
                "tool_status": started_status,
            },
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        if _is_abort_signal_requested(abort_signal):
            yield from self._started_tool_abort_chunks(
                session=session,
                sequence=sequence,
                tool_call=tool_call,
                tool_call_id=tool_call_id,
                abort_signal=abort_signal,
            )
            return

        tool_exception_recovery_enabled = runtime.effective_runtime_config_from_metadata(session.metadata).execution_engine == "provider"
        try:
            read_tracking = read_tracking_for_tool_results(
                tool_results=tuple(tool_results),
                workspace=self._workspace,
            )
            tool_outcome, sequence = yield from self._invoke_tool(
                tool=tool,
                tool_call=tool_call,
                read_paths=read_tracking.read_paths,
                read_lines=read_tracking.read_lines,
                tool_timeout=tool_timeout,
                session=session,
                start_sequence=sequence + 1,
                tool_call_id=tool_call_id,
                abort_signal=abort_signal,
                parent_session_id=session.session.parent_id,
                delegation_depth=delegation_depth_from_metadata(session.metadata),
                remaining_spawn_budget=remaining_spawn_budget_from_metadata(session.metadata),
                model=session_model_identity(session.metadata)[0],
            )
            if isinstance(tool_outcome, Exception):
                raise tool_outcome
            tool_result = tool_outcome
        except Exception as exc:
            drained_chunks, session, sequence = self._drain_runtime_events(
                session=session,
                start_sequence=sequence + 1,
            )
            yield from drained_chunks
            if isinstance(exc, RuntimeToolTimeoutError):
                partial_timeout_payload: dict[str, object] = {}
                partial_timeout_content: str | None = None
                partial_timeout_error: str | None = None
                partial_result = getattr(exc, "partial_result", None)
                if isinstance(partial_result, ToolResult):
                    capped_partial = cap_tool_result_output(
                        partial_result,
                        session_id=session.session.id,
                        tool_call_id=tool_call_id,
                    )
                    capped_partial = replace(
                        capped_partial,
                        data=sanitize_tool_result_data(capped_partial.data),
                    )
                    partial_timeout_payload.update(capped_partial.data)
                    partial_timeout_content = capped_partial.content
                    partial_timeout_error = capped_partial.error
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type="runtime.tool_timeout",
                    source="runtime",
                    payload={
                        "tool": tool_call.tool_name,
                        "timeout_seconds": tool_timeout,
                    },
                )
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                timeout_sanitized_args = sanitize_tool_arguments(dict(tool_call.arguments))
                failed_display = build_tool_display(tool_call.tool_name, timeout_sanitized_args)
                failed_status = build_tool_status(
                    tool_call.tool_name,
                    tool_call_id,
                    phase="failed",
                    status="failed",
                    display=failed_display,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        **_tool_completed_identity_payload(session),
                        **partial_timeout_payload,
                        "tool": tool_call.tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": timeout_sanitized_args,
                        "status": "error",
                        "content": partial_timeout_content,
                        **_tool_error_payload(
                            tool_name=tool_call.tool_name,
                            error=partial_timeout_error or str(exc),
                            error_kind="tool_timeout",
                            extra_details={
                                "timed_out": True,
                                "timeout_seconds": tool_timeout,
                            },
                        ),
                        "display": failed_display,
                        "tool_status": failed_status,
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                failed_chunk, _ = self._persist_chunk(chunk_builders.failed_chunk(session=session, sequence=sequence + 1, error=str(exc)))
                yield failed_chunk
                return
            if not tool_exception_recovery_enabled and not _is_tool_timeout_like_exception(exc):
                error_sanitized_args = sanitize_tool_arguments(dict(tool_call.arguments))
                failed_display = build_tool_display(tool_call.tool_name, error_sanitized_args)
                failed_status = build_tool_status(
                    tool_call.tool_name,
                    tool_call_id,
                    phase="failed",
                    status="failed",
                    display=failed_display,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        **_tool_completed_identity_payload(session),
                        "tool": tool_call.tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": error_sanitized_args,
                        "status": "error",
                        "content": _tool_error_content(tool_call.tool_name, str(exc)),
                        **_tool_error_payload(
                            tool_name=tool_call.tool_name,
                            error=str(exc),
                        ),
                        "display": failed_display,
                        "tool_status": failed_status,
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                failed_chunk, _ = self._persist_chunk(chunk_builders.failed_chunk(session=session, sequence=sequence + 1, error=str(exc)))
                yield failed_chunk
                raise
            error_summary = _tool_error_summary(str(exc))
            error_details = _tool_error_details(tool_name=tool_call.tool_name, error=str(exc))
            retry_guidance = _tool_error_retry_guidance(str(exc))
            error_kind: str | None = None
            if isinstance(exc, ToolDiagnosticError):
                error_kind = exc.error_kind
                error_details = _tool_error_details(
                    tool_name=tool_call.tool_name,
                    error=str(exc),
                    error_kind=exc.error_kind,
                    extra=exc.error_details,
                )
                retry_guidance = exc.retry_guidance

            tool_result = ToolResult(
                tool_name=tool_call.tool_name,
                status="error",
                content=_tool_error_content(tool_call.tool_name, str(exc)),
                error=str(exc),
                data={
                    "tool_call_id": tool_call_id,
                    "arguments": dict(tool_call.arguments),
                },
                error_kind=error_kind,
                error_summary=error_summary,
                error_details=error_details,
                retry_guidance=retry_guidance,
            )

        sanitized_arguments = sanitize_tool_arguments(dict(tool_call.arguments))
        tool_result = cap_tool_result_output(
            tool_result,
            session_id=session.session.id,
            tool_call_id=tool_call_id,
        )
        tool_result = replace(
            tool_result,
            data=sanitize_tool_result_data(tool_result.data),
        )

        drained_chunks, session, _ = self._drain_runtime_events(
            session=session,
            start_sequence=sequence + 1,
        )
        yield from drained_chunks

        # Terminal-seal guard for tool-result delivery on the approval-resume
        # path: once the resume run is interrupted, the in-flight tool result
        # is a late event and must be dropped rather than persisted.
        if _is_abort_signal_requested(abort_signal):
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload=chunk_builders.user_interrupted_payload(
                        run_id=run_id_from_session_metadata(session.metadata),
                        reason=_abort_signal_reason(abort_signal),
                    ),
                    status="interrupted",
                )
            )
            yield failed_chunk
            return

        completed_payload = {
            **_tool_completed_identity_payload(session),
            **tool_result.data,
            "tool_call_id": tool_call_id,
            "arguments": sanitized_arguments,
            "status": tool_result.status,
            "content": tool_result.content,
            "error": tool_result.error,
        }
        if tool_result.error_kind is not None:
            completed_payload["error_kind"] = tool_result.error_kind
        if tool_result.error_summary is not None:
            completed_payload["error_summary"] = tool_result.error_summary
        if tool_result.error_details is not None:
            completed_payload["error_details"] = tool_result.error_details
        if tool_result.retry_guidance is not None:
            completed_payload["retry_guidance"] = tool_result.retry_guidance
        completed_payload.setdefault("tool", tool_result.tool_name)

        completed_display = build_tool_display(
            tool_call.tool_name,
            sanitized_arguments,
            result_data=tool_result.data,
        )
        completed_status = build_tool_status(
            tool_call.tool_name,
            tool_call_id,
            phase="completed" if tool_result.status == "ok" else "failed",
            status="completed" if tool_result.status == "ok" else "failed",
            display=completed_display,
        )
        completed_payload["display"] = completed_display
        completed_payload["tool_status"] = completed_status

        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_completed",
            source="tool",
            payload=completed_payload,
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        clear_tool_execution_intent(self._session_store, self._workspace, session)

        if _is_abort_signal_requested(abort_signal):
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload=chunk_builders.user_interrupted_payload(
                        run_id=run_id_from_session_metadata(session.metadata),
                        reason=_abort_signal_reason(abort_signal),
                    ),
                    status="interrupted",
                )
            )
            yield failed_chunk
            return

        if tool_result.status == "ok":
            post_hook_outcome = run_tool_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=session,
                sequence=sequence,
                tool_name=tool_call.tool_name,
                phase="post",
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(session.metadata),
            )
            sequence = yield from self._persist_chunks(
                post_hook_outcome.chunks,
                fallback_sequence=post_hook_outcome.last_sequence,
            )
            if post_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=session,
                    sequence=sequence,
                    surface="post_tool",
                    error=post_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    persisted_failed, _ = self._persist_chunk(failed_chunk)
                    yield persisted_failed
                    raise RuntimeError(post_hook_outcome.failed_error)
            if post_hook_outcome.action == "cancel":
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error="run cancelled by post-tool hook",
                        payload={"kind": "hook_cancelled", "surface": "post_tool"},
                    )
                )
                yield failed_chunk
                return

        tool_results.append(
            replace(
                tool_result,
                data={
                    **tool_result.data,
                    "tool_call_id": tool_call_id,
                    "arguments": sanitized_arguments,
                },
            )
        )

    def execute_graph_loop(
        self,
        *,
        graph: RuntimeGraph,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None = None,
        permission_policy: PermissionPolicy | None = None,
        preserved_continuity_state: ContextProjection | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        runtime = self._surface
        active_permission_policy = permission_policy or self._permission_policy
        continuity_to_reinject: ContextProjection | None = preserved_continuity_state
        provider_attempt = provider_attempt_from_metadata(graph_request.metadata)
        provider_retry_attempt: int = provider_retry_attempt_from_metadata(graph_request.metadata)
        reasoning_capture_state = ReasoningCaptureState()
        active_graph_request: GraphRunRequest = graph_request
        pending_provider_attempt_reset: _ProviderAttemptReset | None = None
        first_iteration = True
        stuck_detected_emitted = False
        checkpoint_tool_result_count = len(tool_results)
        while True:
            if pending_provider_attempt_reset is not None:
                provider_attempt = pending_provider_attempt_reset.provider_attempt
                graph = pending_provider_attempt_reset.graph
                active_graph_request = pending_provider_attempt_reset.graph_request
                session = pending_provider_attempt_reset.session
                pending_provider_attempt_reset = None
            checkpoint_tool_result_count = self._capture_iteration_checkpoint(
                graph=graph,
                session=session,
                graph_request=graph_request,
                tool_results=tool_results,
                sequence=sequence,
                checkpoint_tool_result_count=checkpoint_tool_result_count,
            )
            if tool_results and tool_results[-1].tool_name == "submit_result" and tool_results[-1].status == "ok":
                sequence = yield from self._submit_result_terminal(
                    session=session,
                    tool_results=tool_results,
                    sequence=sequence,
                )
                break
            sequence = int(sequence)
            current_graph_request: Any = active_graph_request
            current_prompt: str = cast(str, current_graph_request.prompt)
            current_available_tools: tuple[ToolDefinition, ...] = cast(tuple[ToolDefinition, ...], current_graph_request.available_tools)
            current_metadata: dict[str, object] = current_graph_request.metadata
            current_abort_signal: ProviderAbortSignal | None = current_graph_request.abort_signal
            turn_index = len(tool_results) + 1
            sequence, terminated, stuck_detected_emitted = yield from self._run_turn_hooks(
                session=session,
                sequence=sequence,
                tool_results=tool_results,
                turn_index=turn_index,
                provider_attempt=provider_attempt,
                provider_retry_attempt=provider_retry_attempt,
                stuck_detected_emitted=stuck_detected_emitted,
            )
            if terminated:
                return
            context_window, first_iteration = self._resolve_turn_context_window(
                active_graph_request=active_graph_request,
                tool_results=tool_results,
                session=session,
                continuity_to_reinject=continuity_to_reinject,
                first_iteration=first_iteration,
            )
            session, assembled_context = yield from self._assemble_turn_context(
                active_graph_request=active_graph_request,
                context_window=context_window,
                session=session,
            )
            active_graph_request = GraphRunRequest(
                session=session,
                prompt=current_prompt,
                available_tools=current_available_tools,
                context_window=context_window,
                assembled_context=assembled_context,
                metadata=current_metadata,
                abort_signal=current_abort_signal,
            )
            effective_runtime_config = runtime.effective_runtime_config_from_metadata(session.metadata)
            session, sequence, terminated = yield from self._emit_turn_context_events(
                session=session,
                sequence=sequence,
                active_graph_request=active_graph_request,
                effective_runtime_config=effective_runtime_config,
                context_window=context_window,
                continuity_to_reinject=continuity_to_reinject,
            )
            continuity_to_reinject = None
            if terminated:
                return
            try:
                graph_step, sequence, streamed_reasoning_texts = yield from self._invoke_provider_step(
                    active_graph_request=active_graph_request,
                    tool_results=tool_results,
                    session=session,
                    sequence=sequence,
                    reasoning_capture_state=reasoning_capture_state,
                    graph=graph,
                )
                if graph_step is None:
                    return
                provider_retry_attempt = 0
            except Exception as exc:
                verdict = yield from self._apply_provider_error_policy(
                    exc=exc,
                    session=session,
                    sequence=sequence,
                    active_graph_request=active_graph_request,
                    context_window=context_window,
                    effective_runtime_config=effective_runtime_config,
                    provider_attempt=provider_attempt,
                    provider_retry_attempt=provider_retry_attempt,
                    current_metadata=current_metadata,
                    current_prompt=current_prompt,
                    current_available_tools=current_available_tools,
                    current_abort_signal=current_abort_signal,
                    graph=graph,
                )
                action = verdict["action"]
                if action == "exit":
                    return
                if action == "reraise":
                    raise verdict["exc"] from None
                provider_attempt = verdict["provider_attempt"]
                provider_retry_attempt = verdict["provider_retry_attempt"]
                graph = verdict["graph"]
                session = verdict["session"]
                active_graph_request = verdict["graph_request"]
                continue

            sequence = yield from self._persist_turn_reasoning(
                session=session,
                sequence=sequence,
                streamed_reasoning_texts=streamed_reasoning_texts,
            )

            is_final_step, session, current_chunk_session, provider_attempt, terminated = yield from self._finalize_step_state(
                session=session,
                sequence=sequence,
                active_graph_request=active_graph_request,
                graph_step=graph_step,
                provider_attempt=provider_attempt,
                tool_results=tool_results,
            )
            if terminated:
                return

            sequence = yield from self._persist_step_events(
                session=session,
                sequence=sequence,
                graph_step=graph_step,
                reasoning_capture_state=reasoning_capture_state,
                current_chunk_session=current_chunk_session,
            )

            if is_final_step:
                yield from self._emit_final_step_artifacts(
                    runtime=runtime,
                    session=current_chunk_session,
                    graph_step=graph_step,
                    reasoning_capture_state=reasoning_capture_state,
                )
                break

            plan_tool_call, tool, tool_call_id, sequence = yield from self._plan_tool_step(
                session=session,
                sequence=sequence,
                active_graph_request=active_graph_request,
                tool_registry=tool_registry,
                graph_step=graph_step,
            )

            if plan_tool_call.tool_name == "invoke_tool":
                # On-demand dispatch: resolve the inner tool and run it through
                # the SAME execution boundary as a provider-native tool call
                # (policy denial -> registry resolve -> permission -> hooks ->
                # executor). Unknown tools and denials surface as tool-level
                # feedback; the run continues.
                yield from self._execute_invoked_tool(
                    tool_registry=tool_registry,
                    session=session,
                    sequence=sequence,
                    outer_call=plan_tool_call,
                    outer_call_id=tool_call_id,
                    tool_results=tool_results,
                    permission_policy=active_permission_policy,
                    abort_signal=active_graph_request.abort_signal,
                )
                continue

            action, session, sequence = yield from self._resolve_permission_for_tool(
                session=session,
                sequence=sequence,
                tool=tool,
                plan_tool_call=plan_tool_call,
                tool_call_id=tool_call_id,
                approval_resolution=approval_resolution,
                active_permission_policy=active_permission_policy,
                effective_runtime_config=effective_runtime_config,
                tool_results=tool_results,
            )
            if action == "return":
                return
            if action == "continue":
                continue

            sequence, verdict = yield from self._run_tool_hook_phase(
                session=session,
                sequence=sequence,
                tool_name=plan_tool_call.tool_name,
                phase="pre",
                cancel_message="run cancelled by pre-tool hook",
            )
            if verdict == "cancel":
                return

            tool_timeout = runtime.effective_runtime_config_from_metadata(session.metadata).tool_timeout_seconds
            action, tool_result, session, sequence = yield from self._execute_tool_and_recover(
                session=session,
                sequence=sequence,
                plan_tool_call=plan_tool_call,
                tool=tool,
                tool_call_id=tool_call_id,
                tool_timeout=tool_timeout,
                tool_results=tool_results,
                active_graph_request=active_graph_request,
                tool_exception_recovery_enabled=effective_runtime_config.execution_engine == "provider",
            )
            if action == "returned":
                return

            tool_result, duplicate_todo_write, runtime_tool_result_data, session, sequence, terminated = yield from self._finalize_tool_result(
                session=session,
                sequence=sequence,
                plan_tool_call=plan_tool_call,
                tool_call_id=tool_call_id,
                tool_result=cast(ToolResult, tool_result),
                active_graph_request=active_graph_request,
            )
            if terminated:
                return

            if (
                yield from self._handle_question_outcome(
                    session=session,
                    sequence=sequence,
                    plan_tool_call=plan_tool_call,
                    tool_result=tool_result,
                )
            ):
                return

            sanitized_arguments = sanitize_tool_arguments(dict(plan_tool_call.arguments))
            sequence, session = yield from self._emit_tool_completed_events(
                session=session,
                sequence=sequence,
                plan_tool_call=plan_tool_call,
                tool_call_id=tool_call_id,
                sanitized_arguments=sanitized_arguments,
                tool_result=tool_result,
                runtime_tool_result_data=runtime_tool_result_data,
                duplicate_todo_write=duplicate_todo_write,
            )

            if _is_abort_requested(active_graph_request):
                yield from self._emit_interrupted_failure(
                    session=session,
                    sequence=sequence,
                    active_graph_request=active_graph_request,
                )
                return

            if tool_result.status == "ok":
                sequence, verdict = yield from self._run_tool_hook_phase(
                    session=session,
                    sequence=sequence,
                    tool_name=plan_tool_call.tool_name,
                    phase="post",
                    cancel_message="run cancelled by post-tool hook",
                )
                if verdict == "cancel":
                    return

            tool_results.append(
                replace(
                    tool_result,
                    data={
                        **tool_result.data,
                        "tool_call_id": tool_call_id,
                        "arguments": sanitized_arguments,
                    },
                )
            )
            if provider_attempt != 0:
                pending_provider_attempt_reset = _provider_attempt_reset_after_tool_result(
                    provider_attempt=provider_attempt,
                    selection=select_graph_for_effective_config(
                        config=effective_runtime_config,
                        provider_attempt=0,
                    ),
                    graph_request=active_graph_request,
                    session=session,
                )

    def _capture_iteration_checkpoint(
        self,
        *,
        graph: RuntimeGraph,
        session: SessionState,
        graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        sequence: int,
        checkpoint_tool_result_count: int,
    ) -> int:
        if len(tool_results) > checkpoint_tool_result_count and self._at_safe_boundary(graph):
            self._capture_interrupted_checkpoint(
                session=session,
                prompt=graph_request.prompt,
                tool_results=tool_results,
                last_event_sequence=sequence,
            )
            checkpoint_tool_result_count = len(tool_results)
        return checkpoint_tool_result_count

    def _submit_result_terminal(
        self,
        *,
        session: SessionState,
        tool_results: list[ToolResult],
        sequence: int,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        terminal_result = tool_results[-1]
        terminal_output = (terminal_result.content or "").strip()
        if not terminal_output:
            raise ValueError("submit_result completed without a non-empty summary")
        completed_session = session_with_plan_state(
            SessionState(
                session=session.session,
                status="completed",
                turn=session.turn,
                metadata=session.metadata,
            ),
            status="completed",
        )
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="graph.response_ready",
            source="graph",
            payload={"output_preview": terminal_output, "source": "submit_result"},
        )
        yield RuntimeStreamChunk(kind="event", session=completed_session, event=envelope)
        yield RuntimeStreamChunk(kind="output", session=completed_session, output=terminal_output)
        return envelope.sequence

    def _persist_turn_reasoning(
        self,
        *,
        session: SessionState,
        sequence: int,
        streamed_reasoning_texts: list[str],
    ) -> Generator[RuntimeStreamChunk, None, int]:
        # The live provider_stream reasoning deltas above are client-only (not
        # persisted), and non-streaming turns capture reasoning on the step.
        # Persist one aggregated runtime.reasoning_part so replay of a completed
        # session still shows the turn's thinking. The client already rendered
        # the streamed deltas, so the aggregate is deduplicated on the frontend
        # when it equals the streamed text.
        if not streamed_reasoning_texts:
            return sequence
        reasoning_text = "".join(streamed_reasoning_texts)
        reasoning_truncated = len(reasoning_text) > REASONING_PERSISTED_LIMIT_CHARS
        if reasoning_truncated:
            reasoning_text = reasoning_text[:REASONING_PERSISTED_LIMIT_CHARS]
        reasoning_part_payload = runtime_reasoning_part_payload(
            text=reasoning_text,
        )
        if reasoning_truncated:
            reasoning_part_payload["truncated"] = True
        reasoning_part_envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_REASONING_PART,
            source="runtime",
            payload=reasoning_part_payload,
        )
        sequence = reasoning_part_envelope.sequence
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=reasoning_part_envelope,
        )
        return sequence

    def _persist_step_events(
        self,
        *,
        session: SessionState,
        sequence: int,
        graph_step: Any,
        reasoning_capture_state: ReasoningCaptureState,
        current_chunk_session: SessionState,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        renumbered_events = renumber_events(
            getattr(graph_step, "events", ()),
            session_id=session.session.id,
            start_sequence=sequence + 1,
            reasoning_capture_state=reasoning_capture_state,
        )
        persisted_events = self._persist_events(
            session_id=session.session.id,
            events=tuple((event.event_type, event.source, event.payload, None) for event in renumbered_events),
        )
        for envelope in persisted_events:
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=current_chunk_session, event=envelope)
        return sequence

    def _emit_final_step_artifacts(
        self,
        *,
        runtime: RuntimeSurface,
        session: SessionState,
        graph_step: Any,
        reasoning_capture_state: ReasoningCaptureState,
    ) -> Generator[RuntimeStreamChunk]:
        reasoning_diagnostic = _reasoning_output_diagnostic(
            runtime,
            self._provider_catalog_query,
            session=session,
            capture_state=reasoning_capture_state,
        )
        if reasoning_diagnostic is not None:
            envelope = self._persist_event(
                session_id=session.session.id,
                event_type="runtime.reasoning_diagnostic",
                source="runtime",
                payload=reasoning_diagnostic,
            )
            yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        if getattr(graph_step, "output", None) is not None:
            yield RuntimeStreamChunk(
                kind="output",
                session=session,
                output=graph_step.output,
            )

    def _emit_interrupted_failure(
        self,
        *,
        session: SessionState,
        sequence: int,
        active_graph_request: GraphRunRequest,
    ) -> Generator[RuntimeStreamChunk]:
        failed_chunk, _ = self._persist_chunk(
            chunk_builders.failed_chunk(
                session=session,
                sequence=sequence + 1,
                error="run interrupted",
                payload=chunk_builders.user_interrupted_payload(
                    run_id=run_id_from_session_metadata(session.metadata),
                    reason=_abort_reason(active_graph_request),
                ),
                status="interrupted",
            )
        )
        yield failed_chunk

    def _run_turn_hook_phase(
        self,
        *,
        session: SessionState,
        sequence: int,
        surface: RuntimeHookSurface,
        payload: dict[str, object],
        cancel_message: str,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, bool]]:
        hook = run_lifecycle_hooks_for_session(
            hooks=self._config.hooks,
            workspace=self._workspace,
            session=session,
            sequence=sequence,
            surface=surface,
            payload=payload,
            recursion_env_var=HOOK_RECURSION_ENV_VAR,
            policy=hook_execution_policy_from_metadata(session.metadata),
        )
        sequence = yield from self._persist_chunks(
            hook.chunks,
            fallback_sequence=hook.last_sequence,
        )
        if hook.failed_error is not None:
            failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                session=session,
                sequence=sequence,
                surface=surface,
                error=hook.failed_error,
                hooks=self._config.hooks,
            )
            if failed_chunk is not None:
                persisted_failed, _ = self._persist_chunk(failed_chunk)
                yield persisted_failed
                return sequence, True
        if hook.action == "cancel":
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=cancel_message,
                    payload={"kind": "hook_cancelled", "surface": surface},
                )
            )
            yield failed_chunk
            return sequence, True
        return sequence, False

    def _run_turn_hooks(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_results: list[ToolResult],
        turn_index: int,
        provider_attempt: int,
        provider_retry_attempt: int,
        stuck_detected_emitted: bool,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, bool, bool]]:
        turn_progress_payload: dict[str, object] = {
            "turn": turn_index,
            "tool_result_count": len(tool_results),
            "provider_attempt": provider_attempt,
            "provider_retry_attempt": provider_retry_attempt,
        }
        sequence, terminated = yield from self._run_turn_hook_phase(
            session=session,
            sequence=sequence,
            surface="turn_progress",
            payload=turn_progress_payload,
            cancel_message="run cancelled by turn-progress hook",
        )
        if terminated:
            return sequence, True, stuck_detected_emitted
        if not stuck_detected_emitted and self._is_stuck_tool_loop(
            turn=turn_index,
            tool_results=tool_results,
        ):
            stuck_payload: dict[str, object] = {
                **turn_progress_payload,
                "distinct_tool_count": len({result.tool_name for result in tool_results}),
                "reason": "repeated_tool_loop",
            }
            stuck_detected_emitted = True
            sequence, terminated = yield from self._run_turn_hook_phase(
                session=session,
                sequence=sequence,
                surface="stuck_detected",
                payload=stuck_payload,
                cancel_message="run cancelled by stuck-detected hook",
            )
            if terminated:
                return sequence, True, stuck_detected_emitted
        return sequence, False, stuck_detected_emitted

    def _resolve_turn_context_window(
        self,
        *,
        active_graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        session: SessionState,
        continuity_to_reinject: ContextProjection | None,
        first_iteration: bool,
    ) -> tuple[RuntimeContextWindow, bool]:
        runtime = self._surface
        current_graph_request = active_graph_request
        current_prompt = current_graph_request.prompt
        current_abort_signal = current_graph_request.abort_signal
        current_session_metadata: dict[str, object] = session.metadata
        if first_iteration:
            prebuilt_context = cast(RuntimeContextWindow, current_graph_request.context_window)
            first_iteration = False
            if prebuilt_context.original_tool_result_count == len(tool_results):
                base_context = prebuilt_context
            else:
                base_context = runtime.prepare_provider_context_window(
                    prompt=current_prompt,
                    tool_results=tuple(tool_results),
                    session_metadata=current_session_metadata,
                    abort_signal=current_abort_signal,
                )
        else:
            base_context = runtime.prepare_provider_context_window(
                prompt=current_prompt,
                tool_results=tuple(tool_results),
                session_metadata=current_session_metadata,
                abort_signal=current_abort_signal,
            )
        reinjected_continuity = continuity_to_reinject
        if reinjected_continuity is not None:
            summary_anchor, summary_source = continuity_summary_metadata(reinjected_continuity)
            context_window = RuntimeContextWindow(
                prompt=base_context.prompt,
                tool_results=base_context.tool_results,
                compacted=base_context.compacted,
                compaction_reason=base_context.compaction_reason,
                original_tool_result_count=base_context.original_tool_result_count,
                retained_tool_result_count=base_context.retained_tool_result_count,
                original_tool_result_tokens=base_context.original_tool_result_tokens,
                retained_tool_result_tokens=base_context.retained_tool_result_tokens,
                dropped_tool_result_tokens=base_context.dropped_tool_result_tokens,
                token_budget=base_context.token_budget,
                token_estimate_source=base_context.token_estimate_source,
                model_context_window_tokens=base_context.model_context_window_tokens,
                reserved_output_tokens=base_context.reserved_output_tokens,
                truncated_tool_result_count=base_context.truncated_tool_result_count,
                continuity_state=reinjected_continuity,
                summary_anchor=summary_anchor,
                summary_source=summary_source,
            )
        else:
            context_window = base_context
        return context_window, first_iteration

    def _assemble_turn_context(
        self,
        *,
        active_graph_request: GraphRunRequest,
        context_window: RuntimeContextWindow,
        session: SessionState,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, ProviderAssembledContext]]:
        runtime = self._surface
        current_graph_request = active_graph_request
        current_prompt = current_graph_request.prompt
        current_segments = current_graph_request.assembled_context.segments
        session = session_with_context_window_metadata(session, context_window)
        skill_prompt_context = ""
        preserved_system_segments: list[str] = []
        for segment in current_segments:
            if segment.role != "system" or not isinstance(segment.content, str):
                continue
            segment_source = segment.metadata.get("source") if isinstance(segment.metadata, dict) else None
            if segment_source in {
                "hook_preset_guidance",
                "runtime_file_rules",
                "directory_readme_context",
            }:
                continue
            if segment.content.startswith("Runtime-managed todo state is active"):
                continue
            preserved_system_segments.append(segment.content)
            if segment.content.startswith("Runtime-managed skills are active for this turn."):
                skill_prompt_context = segment.content
        assembled_context = runtime.assemble_provider_context(
            prompt=current_prompt,
            tool_results=context_window.tool_results,
            session_metadata=session.metadata,
            skill_prompt_context=skill_prompt_context,
            preserved_system_segments=tuple(preserved_system_segments),
            replayed_conversation_segments=_replayed_conversation_segments(current_graph_request),
        )
        context_window_payload = {
            **assembled_context.metadata,
            **context_window.metadata_payload(),
        }
        for estimate_key in (
            "estimated_context_tokens",
            "estimated_context_token_source",
            "estimated_context_token_exact",
        ):
            if estimate_key in assembled_context.metadata:
                context_window_payload[estimate_key] = assembled_context.metadata[estimate_key]
        session = session_with_context_window_payload_metadata(
            session,
            context_window_payload,
        )
        context_transform_payloads = _context_transform_applied_payloads(
            context_metadata=assembled_context.metadata,
            tool_result_count=len(context_window.tool_results),
        )
        unseen_context_transform_payloads = _unseen_context_transform_payloads(
            session=session,
            payloads=context_transform_payloads,
        )
        if unseen_context_transform_payloads:
            session = session_with_context_transform_applied_state(
                session=session,
                fingerprints=tuple(fingerprint for fingerprint, _payload in unseen_context_transform_payloads),
            )
            for _fingerprint, payload in unseen_context_transform_payloads:
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_CONTEXT_TRANSFORM_APPLIED,
                    source="runtime",
                    payload=payload,
                )
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return session, assembled_context

    def _emit_turn_context_events(
        self,
        *,
        session: SessionState,
        sequence: int,
        active_graph_request: GraphRunRequest,
        effective_runtime_config: EffectiveRuntimeConfig,
        context_window: RuntimeContextWindow,
        continuity_to_reinject: ContextProjection | None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, bool]]:
        runtime = self._surface
        reinjected_continuity = continuity_to_reinject
        provider_context_policy_decision: RuntimeProviderContextPolicyDecision | None = runtime.provider_context_policy_decision_for_graph_request(
            graph_request=active_graph_request,
            effective_config=effective_runtime_config,
        )
        if provider_context_policy_decision is not None:
            if provider_context_policy_decision.action == "warn":
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type="runtime.provider_context_policy",
                    source="runtime",
                    payload={
                        "mode": provider_context_policy_decision.mode,
                        "action": provider_context_policy_decision.action,
                        "blocked": provider_context_policy_decision.blocked,
                        "diagnostic_count": (provider_context_policy_decision.diagnostic_count),
                        "diagnostic_codes": list(provider_context_policy_decision.diagnostic_codes),
                        "blocking_diagnostic_codes": list(provider_context_policy_decision.blocking_diagnostic_codes),
                        "message": provider_context_policy_decision.message,
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
            if provider_context_policy_decision.blocked:
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=provider_context_policy_decision.message,
                        payload={
                            "kind": "provider_context_policy_blocked",
                            "provider_context_policy": {
                                "mode": provider_context_policy_decision.mode,
                                "action": provider_context_policy_decision.action,
                                "blocked": provider_context_policy_decision.blocked,
                                "diagnostic_count": (provider_context_policy_decision.diagnostic_count),
                                "diagnostic_codes": list(provider_context_policy_decision.diagnostic_codes),
                                "blocking_diagnostic_codes": list(provider_context_policy_decision.blocking_diagnostic_codes),
                            },
                        },
                    )
                )
                yield failed_chunk
                return session, sequence, True
        if (
            context_window.compacted
            and reinjected_continuity is None
            and self._should_emit_context_compacted(
                session=session,
                summary_anchor=context_window.summary_anchor,
                original_tool_result_count=context_window.original_tool_result_count,
                retained_tool_result_count=context_window.retained_tool_result_count,
            )
        ):
            memory_payload = self._build_context_compacted_payload(context_window)
            if memory_payload is not None:
                session = session_with_context_compacted_state(
                    session=session,
                    summary_anchor=context_window.summary_anchor,
                    original_tool_result_count=context_window.original_tool_result_count,
                    retained_tool_result_count=context_window.retained_tool_result_count,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_CONTEXT_COMPACTED,
                    source="runtime",
                    payload=memory_payload,
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return session, sequence, False

    def _finalize_step_state(
        self,
        *,
        session: SessionState,
        sequence: int,
        active_graph_request: GraphRunRequest,
        graph_step: Any,
        provider_attempt: int,
        tool_results: list[ToolResult],
    ) -> Generator[RuntimeStreamChunk, None, tuple[bool, SessionState, SessionState, int, bool]]:
        is_final_step = getattr(graph_step, "is_finished", False) or getattr(graph_step, "output", None) is not None
        # Keep-alive delegated turns (internal ``keep_alive_turn`` metadata
        # set by the background-task worker) are intermediate turns of a
        # resumable worker and must not be forced to submit_result; the
        # one-shot child contract (no such metadata key) is unchanged.
        if is_final_step and session.session.parent_id is not None and session.metadata.get("keep_alive_turn") is not True:
            if not tool_results or tool_results[-1].tool_name != "submit_result" or tool_results[-1].status != "ok":
                raise ValueError("delegated child must call submit_result before completing")
        if _is_abort_requested(active_graph_request):
            yield from self._emit_interrupted_failure(
                session=session,
                sequence=sequence,
                active_graph_request=active_graph_request,
            )
            return False, session, session, provider_attempt, True
        session, provider_attempt, final_step_status = _finalized_step_session(
            session=session,
            graph_step=graph_step,
            is_final_step=is_final_step,
            provider_attempt=provider_attempt,
        )
        current_chunk_session = session
        if is_final_step:
            # Keep-alive turns park as ``interrupted`` (resumable child,
            # no handoff) so the background-task worker can park the task
            # idle awaiting steer; one-shot children keep ``completed``.
            current_chunk_session = session_with_plan_state(
                SessionState(
                    session=session.session,
                    status=final_step_status,
                    turn=session.turn,
                    metadata=session.metadata,
                ),
                status=final_step_status,
            )
        return is_final_step, session, current_chunk_session, provider_attempt, False

    def _invoke_provider_step(
        self,
        *,
        active_graph_request: GraphRunRequest,
        tool_results: list[ToolResult],
        session: SessionState,
        sequence: int,
        reasoning_capture_state: ReasoningCaptureState,
        graph: RuntimeGraph,
    ) -> Generator[RuntimeStreamChunk, None, tuple[Any | None, int, list[str]]]:
        streamed_reasoning_texts: list[str] = []
        if _is_abort_requested(active_graph_request):
            yield from self._emit_interrupted_failure(
                session=session,
                sequence=sequence,
                active_graph_request=active_graph_request,
            )
            return None, sequence, streamed_reasoning_texts
        stream_step = getattr(graph, "stream_step", None)
        if active_graph_request.metadata.get("provider_stream") is True and callable(stream_step):
            graph_step = None
            for streamed_item in stream_step(
                active_graph_request,
                tuple(tool_results),
                session=session,
            ):
                if _is_abort_requested(active_graph_request):
                    # Terminal-seal guard for provider deltas: once this
                    # run is interrupted, every remaining stream delta is
                    # a late event — drop it instead of streaming it to
                    # the client. Keep consuming the generator so a
                    # graph-raised provider error (e.g. an abort-aware
                    # provider surfacing a ``cancelled`` failure) still
                    # propagates through the normal exception handler
                    # instead of being masked by the interrupt.
                    if not isinstance(streamed_item, GraphEvent):
                        graph_step = streamed_item
                    continue
                if isinstance(streamed_item, GraphEvent):
                    # Live client-only stream deltas are NOT persisted, so
                    # they must not advance the persisted-sequence cursor.
                    # They share the current cursor value; the renumbered
                    # batch persisted after this loop continues monotonically.
                    # Reasoning deltas are additionally accumulated so the
                    # turn can persist one aggregated runtime.reasoning_part
                    # below, keeping replay faithful after the live stream
                    # ends (mirrors renumber_events capture semantics).
                    if streamed_item.event_type == "graph.provider_stream":
                        reasoning_capture_state.stream_observed = True
                        reasoning_payload = runtime_reasoning_part_from_provider_stream(streamed_item.payload)
                        if reasoning_payload is not None:
                            reasoning_capture_state.reasoning_observed = True
                            captured_text = reasoning_payload.get("text")
                            if isinstance(captured_text, str) and captured_text:
                                streamed_reasoning_texts.append(captured_text)
                            reasoning_capture_state.part_count += 1
                            text_char_count = reasoning_payload.get("text_char_count")
                            if isinstance(text_char_count, int):
                                reasoning_capture_state.text_char_count += text_char_count
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type=streamed_item.event_type,
                            source=streamed_item.source,
                            payload=streamed_item.payload,
                        ),
                    )
                else:
                    graph_step = streamed_item
            if graph_step is None:
                raise RuntimeError("graph stream ended without a terminal step")
        else:
            graph_step = graph.step(
                active_graph_request,
                tool_results=tuple(tool_results),
                session=session,
            )
            # Non-streaming turns (background children) carry the turn's
            # reasoning on the step; aggregate it like the streamed deltas
            # so one bounded runtime.reasoning_part is persisted below.
            non_stream_reasoning = getattr(graph_step, "reasoning", None)
            if isinstance(non_stream_reasoning, str) and non_stream_reasoning:
                reasoning_capture_state.stream_observed = True
                reasoning_capture_state.reasoning_observed = True
                reasoning_capture_state.part_count += 1
                reasoning_capture_state.text_char_count += len(non_stream_reasoning)
                streamed_reasoning_texts.append(non_stream_reasoning)
        return graph_step, sequence, streamed_reasoning_texts

    def _apply_provider_error_policy(
        self,
        *,
        exc: Exception,
        session: SessionState,
        sequence: int,
        active_graph_request: GraphRunRequest,
        context_window: RuntimeContextWindow,
        effective_runtime_config: EffectiveRuntimeConfig,
        provider_attempt: int,
        provider_retry_attempt: int,
        current_metadata: dict[str, object],
        current_prompt: str,
        current_available_tools: tuple[ToolDefinition, ...],
        current_abort_signal: ProviderAbortSignal | None,
        graph: RuntimeGraph,
    ) -> Generator[RuntimeStreamChunk, None, _ProviderErrorPolicyVerdict]:
        current_provider_attempt = provider_attempt_from_metadata({"provider_attempt": provider_attempt})
        provider_error = exc if isinstance(exc, ProviderExecutionError) else None
        if provider_error is not None:
            fallback_selection = fallback_graph_for_provider_error(
                error=provider_error,
                provider_chain=effective_runtime_config.resolved_provider.target_chain,
                config=effective_runtime_config,
                provider_attempt=current_provider_attempt,
            )
            transient_retry_config = provider_transient_retry_config(
                providers=effective_runtime_config.providers,
                provider_name=provider_error.provider_name,
            )
            fallback_target = fallback_selection.provider_target if fallback_selection is not None else None
            provider_decision = decide_provider_error_policy(
                error=provider_error,
                current_provider_attempt=current_provider_attempt,
                provider_retry_attempt=int(provider_retry_attempt),
                transient_retry_config=transient_retry_config,
                fallback_target_provider=(fallback_target.selection.provider if fallback_target is not None else None),
                fallback_target_model=(fallback_target.selection.model if fallback_target is not None else None),
                background_rate_limit_retry=(active_graph_request.metadata.get("background_rate_limit_retry") is True),
            )
            if isinstance(provider_decision, ProviderTerminalDecision) and (provider_decision.kind == "cancelled"):
                # A ``cancelled`` provider error is the abort-aware provider
                # surfacing a user/run cancellation (``abort_signal`` fired via
                # the cancel endpoint or client disconnect) mid-stream. It is
                # not a provider failure: the run ends ``interrupted`` (the
                # ``runtime.failed{cancelled: true}`` event shape is preserved
                # for client compatibility; the terminal-status derivation
                # keys off the cancelled flag, never the event type).
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=str(provider_error),
                        payload=provider_decision.payload,
                        status="interrupted",
                    )
                )
                yield failed_chunk
                return {"action": "exit"}
            if isinstance(provider_decision, ProviderTerminalDecision) and (provider_decision.kind == "background_rate_limit_retry"):
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=str(provider_error),
                        payload=provider_decision.payload,
                    )
                )
                yield failed_chunk
                return {"action": "exit"}
            if isinstance(provider_decision, ProviderTransientRetryDecision):
                delay_ms = provider_decision.delay_ms
                logger.info(
                    ("provider transient retry for session %s: %s/%s (reason=%s, retry_attempt=%s, max_retries=%s, delay_ms=%s)"),
                    session.session.id,
                    provider_error.provider_name,
                    provider_error.model_name,
                    provider_error.kind,
                    provider_decision.retry_attempt,
                    provider_decision.max_retries,
                    delay_ms,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_PROVIDER_TRANSIENT_RETRY,
                    source="runtime",
                    payload=provider_decision.event_payload(),
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                if delay_ms > 0:
                    time.sleep(delay_ms / 1000.0)
                provider_retry_attempt = int(provider_decision.retry_attempt)
                retry_metadata: dict[str, object] = {
                    **current_metadata,
                    "provider_attempt": current_provider_attempt,
                    "provider_retry_attempt": provider_retry_attempt,
                }
                session = SessionState(
                    session=session.session,
                    status=session.status,
                    turn=session.turn,
                    metadata={
                        **session.metadata,
                        "provider_attempt": current_provider_attempt,
                        "provider_retry_attempt": provider_retry_attempt,
                    },
                )
                active_graph_request = GraphRunRequest(
                    session=session,
                    prompt=current_prompt,
                    available_tools=current_available_tools,
                    context_window=context_window,
                    assembled_context=active_graph_request.assembled_context,
                    metadata=retry_metadata,
                    abort_signal=current_abort_signal,
                )
                return {
                    "action": "retry",
                    "provider_attempt": current_provider_attempt,
                    "provider_retry_attempt": provider_retry_attempt,
                    "graph": graph,
                    "session": session,
                    "graph_request": active_graph_request,
                }
            if isinstance(provider_decision, ProviderFallbackDecision):
                assert fallback_selection is not None
                next_target = fallback_selection.provider_target
                logger.info(
                    ("provider fallback for session %s: %s/%s -> %s/%s (reason=%s, attempt=%s)"),
                    session.session.id,
                    provider_error.provider_name,
                    provider_error.model_name,
                    next_target.selection.provider,
                    next_target.selection.model,
                    provider_error.kind,
                    provider_decision.attempt,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_PROVIDER_FALLBACK,
                    source="runtime",
                    payload=provider_decision.event_payload(),
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                provider_attempt = fallback_selection.provider_attempt
                provider_retry_attempt = 0
                fallback_prompt: str = current_prompt
                fallback_available_tools: tuple[ToolDefinition, ...] = current_available_tools
                fallback_context_window = context_window
                fallback_assembled_context: ProviderAssembledContext = active_graph_request.assembled_context
                fallback_metadata: dict[str, object] = {
                    **current_metadata,
                    "provider_attempt": provider_attempt,
                    "provider_retry_attempt": provider_retry_attempt,
                }
                fallback_abort_signal: ProviderAbortSignal | None = current_abort_signal
                session = SessionState(
                    session=session.session,
                    status=session.status,
                    turn=session.turn,
                    metadata={
                        **session.metadata,
                        "provider_attempt": provider_attempt,
                        "provider_retry_attempt": provider_retry_attempt,
                    },
                )
                graph = fallback_selection.graph
                active_graph_request = GraphRunRequest(
                    session=session,
                    prompt=fallback_prompt,
                    available_tools=fallback_available_tools,
                    context_window=fallback_context_window,
                    assembled_context=fallback_assembled_context,
                    metadata=fallback_metadata,
                    abort_signal=fallback_abort_signal,
                )
                return {
                    "action": "fallback",
                    "provider_attempt": provider_attempt,
                    "provider_retry_attempt": provider_retry_attempt,
                    "graph": graph,
                    "session": session,
                    "graph_request": active_graph_request,
                }
            if isinstance(provider_decision, ProviderTerminalDecision) and (provider_decision.kind == "fallback_exhausted"):
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        # Surface the raw provider error message verbatim; the
                        # retry/fallback exhaustion context stays available as
                        # structured payload flags (fallback_exhausted,
                        # provider_retry_exhausted, provider_retry_attempts).
                        error=provider_error.message,
                        payload=provider_decision.payload,
                    )
                )
                yield failed_chunk
                return {"action": "exit"}
        if provider_error is not None:
            assert isinstance(provider_decision, ProviderTerminalDecision)
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=str(provider_error),
                    payload=provider_decision.payload,
                )
            )
            yield failed_chunk
            return {"action": "exit"}
        classified_error = classify_provider_error(exc)
        failed_chunk, _ = self._persist_chunk(
            chunk_builders.failed_chunk(
                session=session,
                sequence=sequence + 1,
                error=str(exc),
                payload=({"kind": "provider_context_limit"} if isinstance(classified_error, SingleAgentContextLimitError) else None),
            )
        )
        yield failed_chunk
        if isinstance(classified_error, SingleAgentContextLimitError):
            return {"action": "exit"}
        return {"action": "reraise", "exc": exc}

    def _plan_tool_step(
        self,
        *,
        session: SessionState,
        sequence: int,
        active_graph_request: GraphRunRequest,
        tool_registry: ToolRegistry,
        graph_step: Any,
    ) -> Generator[RuntimeStreamChunk, None, tuple[Any, Any, str, int]]:
        runtime = self._surface
        plan_tool_call = getattr(graph_step, "tool_call", None)
        if plan_tool_call is None:
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="graph step did not produce a tool call or output",
                )
            )
            yield failed_chunk
            raise ValueError("graph step did not produce a tool call or output")

        explicit_tool_call_id = plan_tool_call.tool_call_id
        tool_call_id = explicit_tool_call_id or f"runtime-tool-{uuid4().hex}"
        tool_request_payload: dict[str, object] = {
            "tool": plan_tool_call.tool_name,
            "arguments": dict(plan_tool_call.arguments),
            **({"path": path} if isinstance((path := plan_tool_call.arguments.get("path")), str) else {}),
        }
        if explicit_tool_call_id is not None or runtime.effective_runtime_config_from_metadata(session.metadata).execution_engine == "provider":
            tool_request_payload["tool_call_id"] = tool_call_id
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="graph.tool_request_created",
            source="graph",
            payload=tool_request_payload,
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        delegation_policy_error = runtime.delegation_tool_policy_error(
            session=session,
            tool_name=plan_tool_call.tool_name,
        )
        if delegation_policy_error is not None:
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=delegation_policy_error,
                    payload={
                        "kind": "delegation_tool_policy_denied",
                        "tool": plan_tool_call.tool_name,
                    },
                )
            )
            yield failed_chunk
            raise ValueError(delegation_policy_error)

        tool_policy_denial = runtime.tool_policy_denial(
            session=session,
            tool_name=plan_tool_call.tool_name,
        )
        if tool_policy_denial is not None:
            policy_error_message = tool_policy_error(tool_policy_denial)
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=policy_error_message,
                    payload={
                        "kind": "runtime_tool_policy_denied",
                        "tool": plan_tool_call.tool_name,
                        "tool_policy": tool_policy_denial.metadata(),
                    },
                )
            )
            yield failed_chunk
            raise ValueError(policy_error_message)

        try:
            tool = tool_registry.resolve(plan_tool_call.tool_name)
        except Exception as exc:
            failed_chunk, _ = self._persist_chunk(chunk_builders.failed_chunk(session=session, sequence=sequence + 1, error=str(exc)))
            yield failed_chunk
            raise

        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_lookup_succeeded",
            source="runtime",
            payload={"tool": plan_tool_call.tool_name},
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return plan_tool_call, tool, tool_call_id, sequence

    def _resolve_permission_for_tool(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool: Any,
        plan_tool_call: Any,
        tool_call_id: str,
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None,
        active_permission_policy: PermissionPolicy,
        effective_runtime_config: EffectiveRuntimeConfig,
        tool_results: list[ToolResult],
    ) -> Generator[RuntimeStreamChunk, None, tuple[str, SessionState, int]]:
        runtime = self._surface
        if approval_resolution is not None:
            pending, decision = approval_resolution
            if plan_tool_call.tool_name == pending.tool_name and dict(plan_tool_call.arguments) == pending.arguments:
                permission_chunks = runtime.approval_resolution_outcome(
                    session=session,
                    pending=pending,
                    decision=decision,
                    sequence=sequence + 1,
                )
                approval_resolution = None
            else:
                # Tool call changed on replay (non-deterministic model output) —
                # deny decisions remain terminal for the original pending
                # approval.  Allow decisions may still fall back to a fresh
                # permission check for older resume paths that re-enter via
                # the graph before executing the approved tool directly.
                approval_resolution = None
                if decision == "deny":
                    permission_chunks = runtime.approval_resolution_outcome(
                        session=session,
                        pending=pending,
                        decision=decision,
                        sequence=sequence + 1,
                    )
                else:
                    permission_chunks = runtime.resolve_permission(
                        session=session,
                        tool=tool.definition,
                        tool_instance=tool,
                        tool_call=plan_tool_call,
                        sequence=sequence + 1,
                        permission_policy=active_permission_policy,
                    )
        else:
            permission_chunks = runtime.resolve_permission(
                session=session,
                tool=tool.definition,
                tool_instance=tool,
                tool_call=plan_tool_call,
                sequence=sequence + 1,
                permission_policy=active_permission_policy,
            )
        if permission_chunks.chunks:
            session = permission_chunks.chunks[-1].session
        sequence = yield from self._persist_chunks(
            permission_chunks.chunks,
            fallback_sequence=permission_chunks.last_sequence,
        )
        if permission_chunks.pending_approval is not None:
            return "return", session, sequence
        if permission_chunks.denied:
            denied_pending = permission_chunks.denied_approval
            denied_replayed_tool_changed = denied_pending is not None and (
                plan_tool_call.tool_name != denied_pending.tool_name or dict(plan_tool_call.arguments) != denied_pending.arguments
            )
            denied_tool_call = (
                ToolCall(
                    tool_name=denied_pending.tool_name,
                    arguments=dict(denied_pending.arguments),
                    tool_call_id=plan_tool_call.tool_call_id,
                )
                if denied_replayed_tool_changed and denied_pending is not None
                else plan_tool_call
            )
            sequence = yield from self._permission_denied_tool_feedback_chunks(
                session=session,
                tool_call=denied_tool_call,
                pending=denied_pending,
                tool_results=tool_results,
                tool_call_id=tool_call_id,
            )
            if denied_replayed_tool_changed or effective_runtime_config.execution_engine != "provider":
                return "return", session, sequence
            return "continue", session, sequence
        return "ok", session, sequence

    def _run_tool_hook_phase(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_name: str,
        phase: Literal["pre", "post"],
        cancel_message: str,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, str]]:
        hook_outcome = run_tool_hooks_for_session(
            hooks=self._config.hooks,
            workspace=self._workspace,
            session=session,
            sequence=sequence,
            tool_name=tool_name,
            phase=phase,
            recursion_env_var=HOOK_RECURSION_ENV_VAR,
            policy=hook_execution_policy_from_metadata(session.metadata),
        )
        sequence = yield from self._persist_chunks(
            hook_outcome.chunks,
            fallback_sequence=hook_outcome.last_sequence,
        )
        if hook_outcome.failed_error is not None:
            failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                session=session,
                sequence=sequence,
                surface=cast(RuntimeHookSurface, f"{phase}_tool"),
                error=hook_outcome.failed_error,
                hooks=self._config.hooks,
            )
            if failed_chunk is not None:
                persisted_failed, _ = self._persist_chunk(failed_chunk)
                yield persisted_failed
                raise RuntimeError(hook_outcome.failed_error)
        if hook_outcome.action == "cancel":
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=cancel_message,
                    payload={"kind": "hook_cancelled", "surface": f"{phase}_tool"},
                )
            )
            yield failed_chunk
            return sequence, "cancel"
        return sequence, "ok"

    def _execute_tool_and_recover(
        self,
        *,
        session: SessionState,
        sequence: int,
        plan_tool_call: Any,
        tool: Any,
        tool_call_id: str,
        tool_timeout: int | None,
        tool_results: list[ToolResult],
        active_graph_request: GraphRunRequest,
        tool_exception_recovery_enabled: bool,
    ) -> Generator[RuntimeStreamChunk, None, tuple[str, ToolResult | None, SessionState, int]]:
        start_args = dict(plan_tool_call.arguments)
        started_display = build_tool_display(plan_tool_call.tool_name, start_args)
        started_status = build_tool_status(
            plan_tool_call.tool_name,
            tool_call_id,
            phase="running",
            status="running",
            display=started_display,
        )
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_TOOL_STARTED,
            source="runtime",
            payload={
                "tool": plan_tool_call.tool_name,
                "tool_call_id": tool_call_id,
                "display": started_display,
                "tool_status": started_status,
            },
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        if _is_abort_requested(active_graph_request):
            yield from self._started_tool_abort_chunks(
                session=session,
                sequence=sequence,
                tool_call=plan_tool_call,
                tool_call_id=tool_call_id,
                abort_signal=active_graph_request.abort_signal,
            )
            return "returned", None, session, sequence
        try:
            read_tracking = read_tracking_for_tool_results(
                tool_results=tuple(tool_results),
                workspace=self._workspace,
            )
            tool_outcome, sequence = yield from self._invoke_tool(
                tool=tool,
                tool_call=plan_tool_call,
                read_paths=read_tracking.read_paths,
                read_lines=read_tracking.read_lines,
                tool_timeout=tool_timeout,
                session=session,
                start_sequence=sequence + 1,
                tool_call_id=tool_call_id,
                abort_signal=active_graph_request.abort_signal,
                parent_session_id=session.session.parent_id,
                delegation_depth=delegation_depth_from_metadata(session.metadata),
                remaining_spawn_budget=remaining_spawn_budget_from_metadata(session.metadata),
                model=session_model_identity(session.metadata)[0],
            )
            if isinstance(tool_outcome, Exception):
                raise tool_outcome
            tool_result = tool_outcome
        except Exception as exc:
            drained_chunks, session, sequence = self._drain_runtime_events(
                session=session,
                start_sequence=sequence + 1,
            )
            yield from drained_chunks
            if isinstance(exc, RuntimeToolTimeoutError):
                partial_timeout_payload: dict[str, object] = {}
                partial_timeout_content: str | None = None
                partial_timeout_error: str | None = None
                partial_result = getattr(exc, "partial_result", None)
                if isinstance(partial_result, ToolResult):
                    capped_partial = cap_tool_result_output(
                        partial_result,
                        session_id=session.session.id,
                        tool_call_id=tool_call_id,
                    )
                    capped_partial = replace(
                        capped_partial,
                        data=sanitize_tool_result_data(capped_partial.data),
                    )
                    partial_timeout_payload.update(capped_partial.data)
                    partial_timeout_content = capped_partial.content
                    partial_timeout_error = capped_partial.error
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type="runtime.tool_timeout",
                    source="runtime",
                    payload={
                        "tool": plan_tool_call.tool_name,
                        "timeout_seconds": tool_timeout,
                    },
                )
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                timeout_sanitized_args = sanitize_tool_arguments(dict(plan_tool_call.arguments))
                failed_display = build_tool_display(plan_tool_call.tool_name, timeout_sanitized_args)
                failed_status = build_tool_status(
                    plan_tool_call.tool_name,
                    tool_call_id,
                    phase="failed",
                    status="failed",
                    display=failed_display,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        **_tool_completed_identity_payload(session),
                        **partial_timeout_payload,
                        "tool": plan_tool_call.tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": timeout_sanitized_args,
                        "status": "error",
                        "content": partial_timeout_content,
                        **_tool_error_payload(
                            tool_name=plan_tool_call.tool_name,
                            error=partial_timeout_error or str(exc),
                            error_kind="tool_timeout",
                            extra_details={
                                "timed_out": True,
                                "timeout_seconds": tool_timeout,
                            },
                        ),
                        "display": failed_display,
                        "tool_status": failed_status,
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                failed_chunk, _ = self._persist_chunk(chunk_builders.failed_chunk(session=session, sequence=sequence + 1, error=str(exc)))
                yield failed_chunk
                return "returned", None, session, sequence
            if not tool_exception_recovery_enabled and not _is_tool_timeout_like_exception(exc):
                error_sanitized_args = sanitize_tool_arguments(dict(plan_tool_call.arguments))
                failed_display = build_tool_display(plan_tool_call.tool_name, error_sanitized_args)
                failed_status = build_tool_status(
                    plan_tool_call.tool_name,
                    tool_call_id,
                    phase="failed",
                    status="failed",
                    display=failed_display,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload={
                        **_tool_completed_identity_payload(session),
                        "tool": plan_tool_call.tool_name,
                        "tool_call_id": tool_call_id,
                        "arguments": error_sanitized_args,
                        "status": "error",
                        "content": _tool_error_content(plan_tool_call.tool_name, str(exc)),
                        **_tool_error_payload(
                            tool_name=plan_tool_call.tool_name,
                            error=str(exc),
                        ),
                        "display": failed_display,
                        "tool_status": failed_status,
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                failed_chunk, _ = self._persist_chunk(chunk_builders.failed_chunk(session=session, sequence=sequence + 1, error=str(exc)))
                yield failed_chunk
                raise
            error_summary = _tool_error_summary(str(exc))
            error_details = _tool_error_details(
                tool_name=plan_tool_call.tool_name,
                error=str(exc),
            )
            retry_guidance = _tool_error_retry_guidance(str(exc))
            error_kind: str | None = None
            if isinstance(exc, ToolDiagnosticError):
                error_kind = exc.error_kind
                error_details = _tool_error_details(
                    tool_name=plan_tool_call.tool_name,
                    error=str(exc),
                    error_kind=exc.error_kind,
                    extra=exc.error_details,
                )
                retry_guidance = exc.retry_guidance

            tool_result = ToolResult(
                tool_name=plan_tool_call.tool_name,
                status="error",
                content=_tool_error_content(plan_tool_call.tool_name, str(exc)),
                error=str(exc),
                data={
                    "tool_call_id": tool_call_id,
                    "arguments": dict(plan_tool_call.arguments),
                },
                error_kind=error_kind,
                error_summary=error_summary,
                error_details=error_details,
                retry_guidance=retry_guidance,
            )
        return "ok", tool_result, session, sequence

    def _finalize_tool_result(
        self,
        *,
        session: SessionState,
        sequence: int,
        plan_tool_call: Any,
        tool_call_id: str,
        tool_result: ToolResult,
        active_graph_request: GraphRunRequest,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolResult, bool, dict[str, object], SessionState, int, bool]]:
        tool_result, duplicate_todo_write, runtime_tool_result_data = _normalized_tool_result(
            tool_result=tool_result,
            session=session,
            plan_tool_call=plan_tool_call,
            sequence=sequence,
            tool_call_id=tool_call_id,
        )
        drained_chunks, session, _ = self._drain_runtime_events(
            session=session,
            start_sequence=sequence + 1,
        )
        yield from drained_chunks

        # Terminal-seal guard for tool-result delivery: if the run was
        # interrupted while the tool was in flight, this result arrived
        # after the run was sealed and is a late event — drop it instead of
        # persisting ``runtime.tool_completed``. The failure chunk below
        # records the interruption as the terminal truth. (The
        # ``_started_tool_abort_chunks`` path still synthesizes a terminal
        # ``runtime.tool_completed`` for tools that never ran — that is the
        # loop's own bookkeeping, not a late delivery.)
        if _is_abort_requested(active_graph_request):
            yield from self._emit_interrupted_failure(
                session=session,
                sequence=sequence,
                active_graph_request=active_graph_request,
            )
            return tool_result, duplicate_todo_write, runtime_tool_result_data, session, sequence, True
        return tool_result, duplicate_todo_write, runtime_tool_result_data, session, sequence, False

    def _handle_question_outcome(
        self,
        *,
        session: SessionState,
        sequence: int,
        plan_tool_call: Any,
        tool_result: ToolResult,
    ) -> Generator[RuntimeStreamChunk, None, bool]:
        if plan_tool_call.tool_name == QuestionTool.definition.name and tool_result.status == "ok":
            pending_question = PendingQuestion(
                request_id=f"question-{uuid4().hex}",
                tool_name=plan_tool_call.tool_name,
                arguments=dict(plan_tool_call.arguments),
                prompts=QuestionTool.parse_prompts(plan_tool_call.arguments),
            )
            waiting_session = session_with_plan_state(
                SessionState(
                    session=session.session,
                    status="waiting",
                    turn=session.turn,
                    metadata=session.metadata,
                ),
                status="waiting_question",
                blocked_tool=pending_question.tool_name,
            )
            envelope = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_QUESTION_REQUESTED,
                source="runtime",
                payload={
                    "request_id": pending_question.request_id,
                    "tool": pending_question.tool_name,
                    "question_count": len(pending_question.prompts),
                    "questions": [
                        {
                            "header": prompt.header,
                            "question": prompt.question,
                            "multiple": prompt.multiple,
                            "options": [
                                {
                                    "label": option.label,
                                    "description": option.description,
                                }
                                for option in prompt.options
                            ],
                        }
                        for prompt in pending_question.prompts
                    ],
                },
            )
            yield RuntimeStreamChunk(kind="event", session=waiting_session, event=envelope)
            return True
        return False

    def _emit_tool_completed_events(
        self,
        *,
        session: SessionState,
        sequence: int,
        plan_tool_call: Any,
        tool_call_id: str,
        sanitized_arguments: dict[str, object],
        tool_result: ToolResult,
        runtime_tool_result_data: dict[str, object],
        duplicate_todo_write: bool,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, SessionState]]:
        completed_payload = _tool_completed_payload(
            session=session,
            tool_result=tool_result,
            tool_call_id=tool_call_id,
            sanitized_arguments=sanitized_arguments,
        )
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_completed",
            source="tool",
            payload=completed_payload,
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        if plan_tool_call.tool_name == "skill" and tool_result.status == "ok":
            skill_payload = completed_payload.get("skill")
            if isinstance(skill_payload, dict):
                typed_skill_payload = cast(dict[str, object], skill_payload)
                skill_name: object | None = typed_skill_payload.get("name")
                skill_source_path: object | None = typed_skill_payload.get("source_path")
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_SKILL_LOADED,
                    source="runtime",
                    payload={
                        "name": skill_name if isinstance(skill_name, str) else None,
                        "source": "tool",
                        "source_path": (skill_source_path if isinstance(skill_source_path, str) else None),
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        if plan_tool_call.tool_name == "todo_write" and tool_result.status == "ok":
            revision = sequence + 1
            raw_todos = runtime_tool_result_data.get("todos")
            if not duplicate_todo_write:
                session, todo_payload = session_with_todo_state(
                    session,
                    raw_todos=raw_todos,
                    revision=revision,
                )
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_TODO_UPDATED,
                    source="runtime",
                    payload=todo_payload,
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return sequence, session

    def _dispatch_error_feedback_chunks(
        self,
        *,
        session: SessionState,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, object],
        tool_results: list[ToolResult],
        error: str,
        error_kind: str,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        """Emit a failed ``runtime.tool_completed`` and append an error result.

        Tool-level feedback for dispatched tools: the run continues after an
        unknown, denied, or hook-cancelled dispatch instead of failing the
        session.
        """
        sanitized_arguments = sanitize_tool_arguments(dict(arguments))
        tool_result = ToolResult(
            tool_name=tool_name,
            status="error",
            content=_tool_error_content(tool_name, error),
            error=error,
            data={
                "tool_call_id": tool_call_id,
                "arguments": sanitized_arguments,
            },
            error_kind=error_kind,
            error_summary=_tool_error_summary(error),
            error_details=_tool_error_details(
                tool_name=tool_name,
                error=error,
                error_kind=error_kind,
            ),
            retry_guidance="Check the tool name and arguments, then retry.",
        )
        completed_display = build_tool_display(
            tool_name,
            sanitized_arguments,
            result_data=tool_result.data,
        )
        completed_status = build_tool_status(
            tool_name,
            tool_call_id,
            phase="failed",
            status="failed",
            display=completed_display,
        )
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_completed",
            source="tool",
            payload={
                **_tool_completed_identity_payload(session),
                **tool_result.data,
                "tool": tool_result.tool_name,
                "tool_call_id": tool_call_id,
                "arguments": sanitized_arguments,
                "status": tool_result.status,
                "content": tool_result.content,
                "error": tool_result.error,
                "error_kind": tool_result.error_kind,
                "error_summary": tool_result.error_summary,
                "error_details": tool_result.error_details,
                "retry_guidance": tool_result.retry_guidance,
                "display": completed_display,
                "tool_status": completed_status,
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        tool_results.append(
            replace(
                tool_result,
                data={
                    **tool_result.data,
                    "tool_call_id": tool_call_id,
                    "arguments": sanitized_arguments,
                },
            )
        )
        return envelope.sequence

    def _execute_invoked_tool(
        self,
        *,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        outer_call: ToolCall,
        outer_call_id: str,
        tool_results: list[ToolResult],
        permission_policy: PermissionPolicy | None,
        abort_signal: ProviderAbortSignal | None,
    ) -> Generator[RuntimeStreamChunk]:
        """Execute an ``invoke_tool(name, arguments)`` dispatch call.

        The inner tool is resolved from the runtime registry and executed
        through the same boundary as a provider-native tool call: delegation
        policy, runtime policy (allowlist / read-only), permission resolution
        (approval pause for mutating tools), pre/post hooks, and the shared
        tool executor. Tool-level failures (unknown name, denied, cancelled)
        never terminate the run.
        """
        runtime = self._surface
        try:
            parsed = InvokeToolArgs.model_validate(dict(outer_call.arguments))
        except ValidationError as exc:
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name="invoke_tool",
                tool_call_id=outer_call_id,
                arguments=dict(outer_call.arguments),
                tool_results=tool_results,
                error=format_validation_error("invoke_tool", exc),
                error_kind="invalid_arguments",
            )
            return

        inner_name = parsed.name
        inner_arguments = dict(parsed.arguments or {})

        delegation_policy_error = runtime.delegation_tool_policy_error(
            session=session,
            tool_name=inner_name,
        )
        if delegation_policy_error is not None:
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                tool_results=tool_results,
                error=delegation_policy_error,
                error_kind="delegation_policy_denied",
            )
            return

        tool_policy_denial = runtime.tool_policy_denial(
            session=session,
            tool_name=inner_name,
        )
        if tool_policy_denial is not None:
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                tool_results=tool_results,
                error=tool_policy_error(tool_policy_denial),
                error_kind="runtime_tool_policy_denied",
            )
            return

        try:
            tool = tool_registry.resolve(inner_name)
        except Exception as exc:
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                tool_results=tool_results,
                error=f"unknown tool: {inner_name} ({exc})",
                error_kind="unknown_tool",
            )
            return

        inner_call = ToolCall(
            tool_name=inner_name,
            arguments=inner_arguments,
            tool_call_id=outer_call_id,
        )

        # Record the inner request before permission resolution: the approval
        # resume path recovers the provider-visible tool_call_id from this
        # event so the resumed tool result pairs with the pending invoke_tool
        # call in the provider history.
        tool_request_payload: dict[str, object] = {
            "tool": inner_name,
            "arguments": dict(inner_arguments),
        }
        if outer_call_id is not None:
            tool_request_payload["tool_call_id"] = outer_call_id
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="graph.tool_request_created",
            source="graph",
            payload=tool_request_payload,
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        lookup_envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_lookup_succeeded",
            source="runtime",
            payload={"tool": inner_name},
        )
        sequence = lookup_envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=lookup_envelope)

        permission_chunks = runtime.resolve_permission(
            session=session,
            tool=tool.definition,
            tool_instance=tool,
            tool_call=inner_call,
            sequence=sequence + 1,
            permission_policy=permission_policy or self._permission_policy,
        )
        if permission_chunks.chunks:
            session = permission_chunks.chunks[-1].session
        sequence = yield from self._persist_chunks(
            permission_chunks.chunks,
            fallback_sequence=permission_chunks.last_sequence,
        )
        if permission_chunks.pending_approval is not None:
            # Pause for approval. The approval resume path executes the inner
            # tool directly through execute_approved_tool_call (same governed
            # boundary), pairing its result with the recorded tool_call_id.
            return
        if permission_chunks.denied:
            yield from self._permission_denied_tool_feedback_chunks(
                session=session,
                tool_call=inner_call,
                pending=permission_chunks.denied_approval,
                tool_results=tool_results,
                tool_call_id=outer_call_id,
            )
            return

        pre_hook_outcome = run_tool_hooks_for_session(
            hooks=self._config.hooks,
            workspace=self._workspace,
            session=session,
            sequence=sequence,
            tool_name=inner_name,
            phase="pre",
            recursion_env_var=HOOK_RECURSION_ENV_VAR,
            policy=hook_execution_policy_from_metadata(session.metadata),
        )
        sequence = yield from self._persist_chunks(
            pre_hook_outcome.chunks,
            fallback_sequence=pre_hook_outcome.last_sequence,
        )
        if pre_hook_outcome.failed_error is not None:
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                tool_results=tool_results,
                error=pre_hook_outcome.failed_error,
                error_kind="hook_failed",
            )
            return
        if pre_hook_outcome.action == "cancel":
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                tool_results=tool_results,
                error="run cancelled by pre-tool hook",
                error_kind="hook_cancelled",
            )
            return

        tool_timeout = runtime.effective_runtime_config_from_metadata(session.metadata).tool_timeout_seconds
        start_args = dict(inner_arguments)
        started_display = build_tool_display(inner_name, start_args)
        started_status = build_tool_status(
            inner_name,
            outer_call_id,
            phase="running",
            status="running",
            display=started_display,
        )
        execution_intent = ToolExecutionIntent.from_call(
            inner_call,
            tool.definition,
            tool_call_id=outer_call_id or f"runtime-tool-{uuid4().hex}",
        )
        persist_tool_execution_intent(self._session_store, self._workspace, session, execution_intent.metadata_payload())
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_TOOL_STARTED,
            source="runtime",
            payload={
                "tool": inner_name,
                "tool_call_id": outer_call_id,
                "display": started_display,
                "tool_status": started_status,
            },
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        if _is_abort_signal_requested(abort_signal):
            yield from self._started_tool_abort_chunks(
                session=session,
                sequence=sequence,
                tool_call=inner_call,
                tool_call_id=outer_call_id,
                abort_signal=abort_signal,
            )
            return

        try:
            read_tracking = read_tracking_for_tool_results(
                tool_results=tuple(tool_results),
                workspace=self._workspace,
            )
            tool_outcome, sequence = yield from self._invoke_tool(
                tool=tool,
                tool_call=inner_call,
                read_paths=read_tracking.read_paths,
                read_lines=read_tracking.read_lines,
                tool_timeout=tool_timeout,
                session=session,
                start_sequence=sequence + 1,
                tool_call_id=outer_call_id,
                abort_signal=abort_signal,
                parent_session_id=session.session.parent_id,
                delegation_depth=delegation_depth_from_metadata(session.metadata),
                remaining_spawn_budget=remaining_spawn_budget_from_metadata(session.metadata),
                model=session_model_identity(session.metadata)[0],
            )
            if isinstance(tool_outcome, Exception):
                raise tool_outcome
            tool_result = tool_outcome
        except RuntimeToolTimeoutError:
            envelope = self._persist_event(
                session_id=session.session.id,
                event_type="runtime.tool_timeout",
                source="runtime",
                payload={
                    "tool": inner_name,
                    "timeout_seconds": tool_timeout,
                },
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                tool_results=tool_results,
                error=f"tool '{inner_name}' exceeded runtime timeout of {tool_timeout}s",
                error_kind="tool_timeout",
            )
            return
        except Exception as exc:
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                tool_results=tool_results,
                error=str(exc),
                error_kind="tool_error",
            )
            return

        sanitized_arguments = sanitize_tool_arguments(dict(inner_call.arguments))
        tool_result = cap_tool_result_output(
            tool_result,
            session_id=session.session.id,
            tool_call_id=outer_call_id,
        )
        tool_result = replace(
            tool_result,
            data=sanitize_tool_result_data(tool_result.data),
        )

        drained_chunks, session, _ = self._drain_runtime_events(
            session=session,
            start_sequence=sequence + 1,
        )
        yield from drained_chunks

        if _is_abort_signal_requested(abort_signal):
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload=chunk_builders.user_interrupted_payload(
                        run_id=run_id_from_session_metadata(session.metadata),
                        reason=_abort_signal_reason(abort_signal),
                    ),
                    status="interrupted",
                )
            )
            yield failed_chunk
            return

        completed_payload = {
            **_tool_completed_identity_payload(session),
            **tool_result.data,
            "tool_call_id": outer_call_id,
            "arguments": sanitized_arguments,
            "status": tool_result.status,
            "content": tool_result.content,
            "error": tool_result.error,
        }
        if tool_result.error_kind is not None:
            completed_payload["error_kind"] = tool_result.error_kind
        if tool_result.error_summary is not None:
            completed_payload["error_summary"] = tool_result.error_summary
        if tool_result.error_details is not None:
            completed_payload["error_details"] = tool_result.error_details
        if tool_result.retry_guidance is not None:
            completed_payload["retry_guidance"] = tool_result.retry_guidance
        completed_payload.setdefault("tool", tool_result.tool_name)

        completed_display = build_tool_display(
            inner_name,
            sanitized_arguments,
            result_data=tool_result.data,
        )
        completed_status = build_tool_status(
            inner_name,
            outer_call_id,
            phase="completed" if tool_result.status == "ok" else "failed",
            status="completed" if tool_result.status == "ok" else "failed",
            display=completed_display,
        )
        completed_payload["display"] = completed_display
        completed_payload["tool_status"] = completed_status

        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_completed",
            source="tool",
            payload=completed_payload,
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        clear_tool_execution_intent(self._session_store, self._workspace, session)

        if _is_abort_signal_requested(abort_signal):
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload=chunk_builders.user_interrupted_payload(
                        run_id=run_id_from_session_metadata(session.metadata),
                        reason=_abort_signal_reason(abort_signal),
                    ),
                    status="interrupted",
                )
            )
            yield failed_chunk
            return

        if tool_result.status == "ok":
            post_hook_outcome = run_tool_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=session,
                sequence=sequence,
                tool_name=inner_name,
                phase="post",
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(session.metadata),
            )
            sequence = yield from self._persist_chunks(
                post_hook_outcome.chunks,
                fallback_sequence=post_hook_outcome.last_sequence,
            )
            if post_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=session,
                    sequence=sequence,
                    surface="post_tool",
                    error=post_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    persisted_failed, _ = self._persist_chunk(failed_chunk)
                    yield persisted_failed
                    raise RuntimeError(post_hook_outcome.failed_error)
            if post_hook_outcome.action == "cancel":
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error="run cancelled by post-tool hook",
                        payload={"kind": "hook_cancelled", "surface": "post_tool"},
                    )
                )
                yield failed_chunk
                return

        tool_results.append(
            replace(
                tool_result,
                data={
                    **tool_result.data,
                    "tool_call_id": outer_call_id,
                    "arguments": sanitized_arguments,
                },
            )
        )

    def _permission_denied_tool_feedback_chunks(
        self,
        *,
        session: SessionState,
        tool_call: ToolCall,
        pending: PendingApproval | None,
        tool_results: list[ToolResult],
        tool_call_id: str | None = None,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        tool_feedback_id = tool_call_id or tool_call.tool_call_id or f"runtime-tool-{uuid4().hex}"
        sanitized_arguments = sanitize_tool_arguments(dict(tool_call.arguments))
        error = f"permission denied for tool: {tool_call.tool_name}"
        result_data: dict[str, object] = {
            "tool_call_id": tool_feedback_id,
            "arguments": sanitized_arguments,
            "permission_denied": True,
        }
        if pending is not None:
            result_data["approval_request_id"] = pending.request_id
            result_data["approval_decision"] = "deny"
            if pending.path_scope is not None:
                result_data["path_scope"] = pending.path_scope
            if pending.operation_class is not None:
                result_data["operation_class"] = pending.operation_class
            if pending.canonical_path is not None:
                result_data["canonical_path"] = pending.canonical_path
            if pending.matched_rule is not None:
                result_data["matched_rule"] = pending.matched_rule
            if pending.policy_surface is not None:
                result_data["policy_surface"] = pending.policy_surface

        denied_by: str | None = None
        if pending is not None and pending.policy_mode == "ask":
            denied_by = "user"
            result_data["denied_by"] = denied_by

        tool_result = ToolResult(
            tool_name=tool_call.tool_name,
            status="error",
            content=_tool_error_content(tool_call.tool_name, error),
            error=error,
            data=sanitize_tool_result_data(result_data),
            error_kind="permission_denied",
            error_summary=_tool_error_summary(error),
            error_details=_tool_error_details(
                tool_name=tool_call.tool_name,
                error=error,
                error_kind="permission_denied",
                extra={
                    "permission_denied": True,
                    **({"denied_by": denied_by} if denied_by is not None else {}),
                },
            ),
            retry_guidance="Adjust the request or approval settings, then retry.",
        )
        completed_display = build_tool_display(
            tool_call.tool_name,
            sanitized_arguments,
            result_data=tool_result.data,
        )
        completed_status = build_tool_status(
            tool_call.tool_name,
            tool_feedback_id,
            phase="failed",
            status="failed",
            display=completed_display,
        )
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_completed",
            source="tool",
            payload={
                **_tool_completed_identity_payload(session),
                **tool_result.data,
                "tool": tool_result.tool_name,
                "tool_call_id": tool_feedback_id,
                "arguments": sanitized_arguments,
                "status": tool_result.status,
                "content": tool_result.content,
                "error": tool_result.error,
                "error_kind": tool_result.error_kind,
                "error_summary": tool_result.error_summary,
                "error_details": tool_result.error_details,
                "retry_guidance": tool_result.retry_guidance,
                "display": completed_display,
                "tool_status": completed_status,
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        tool_results.append(
            replace(
                tool_result,
                data={
                    **tool_result.data,
                    "tool_call_id": tool_feedback_id,
                    "arguments": sanitized_arguments,
                },
            )
        )
        return envelope.sequence

    @staticmethod
    def _current_session_state(session: SessionState) -> SessionState:
        return session

    @staticmethod
    def _current_run_id(session: SessionState) -> str | None:
        run_id = runtime_state_run_id(session.metadata)
        return run_id if run_id else None

    @staticmethod
    def _current_provider_attempt(session: SessionState) -> int:
        raw_provider_attempt = session.metadata.get("provider_attempt", 0)
        if isinstance(raw_provider_attempt, int) and not isinstance(raw_provider_attempt, bool):
            return raw_provider_attempt
        return 0

    @staticmethod
    def _build_context_compacted_payload(
        context_window: RuntimeContextWindow,
    ) -> dict[str, object] | None:
        if not context_window.compacted:
            return None
        token_metadata: dict[str, object] = {}
        if context_window.token_budget is not None:
            token_metadata = {
                "original_tool_result_tokens": context_window.original_tool_result_tokens,
                "retained_tool_result_tokens": context_window.retained_tool_result_tokens,
                "dropped_tool_result_tokens": context_window.dropped_tool_result_tokens,
                "token_budget": context_window.token_budget,
                "token_estimate_source": context_window.token_estimate_source,
            }
        return {
            "reason": context_window.compaction_reason,
            "original_tool_result_count": context_window.original_tool_result_count,
            "retained_tool_result_count": context_window.retained_tool_result_count,
            **token_metadata,
            "compacted": True,
            "summary_anchor": context_window.summary_anchor,
            "projection_id": context_window.summary_anchor,
            "summary_source": context_window.summary_source,
            "summary_strategy": context_window.summary_strategy,
            "projection": (context_window.continuity_state.metadata_payload() if context_window.continuity_state is not None else None),
        }

    @staticmethod
    def _should_emit_context_compacted(
        *,
        session: SessionState,
        summary_anchor: str | None,
        original_tool_result_count: int,
        retained_tool_result_count: int,
    ) -> bool:
        current_run_id = runtime_state_run_id(session.metadata)
        memory_state = runtime_state_context_compacted(session.metadata) or {}
        last_run_id_raw = memory_state.get("last_emitted_run_id")
        last_run_id = last_run_id_raw if isinstance(last_run_id_raw, str) else None
        if current_run_id is not None and last_run_id is not None and current_run_id != last_run_id:
            return True
        if summary_anchor is not None and memory_state.get("last_summary_anchor") == summary_anchor:
            return False
        return not (
            memory_state.get("last_original_tool_result_count") == original_tool_result_count
            and memory_state.get("last_retained_tool_result_count") == retained_tool_result_count
        )

    @staticmethod
    def _is_stuck_tool_loop(*, turn: int, tool_results: list[ToolResult]) -> bool:
        if turn < _STUCK_DETECTED_MIN_TURN:
            return False
        if len(tool_results) < _STUCK_DETECTED_MIN_TOOL_RESULTS:
            return False
        return len({result.tool_name for result in tool_results}) <= 2

    @staticmethod
    def _at_safe_boundary(graph: RuntimeGraph) -> bool:
        is_at_safe_boundary = getattr(graph, "is_at_safe_boundary", None)
        return callable(is_at_safe_boundary) and bool(is_at_safe_boundary())

    def _drain_runtime_events(
        self,
        *,
        session: SessionState,
        start_sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        emitted: list[RuntimeStreamChunk] = []
        sequence = start_sequence - 1
        current_session: SessionState = session
        for acp_event in envelopes_for_acp_events(
            session_id=session.session.id,
            start_sequence=start_sequence,
            acp_events=self._acp_adapter.drain_events(),
        ):
            current_session = session_with_current_acp_metadata(current_session, self._acp_adapter.current_state())
            envelope = self._persist_event(
                session_id=acp_event.session_id,
                event_type=acp_event.event_type,
                source=acp_event.source,
                payload=acp_event.payload,
            )
            sequence = envelope.sequence
            emitted.append(RuntimeStreamChunk(kind="event", session=current_session, event=envelope))
        for mcp_event in envelopes_for_mcp_events(
            session_id=session.session.id,
            start_sequence=sequence + 1,
            mcp_events=self._mcp_manager.drain_events(),
        ):
            envelope = self._persist_event(
                session_id=mcp_event.session_id,
                event_type=mcp_event.event_type,
                source=mcp_event.source,
                payload=mcp_event.payload,
            )
            sequence = envelope.sequence
            emitted.append(RuntimeStreamChunk(kind="event", session=current_session, event=envelope))
        for lsp_event in envelopes_for_lsp_events(
            session_id=session.session.id,
            start_sequence=sequence + 1,
            lsp_events=self._lsp_manager.drain_events(),
        ):
            envelope = self._persist_event(
                session_id=lsp_event.session_id,
                event_type=lsp_event.event_type,
                source=lsp_event.source,
                payload=lsp_event.payload,
            )
            sequence = envelope.sequence
            emitted.append(RuntimeStreamChunk(kind="event", session=current_session, event=envelope))
        return tuple(emitted), current_session, sequence
