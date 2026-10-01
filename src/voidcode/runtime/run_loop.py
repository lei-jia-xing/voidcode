from __future__ import annotations

import logging
import time
from collections.abc import Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast
from uuid import uuid4

from pydantic import ValidationError

from ..core.todos import TodoPhase
from ..core.tool_context import ToolContext
from ..core.transcript import AssembledContext, ContextSegment, ToolResultView
from ..graph.contracts import GraphEvent, GraphRunRequest, GraphStep, RuntimeGraph, SafeBoundaryGraph, StreamableGraph
from ..hook.config import RuntimeHookSurface
from ..hook.typed import (
    ToolInputEvent,
    ToolInputHandlerRegistry,
    ToolInputHookOutcome,
    tool_input_arguments_sha256,
    tool_input_rewrite_metadata,
    validate_tool_input_schema,
)
from ..provider.errors import (
    ProviderContextLimitError,
    ProviderExecutionError,
    classify_provider_error,
)
from ..provider.protocol import (
    ProviderAbortSignal,
)
from ..tools._pydantic_args import format_validation_error
from ..tools._repair import ToolDiagnosticError
from ..tools.contracts import (
    RuntimeToolTimeoutError,
    Tool,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolInvocation,
    ToolResult,
    is_read_tier,
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
from .context.continuity import replayed_conversation_segments_from_segments
from .context.transforms import context_transform_applied_payloads
from .context.window import (
    BeforeCompactInput,
    ContextProjection,
    ContinuitySummaryKind,
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
    RUNTIME_PROVIDER_CONTEXT_POLICY,
    RUNTIME_PROVIDER_CONTEXT_RECOVERY,
    RUNTIME_PROVIDER_FALLBACK,
    RUNTIME_PROVIDER_TRANSIENT_RETRY,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_REASONING_PART,
    RUNTIME_REMINDER_INJECTED,
    RUNTIME_SKILL_LOADED,
    RUNTIME_TODO_UPDATED,
    RUNTIME_TOOL_INPUT_PROCESSED,
    RUNTIME_TOOL_PROGRESS,
    RUNTIME_TOOL_STARTED,
    RUNTIME_TOOL_TIMEOUT,
    EventEnvelope,
    EventSource,
    runtime_reasoning_part_from_provider_stream,
    runtime_reasoning_part_payload,
)
from .execution.graph_adapter import graph_request_for_session, graph_session_snapshot
from .execution.provider_execution_metadata import (
    provider_attempt_from_metadata,
    provider_retry_attempt_from_metadata,
    run_id_from_session_metadata,
    session_with_provider_usage_metadata,
)
from .execution.provider_fallback import (
    ProviderFallbackDecision,
    ProviderTerminalDecision,
    ProviderTransientRetryDecision,
    decide_provider_error_policy,
    provider_transient_retry_config,
)
from .execution.seams import (
    ContextLimitPromotion,
    RuntimeGraphSelection,
    context_limit_promotion_for_provider_error,
    fallback_graph_for_provider_error,
    select_graph_for_effective_config,
)
from .execution.tool_replay import ToolExecutionIntent
from .execution.tool_result_projection import (
    _fit_numbered_progress_payload,
    _is_terminal_yield_result,
    _normalized_tool_result,
    _progress_payload_size,
    _serialized_tool_results,
    _tool_completed_identity_payload,
    _tool_completed_payload,
    _tool_error_details,
    _tool_error_diagnostics,  # noqa: F401 — preserve run_loop private import compatibility
    _tool_error_payload,
    _tool_error_retry_guidance,
    _tool_error_summary,
)
from .hook_runtime import (
    HOOK_RECURSION_ENV_VAR,
    before_compact_input_from_hook_outcome,
    hook_blocked_reason,
    hook_execution_policy_from_metadata,
    run_lifecycle_hooks_for_session,
    run_tool_hooks_for_session,
)
from .mode import runtime_mode_from_metadata, runtime_read_only_from_metadata
from .permission import PendingApproval, PermissionPolicy, PermissionResolution
from .question import PendingQuestion
from .reminders import (
    TODO_MID_RUN_KIND,
    TODO_REMINDER_KIND,
    ReminderSuppression,
    decide_todo_mid_run_nudge,
    decide_todo_reminder,
    incomplete_todo_phases,
    todo_mid_run_segment,
    todo_mid_run_state_from_metadata,
    todo_mutation_count,
    todo_reminder_segment,
    todo_reminder_state_from_metadata,
)
from .session import SessionState, SessionStatus
from .session_metadata_helpers import (
    clear_tool_execution_intent,
    delegation_depth_from_metadata,
    parse_plan_state_metadata,
    persist_tool_execution_intent,
    remaining_spawn_budget_from_metadata,
    runtime_state_context_compacted,
    runtime_state_context_transform_applied,
    runtime_state_run_id,
    session_metadata_with_runtime_state_updates,
    session_model_identity,
    session_with_context_compacted_state,
    session_with_context_transform_applied_state,
    session_with_context_window_metadata,
    session_with_context_window_payload_metadata,
    session_with_current_acp_metadata,
    session_with_plan_state,
    session_with_reminder_state,
    session_with_todo_state,
)
from .skill_metadata import skill_snapshot_from_metadata
from .storage import SessionStore
from .todos import runtime_todo_phases_from_payload, todo_state_from_session_metadata
from .tool_call_preview import PREVIEW_SNAPSHOT_MAX_BYTES, WRITE_PREVIEW_TOOLS, build_partial_tool_call_preview, build_tool_call_preview
from .tool_display import build_tool_display, build_tool_status
from .tool_execution import RuntimeToolExecutor
from .tool_scope import tool_policy_error

if TYPE_CHECKING:
    from .acp import AcpAdapter
    from .lsp import LspManager
    from .mcp import McpManager
    from .provider_catalog_query import RuntimeProviderCatalogQuery
    from .runtime_surface import RuntimeSurface
    from .tool_registry import ToolRegistry

from .execution import chunk_builders

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
    graph_step: GraphStep,
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
        graph_step.provider_usage,
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


def _terminal_provider_error_payload(
    payload: dict[str, object],
    *,
    recovery: _ContextLimitRecoveryState,
    error: ProviderExecutionError,
) -> dict[str, object]:
    """Terminal provider-error payload, naming the way out of a context-limit failure.

    A ``context_limit`` failure stays resumable, so the payload must say what the
    remaining lever is instead of only reporting that the call failed.
    """
    if error.kind != "context_limit":
        return payload
    outcome = "prune" if recovery.pruned else "unavailable"
    return {
        **payload,
        "context_limit_recovery": outcome,
        "resumable": True,
        "guidance": (
            "Provider rejected the request as over its context window"
            + (" after bounded pruning." if recovery.pruned else "; nothing was prunable.")
            + " Reduce the session context or configure a larger-window model, then resume this"
            " session (voidcode sessions resume): the same turn is retried."
        ),
    }


def _runtime_waits_for_user(session: SessionState) -> bool:
    """Whether the session already parked on an answer the runtime is waiting for.

    Approval and question waits pause the loop for a user decision, so a
    "keep working" nudge would contradict the pending state the same turn
    already renders (``context/window.py::_pending_state_segment``).
    """
    raw_plan_state = session.metadata.get("plan_state")
    if raw_plan_state is None:
        return False
    return parse_plan_state_metadata(raw_plan_state).get("status") in {"waiting_approval", "waiting_question"}


def _replayed_conversation_segments(
    request: GraphRunRequest,
) -> tuple[ContextSegment, ...]:
    assembled_context = request.assembled_context
    return replayed_conversation_segments_from_segments(assembled_context.segments)


def _graph_request_without_provider_attempt(
    request: GraphRunRequest,
    *,
    session: SessionState,
) -> GraphRunRequest:
    return graph_request_for_session(
        GraphRunRequest(
            session=graph_session_snapshot(session),
            prompt=request.prompt,
            available_tools=request.available_tools,
            context_window=request.context_window,
            assembled_context=request.assembled_context,
            metadata=_metadata_without_provider_attempt(request.metadata),
            abort_signal=request.abort_signal,
            tool_call_preview=request.tool_call_preview,
            run_step=request.run_step,
        ),
        session,
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


@dataclass(slots=True)
class _ContextLimitRecoveryState:
    """Run-local context-limit recovery bookkeeping.

    Deliberately not persisted: recovery is a property of the in-flight turn, and
    a resumed session must be allowed to try its own recovery once more (the
    persisted session metadata owns nothing about retries).
    """

    #: The local pruning lever ran (whether or not it reclaimed anything): the
    #: escalation still happens when nothing was prunable.
    prune_attempted: bool = False
    #: The local pruning lever actually shrank the view.
    pruned: bool = False
    promoted: bool = False


@dataclass(frozen=True, slots=True)
class _ProviderAttemptReset:
    provider_attempt: int
    graph: RuntimeGraph
    graph_request: GraphRunRequest
    session: SessionState


@dataclass(frozen=True, slots=True)
class _ResolvedToolCall:
    """Resolved runtime tool and final call passed to the executor seam.

    Each entry point owns its policy, permission, typed-input, and lifecycle
    preconditions. Once those checks have resolved a tool and call, execution
    must cross this one boundary so every path constructs the same canonical
    :class:`ToolInvocation` and observes the same progress stream.
    """

    tool: Tool
    tool_call: ToolCall
    tool_call_id: str


def _is_tool_timeout_like_exception(exc: Exception) -> bool:
    if isinstance(exc, TimeoutError):
        return True
    message = str(exc).lower()
    return "timeout" in message or "timed out" in message


def _tool_timeout_execution_facts(exc: RuntimeToolTimeoutError) -> dict[str, object]:
    """Additive execution facts every timeout surface must carry.

    ``error_kind='tool_timeout'`` alone reads as "the call failed"; the facts
    say whether the runtime cancelled the invocation, whether it confirmed the
    execution stopped, and therefore what may be claimed about side effects.
    """
    return dict(exc.execution_facts())


def _is_abort_requested(request: GraphRunRequest) -> bool:
    return bool(request.abort_signal is not None and request.abort_signal.cancelled)


def _is_abort_signal_requested(abort_signal: ProviderAbortSignal | None) -> bool:
    return bool(abort_signal is not None and abort_signal.cancelled)


def _abort_signal_reason(abort_signal: ProviderAbortSignal | None) -> str | None:
    if abort_signal is None:
        return None
    return abort_signal.reason or None


def _abort_reason(request: GraphRunRequest) -> str | None:
    return _abort_signal_reason(request.abort_signal)


def _live_event_surfaces_output(event: GraphEvent) -> bool:
    """Whether a live-only graph event renders part of the provider response.

    Only content-bearing deltas and tool-call lifecycle events reach the client as
    assistant output. ``error``/``done`` markers carry no output, so they must not
    block a retry/fallback.
    """
    if event.event_type == "graph.provider_stream":
        if event.payload.get("kind") not in {"delta", "content"}:
            return False
        if event.payload.get("channel") not in {"text", "reasoning"}:
            return False
        text = event.payload.get("text")
        return isinstance(text, str) and bool(text)
    return event.event_type in {"graph.tool_call_start", "graph.tool_call_delta", "graph.tool_call_end"}


class _ProviderErrorPolicyVerdict(TypedDict):
    action: Literal["exit", "reraise", "retry", "fallback"]
    exc: NotRequired[BaseException]
    provider_attempt: NotRequired[int]
    provider_retry_attempt: NotRequired[int]
    graph: NotRequired[RuntimeGraph]
    session: NotRequired[SessionState]
    graph_request: NotRequired[GraphRunRequest]


@dataclass(slots=True)
class _AttemptStreamVisibility:
    """Whether the in-flight provider attempt already surfaced live stream output.

    Live provider deltas are client-only: they are not persisted, so the runtime
    has no transcript record to reconcile them against. When such an attempt is
    restarted (transient retry or provider fallback) the runtime keeps the
    recovery and annotates the retry/fallback event with
    ``discarded_streamed_output`` so the client can drop its in-flight
    projection. ``surfaced`` is reset at the start of every attempt.
    """

    surfaced: bool = False


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
        tool_input_handler_registry: ToolInputHandlerRegistry,
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
        self._tool_input_handler_registry = tool_input_handler_registry
        self._tool_executor = tool_executor
        self._pending_hook_guidance: list[str] = []

    def update_provider_catalog_query(self, provider_catalog_query: RuntimeProviderCatalogQuery) -> None:
        """Publish the provider catalog query used by reasoning diagnostics."""
        self._provider_catalog_query = provider_catalog_query

    def _note_hook_guidance(self, guidance: Iterable[str]) -> None:
        # ponytail: run-local buffer drained at each turn assembly; prompt consumer bounds to 8 items x 2000 chars.
        for item in guidance:
            if isinstance(item, str) and item.strip():
                self._pending_hook_guidance.append(item)

    def _drain_pending_hook_guidance(self) -> tuple[str, ...]:
        drained = tuple(self._pending_hook_guidance)
        del self._pending_hook_guidance[:]
        return drained

    def _tool_call_preview(
        self,
        tool_name: str,
        fragments: tuple[str, ...],
        parsed_arguments: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Build a live-only preview within the runtime workspace boundary."""
        try:
            return build_partial_tool_call_preview(
                workspace=self._workspace,
                tool_name=tool_name,
                argument_text="".join(fragments),
                parsed_arguments=parsed_arguments,
            )
        except Exception:
            return None

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

    def _number_yield_progress(self, *, session: SessionState, tool_result: ToolResult) -> ToolResult:
        if tool_result.tool_name != "yield" or tool_result.status != "ok" or tool_result.data.get("yield_kind") != "progress":
            return tool_result
        raw_progress = tool_result.data.get("progress")
        if not isinstance(raw_progress, Mapping):
            raise ValueError("yield progress payload is missing its bounded progress object")
        # The event log is the sole ordinal/retention authority. Storage
        # failures must propagate rather than silently restarting at ordinal 1.
        stored = self._session_store.load_session(workspace=self._workspace, session_id=session.session.id)
        prior_progress = [
            event
            for event in stored.events
            if event.event_type == "runtime.tool_completed" and event.payload.get("tool") == "yield" and event.payload.get("yield_kind") == "progress"
        ]
        prior_count = len(prior_progress)
        prior_bytes = 0
        for event in prior_progress:
            prior_progress_payload = event.payload.get("progress")
            if isinstance(prior_progress_payload, Mapping):
                prior_bytes += _progress_payload_size(prior_progress_payload)
        from ..tools.yield_tool import YIELD_PROGRESS_MAX_RETAINED_CHARS, YIELD_PROGRESS_MAX_SECTION_CHARS, YIELD_PROGRESS_MAX_SECTIONS

        if prior_count >= YIELD_PROGRESS_MAX_SECTIONS:
            raise ValueError(f"yield progress limit exceeded: at most {YIELD_PROGRESS_MAX_SECTIONS} sections")
        raw_size = _progress_payload_size(raw_progress)
        progress = dict(raw_progress)
        progress["ordinal"] = prior_count + 1
        progress["retained_chars"] = prior_bytes + raw_size
        for _ in range(3):
            progress = _fit_numbered_progress_payload(progress, max_chars=YIELD_PROGRESS_MAX_SECTION_CHARS)
            final_size = _progress_payload_size(progress)
            if prior_bytes + final_size > YIELD_PROGRESS_MAX_RETAINED_CHARS:
                raise ValueError(f"yield progress limit exceeded: retained sections are capped at {YIELD_PROGRESS_MAX_RETAINED_CHARS} characters")
            updated_retained = prior_bytes + final_size
            if progress.get("retained_chars") == updated_retained:
                break
            progress["retained_chars"] = updated_retained
        if _progress_payload_size(progress) > YIELD_PROGRESS_MAX_SECTION_CHARS:
            raise ValueError(f"yield progress section must be at most {YIELD_PROGRESS_MAX_SECTION_CHARS} characters")
        return replace(tool_result, data={**tool_result.data, "progress": progress})

    def _capture_interrupted_checkpoint(
        self,
        *,
        session: SessionState,
        prompt: str,
        tool_results: list[ToolResult],
        last_event_sequence: int,
    ) -> None:
        current_turn_results = [result for result in tool_results if result.source != "replayed_conversation"]
        self._session_store.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=session.session.id,
            prompt=prompt,
            session_metadata=session.metadata,
            tool_results=_serialized_tool_results(current_turn_results),
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

    def _execute_resolved_tool_call(
        self,
        *,
        resolved_call: _ResolvedToolCall,
        read_paths: frozenset[str],
        read_lines: Mapping[str, frozenset[int]],
        tool_timeout: int | None,
        session: SessionState,
        start_sequence: int,
        abort_signal: ProviderAbortSignal | None,
        parent_session_id: str | None,
        delegation_depth: int,
        remaining_spawn_budget: int | None,
        model: str | None = None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolResult | Exception, int]]:
        """Execute one already-resolved call through the canonical tool boundary.

        Policy, permission, typed-input, and lifecycle checks remain owned by
        each caller. This seam begins only after those checks have resolved the
        final tool and arguments, and centralizes immutable invocation context
        construction plus progress persistence for native, approval-resume,
        and ``invoke_tool`` inner calls.
        """
        sequence = start_sequence - 1
        todo_state = todo_state_from_session_metadata(session.metadata)
        todo_phases: tuple[TodoPhase, ...] = runtime_todo_phases_from_payload(todo_state["phases"]) if todo_state is not None else ()
        invocation = ToolInvocation(
            tool_call=resolved_call.tool_call,
            tool_definition=resolved_call.tool.definition,
            context=ToolContext(
                session_id=session.session.id,
                run_id=run_id_from_session_metadata(session.metadata),
                invocation_id=resolved_call.tool_call_id,
                parent_session_id=parent_session_id,
                delegation_depth=delegation_depth,
                remaining_spawn_budget=remaining_spawn_budget,
                read_paths=read_paths,
                read_lines=read_lines,
                todo_phases=todo_phases,
                tool_timeout_seconds=tool_timeout,
                model=model,
                abort_signal=abort_signal,
            ),
        )
        execution = self._tool_executor.invoke(
            tool=resolved_call.tool,
            invocation=invocation,
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
                payload={"tool_call_id": resolved_call.tool_call_id, **progress.payload},
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

    def _persist_resolved_tool_intent(
        self,
        *,
        session: SessionState,
        tool: Tool,
        tool_call: ToolCall,
        tool_call_id: str | None,
    ) -> tuple[ToolCall, str, dict[str, object], SessionState]:
        """Persist the canonical final call before its pre-tool hook when required."""
        canonical_tool_call_id = tool_call_id or tool_call.tool_call_id or f"runtime-tool-{uuid4().hex}"
        canonical_tool_call = replace(tool_call, tool_call_id=canonical_tool_call_id)
        execution_intent = ToolExecutionIntent.from_call(
            canonical_tool_call,
            tool.definition,
            tool_call_id=canonical_tool_call_id,
        )
        intent_payload = execution_intent.metadata_payload()
        session = replace(
            session,
            metadata=session_metadata_with_runtime_state_updates(
                session.metadata,
                updates={"pending_tool_intent": intent_payload},
            ),
        )
        persist_tool_execution_intent(self._session_store, self._workspace, session, intent_payload)
        return canonical_tool_call, canonical_tool_call_id, intent_payload, session

    def _emit_started_tool_event(
        self,
        *,
        session: SessionState,
        tool_call: ToolCall,
        tool_call_id: str,
        execution_intent_payload: dict[str, object] | None = None,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        """Persist and yield one canonical ``runtime.tool_started`` event."""
        sanitized_args = sanitize_tool_arguments(dict(tool_call.arguments))
        started_display = build_tool_display(tool_call.tool_name, sanitized_args)
        started_status = build_tool_status(
            tool_call.tool_name,
            tool_call_id,
            phase="running",
            status="running",
            display=started_display,
        )
        payload: dict[str, object] = {
            "tool": tool_call.tool_name,
            "tool_call_id": tool_call_id,
            "display": started_display,
            "tool_status": started_status,
        }
        if execution_intent_payload is not None:
            payload["execution_intent"] = execution_intent_payload
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_TOOL_STARTED,
            source="runtime",
            payload=payload,
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return envelope.sequence

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
        self._note_hook_guidance(pre_hook_outcome.guidance)
        if pre_hook_outcome.action == "cancel":
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=hook_blocked_reason(pre_hook_outcome, tool_name=tool_call.tool_name),
                    payload={"kind": "hook_cancelled", "surface": "pre_tool"},
                )
            )
            yield failed_chunk
            return

        tool_timeout = runtime.effective_runtime_config_from_metadata(session.metadata).tool_timeout_seconds
        tool_call, tool_call_id, intent_payload, _ = self._persist_resolved_tool_intent(
            session=session,
            tool=tool,
            tool_call=tool_call,
            tool_call_id=tool_call.tool_call_id,
        )
        sequence = yield from self._emit_started_tool_event(
            session=session,
            tool_call=tool_call,
            tool_call_id=tool_call_id,
            execution_intent_payload=intent_payload,
        )

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
            tool_outcome, sequence = yield from self._execute_resolved_tool_call(
                resolved_call=_ResolvedToolCall(
                    tool=tool,
                    tool_call=tool_call,
                    tool_call_id=tool_call_id,
                ),
                read_paths=read_tracking.read_paths,
                read_lines=read_tracking.read_lines,
                tool_timeout=tool_timeout,
                session=session,
                start_sequence=sequence + 1,
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
                partial_result = exc.partial_result
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
                timeout_facts = _tool_timeout_execution_facts(exc)
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_TOOL_TIMEOUT,
                    source="runtime",
                    payload={
                        "tool": tool_call.tool_name,
                        "timeout_seconds": tool_timeout,
                        **timeout_facts,
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
                        **timeout_facts,
                        **_tool_error_payload(
                            tool_name=tool_call.tool_name,
                            error=partial_timeout_error or exc.error_message,
                            error_kind="tool_timeout",
                            extra_details={
                                "timed_out": True,
                                "timeout_seconds": tool_timeout,
                                **timeout_facts,
                            },
                        ),
                        "display": failed_display,
                        "tool_status": failed_status,
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=exc.error_message,
                        payload={
                            "kind": "tool_timeout",
                            "tool": tool_call.tool_name,
                            "timeout_seconds": tool_timeout,
                            **timeout_facts,
                        },
                    )
                )
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
            error_kind: str | None = None
            error_details: dict[str, object] = {}
            retry_guidance: str | None = _tool_error_retry_guidance(str(exc))
            if isinstance(exc, ToolDiagnosticError):
                error_kind = exc.error_kind
                error_details = dict(exc.error_details)
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
                diagnostics=ToolDiagnostics(
                    kind=error_kind,
                    summary=_tool_error_summary(str(exc)),
                    details={"tool_name": tool_call.tool_name, **error_details},
                    guidance=retry_guidance,
                ),
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
        tool_result = self._number_yield_progress(session=session, tool_result=tool_result)
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

        completed_payload = _tool_completed_payload(
            session=session,
            tool_result=tool_result,
            tool_call_id=tool_call_id,
            sanitized_arguments=sanitized_arguments,
            display_tool_name=tool_call.tool_name,
        )

        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_completed",
            source="tool",
            payload=completed_payload,
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        session = clear_tool_execution_intent(self._session_store, self._workspace, session)

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
            self._note_hook_guidance(post_hook_outcome.guidance)

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
        attempt_stream_visibility = _AttemptStreamVisibility()
        pending_provider_attempt_reset: _ProviderAttemptReset | None = None
        first_iteration = True
        stuck_detected_emitted = False
        # Reminder to append at the tail of the next turn's provider segments;
        # cleared once assembled. Never persisted, never in the cache prefix.
        pending_reminder_segment: ContextSegment | None = None
        # One bounded-pruning recovery per turn; see ``_ContextLimitRecoveryState``.
        context_limit_recovery = _ContextLimitRecoveryState()
        self._pending_hook_guidance = []
        checkpoint_tool_result_count = len(tool_results)
        # Run-local watermark propagated to graph events, hooks, and diagnostics.
        run_step = graph_request.run_step

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
            if tool_results and _is_terminal_yield_result(tool_results[-1]):
                sequence = yield from self._yield_terminal(
                    session=session,
                    tool_results=tool_results,
                    sequence=sequence,
                )
                break
            queued_messages = runtime.drain_queued_messages(session.session.id, kind="steering")
            if queued_messages:
                steering_text = "\n\n".join(queued_messages)
                active_graph_request = replace(
                    active_graph_request,
                    prompt=(
                        f"{active_graph_request.prompt}\n\nRuntime steering messages:\n{steering_text}"
                        if active_graph_request.prompt.strip()
                        else steering_text
                    ),
                )
            current_graph_request: Any = active_graph_request
            current_prompt: str = current_graph_request.prompt
            current_available_tools: tuple[ToolDefinition, ...] = current_graph_request.available_tools
            current_metadata: dict[str, object] = current_graph_request.metadata
            current_abort_signal: ProviderAbortSignal | None = current_graph_request.abort_signal
            turn_index = run_step
            sequence, terminated, stuck_detected_emitted, turn_hook_guidance = yield from self._run_turn_hooks(
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
            provider_tool_results = tuple(tool_results)
            sequence, before_compact_input = yield from self._run_before_compact_hook_phase(
                session=session,
                sequence=sequence,
                tool_results=provider_tool_results,
            )
            # Gate order: the hook is consulted first, so a hook cancel skips the
            # opt-in model call entirely; then the caller-supplied summary; then
            # the gate on the compiled window, which the run loop reads instead
            # of resolving the config a second time. With the gate off not even
            # ``summarize_continuity`` is called.
            continuity_summary_override: str | None = None
            continuity_summary_kind: str | None = None
            if (before_compact_input is None or not before_compact_input.cancel) and (
                active_graph_request.context_window is not None and cast(RuntimeContextWindow, active_graph_request.context_window).summary_enabled
            ):
                continuity_summary_override = runtime.summarize_continuity(
                    tool_results=provider_tool_results,
                    session_metadata=session.metadata,
                )
                # A failure or a blank answer degrades to the deterministic
                # projection; only the provenance differs, and it is reported
                # explicitly because no override is passed in that case.
                continuity_summary_kind = "model" if continuity_summary_override is not None else "fallback"
            context_window, first_iteration = self._resolve_turn_context_window(
                active_graph_request=active_graph_request,
                tool_results=provider_tool_results,
                session=session,
                continuity_to_reinject=continuity_to_reinject,
                first_iteration=first_iteration,
                before_compact=before_compact_input,
            )
            session, sequence, mid_run_segment = yield from self._todo_mid_run_nudge_step(
                session=session,
                sequence=sequence,
                tool_results=provider_tool_results,
                active_graph_request=active_graph_request,
                tool_registry=tool_registry,
                effective_runtime_config=runtime.effective_runtime_config_from_metadata(session.metadata),
            )
            session, assembled_context, context_window = yield from self._assemble_turn_context(
                active_graph_request=active_graph_request,
                context_window=context_window,
                session=session,
                hook_guidance=turn_hook_guidance,
                reminder_segment=pending_reminder_segment or mid_run_segment,
                before_compact=before_compact_input,
                continuity_summary_override=continuity_summary_override,
                continuity_summary_kind=cast(ContinuitySummaryKind | None, continuity_summary_kind),
            )
            pending_reminder_segment = None
            active_graph_request = graph_request_for_session(
                GraphRunRequest(
                    session=graph_session_snapshot(session),
                    prompt=current_prompt,
                    available_tools=current_available_tools,
                    context_window=context_window,
                    assembled_context=assembled_context,
                    metadata=current_metadata,
                    abort_signal=current_abort_signal,
                    tool_call_preview=self._tool_call_preview,
                    run_step=run_step,
                ),
                session,
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
                    tool_results=provider_tool_results,
                    session=session,
                    sequence=sequence,
                    reasoning_capture_state=reasoning_capture_state,
                    graph=graph,
                    attempt_stream_visibility=attempt_stream_visibility,
                )
                if graph_step is None:
                    return
                provider_retry_attempt = 0
            except Exception as exc:
                verdict = yield from self._apply_provider_error_policy(
                    exc=exc,
                    session=session,
                    tool_results=provider_tool_results,
                    context_limit_recovery=context_limit_recovery,
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
                    attempt_stream_visibility=attempt_stream_visibility,
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
                # Finalize-window abort guard: ``_finalize_step_state`` above
                # checks the abort signal at its entry, but a user interrupt can
                # land during ``_persist_step_events``/finalize after that check.
                # Without this guard the run would break out as ``completed`` and
                # the cancellation would be silently lost — the client needs the
                # terminal ``runtime.failed{cancelled: true}`` event to render the
                # interrupt. Emit it before the final-step artifacts so the run
                # still ends ``interrupted``.
                if _is_abort_requested(active_graph_request):
                    yield from self._emit_interrupted_failure(
                        session=session,
                        sequence=sequence,
                        active_graph_request=active_graph_request,
                    )
                    return
                session, sequence, pending_reminder_segment = yield from self._todo_reminder_step(
                    session=session,
                    sequence=sequence,
                    tool_results=tool_results,
                    available_tools=current_available_tools,
                    effective_runtime_config=effective_runtime_config,
                )
                if pending_reminder_segment is not None:
                    # Incomplete todos: run one more turn with the reminder
                    # appended at the tail instead of ending the session.
                    continue
                yield from self._emit_final_step_artifacts(
                    runtime=runtime,
                    session=current_chunk_session,
                    graph_step=graph_step,
                    reasoning_capture_state=reasoning_capture_state,
                )
                break

            plan_tool_call, tool, tool_call_id, sequence, input_hook_outcome = yield from self._plan_tool_step(
                session=session,
                sequence=sequence,
                tool_registry=tool_registry,
                graph_step=graph_step,
                is_resume=current_graph_request.metadata.get("runtime_resume") is True or current_graph_request.metadata.get("resume") is True,
            )
            if input_hook_outcome.action == "block":
                sequence = yield from self._dispatch_error_feedback_chunks(
                    session=session,
                    tool_name=plan_tool_call.tool_name,
                    tool_call_id=tool_call_id,
                    arguments=dict(plan_tool_call.arguments),
                    tool_results=tool_results,
                    error=input_hook_outcome.blocked_reason or "tool input handler blocked the call",
                    error_kind="tool_input_handler_blocked",
                )
                continue
            if plan_tool_call.tool_name == "invoke_tool":
                # On-demand dispatch: resolve the inner tool and run it through
                # the SAME execution boundary as a provider-native tool call
                # (policy denial -> registry resolve -> permission -> hooks ->
                # executor). Unknown tools and denials surface as tool-level
                # feedback; the run continues.
                session, sequence = yield from self._execute_invoked_tool(
                    tool_registry=tool_registry,
                    session=session,
                    sequence=sequence,
                    outer_call=plan_tool_call,
                    outer_call_id=tool_call_id,
                    tool_results=tool_results,
                    permission_policy=active_permission_policy,
                    abort_signal=active_graph_request.abort_signal,
                    is_resume=current_graph_request.metadata.get("runtime_resume") is True or current_graph_request.metadata.get("resume") is True,
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

            plan_tool_call, tool_call_id, _intent_payload, session = self._persist_resolved_tool_intent(
                session=session,
                tool=tool,
                tool_call=plan_tool_call,
                tool_call_id=tool_call_id,
            )
            sequence, verdict = yield from self._run_tool_hook_phase(
                session=session,
                sequence=sequence,
                tool_name=plan_tool_call.tool_name,
                phase="pre",
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
            assert tool_result is not None

            tool_result, todo_mutated, runtime_tool_result_data, session, sequence, terminated = yield from self._finalize_tool_result(
                session=session,
                sequence=sequence,
                plan_tool_call=plan_tool_call,
                tool_call_id=tool_call_id,
                tool_result=tool_result,
                active_graph_request=active_graph_request,
            )
            if terminated:
                return

            if (
                yield from self._handle_question_outcome(
                    session=session,
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
                todo_mutated=todo_mutated,
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
            run_step += 1
            if provider_attempt != 0:
                pending_provider_attempt_reset = _provider_attempt_reset_after_tool_result(
                    provider_attempt=provider_attempt,
                    selection=select_graph_for_effective_config(
                        config=effective_runtime_config,
                        provider_attempt=0,
                    ),
                    graph_request=replace(active_graph_request, run_step=run_step),
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

    def _yield_terminal(
        self,
        *,
        session: SessionState,
        tool_results: list[ToolResult],
        sequence: int,  # noqa: ARG002 — retained for terminalization call-shape symmetry; persisted envelope owns sequence.
    ) -> Generator[RuntimeStreamChunk, None, int]:
        terminal_result = tool_results[-1]
        terminal_output = (terminal_result.content or terminal_result.error or "").strip()
        if not terminal_output:
            raise ValueError("yield completed without a non-empty summary")
        if terminal_result.data.get("yield_kind") == "terminal_error":
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=terminal_result.error or terminal_output or "delegated yield failed",
                    payload={"kind": "delegated_yield_error", "terminal": True},
                )
            )
            yield failed_chunk
            return failed_chunk.event.sequence if failed_chunk.event is not None else sequence
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
            payload={"output_preview": terminal_output, "source": "yield"},
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
        graph_step: GraphStep,
        reasoning_capture_state: ReasoningCaptureState,
        current_chunk_session: SessionState,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        live_only_event_types = {
            "graph.provider_stream",
            "graph.tool_call_start",
            "graph.tool_call_delta",
            "graph.tool_call_end",
        }
        step_events = tuple(event for event in graph_step.events if event.event_type not in live_only_event_types)
        renumbered_events = renumber_events(
            step_events,
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
        graph_step: GraphStep,
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
        if graph_step.output is not None:
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
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, bool, tuple[str, ...]]]:
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
                return sequence, True, ()
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
            return sequence, True, ()
        return sequence, False, hook.guidance

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
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, bool, bool, tuple[str, ...]]]:
        turn_progress_payload: dict[str, object] = {
            "turn": turn_index,
            "tool_result_count": len(tool_results),
            "provider_attempt": provider_attempt,
            "provider_retry_attempt": provider_retry_attempt,
        }
        sequence, terminated, turn_guidance = yield from self._run_turn_hook_phase(
            session=session,
            sequence=sequence,
            surface="turn_progress",
            payload=turn_progress_payload,
            cancel_message="run cancelled by turn-progress hook",
        )
        collected_guidance = list(turn_guidance)
        if terminated:
            return sequence, True, stuck_detected_emitted, ()
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
            sequence, terminated, stuck_guidance = yield from self._run_turn_hook_phase(
                session=session,
                sequence=sequence,
                surface="stuck_detected",
                payload=stuck_payload,
                cancel_message="run cancelled by stuck-detected hook",
            )
            collected_guidance.extend(stuck_guidance)
            if terminated:
                return sequence, True, stuck_detected_emitted, ()
        return sequence, False, stuck_detected_emitted, tuple(collected_guidance)

    def _run_before_compact_hook_phase(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_results: tuple[ToolResult | ToolResultView, ...],
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, BeforeCompactInput | None]]:
        try:
            hook = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=session,
                sequence=sequence,
                surface="before_compact",
                payload={
                    "tool_result_count": len(tool_results),
                    "tool_names": sorted({result.tool_name for result in tool_results}),
                },
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(session.metadata),
            )
        except Exception as exc:
            logger.warning("before_compact hook failed: %s", exc)
            return sequence, None
        sequence = yield from self._persist_chunks(
            hook.chunks,
            fallback_sequence=hook.last_sequence,
        )
        return sequence, before_compact_input_from_hook_outcome(hook)

    def _resolve_turn_context_window(
        self,
        *,
        active_graph_request: GraphRunRequest,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        session: SessionState,
        continuity_to_reinject: ContextProjection | None,
        first_iteration: bool,
        before_compact: BeforeCompactInput | None = None,
    ) -> tuple[RuntimeContextWindow, bool]:
        runtime = self._surface
        current_graph_request = active_graph_request
        current_prompt = current_graph_request.prompt
        current_abort_signal = current_graph_request.abort_signal
        current_session_metadata: dict[str, object] = session.metadata
        if first_iteration:
            # Boundary: the graph field is typed with the provider Protocol, which
            # does not declare the runtime-only counters read below; the runtime is
            # the sole producer of this field, so the concrete window is the truth.
            prebuilt_context = cast(RuntimeContextWindow, current_graph_request.context_window)
            first_iteration = False
            if (
                before_compact is None
                and prebuilt_context.original_tool_result_count == len(tool_results)
                and prebuilt_context.tool_results == tuple(tool_results)
            ):
                base_context = prebuilt_context
            else:
                base_context = runtime.prepare_provider_context_window(
                    prompt=current_prompt,
                    tool_results=tuple(tool_results),
                    session_metadata=current_session_metadata,
                    abort_signal=current_abort_signal,
                    before_compact=before_compact,
                )
        else:
            base_context = runtime.prepare_provider_context_window(
                prompt=current_prompt,
                tool_results=tuple(tool_results),
                session_metadata=current_session_metadata,
                abort_signal=current_abort_signal,
                before_compact=before_compact,
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
        hook_guidance: Iterable[str] | None = None,
        reminder_segment: ContextSegment | None = None,
        before_compact: BeforeCompactInput | None = None,
        continuity_summary_override: str | None = None,
        continuity_summary_kind: ContinuitySummaryKind | None = None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, AssembledContext, RuntimeContextWindow]]:
        runtime = self._surface
        current_graph_request = active_graph_request
        current_prompt = current_graph_request.prompt
        session = session_with_context_window_metadata(session, context_window)
        persisted_skill_snapshot = skill_snapshot_from_metadata(session.metadata)
        skill_prompt_context = persisted_skill_snapshot.skill_prompt_context if persisted_skill_snapshot is not None else ""
        if (
            not skill_prompt_context
            and persisted_skill_snapshot is not None
            and persisted_skill_snapshot.source == "run"
            and current_graph_request.metadata.get("runtime_resume") is not True
            and current_graph_request.assembled_context is not None
        ):
            for segment in current_graph_request.assembled_context.segments:
                if segment.role != "system" or not isinstance(segment.content, str):
                    continue
                if isinstance(segment.metadata, dict) and segment.metadata.get("source") == "skill_prompt":
                    skill_prompt_context = segment.content
                    break
        assembled_context = runtime.assemble_provider_context(
            prompt=current_prompt,
            tool_results=context_window.tool_results,
            session_metadata=session.metadata,
            skill_prompt_context=skill_prompt_context,
            replayed_conversation_segments=_replayed_conversation_segments(current_graph_request),
            hook_guidance=(*self._drain_pending_hook_guidance(), *(hook_guidance or ())) or None,
            reminder_segment=reminder_segment,
            before_compact=before_compact,
            continuity_summary_override=continuity_summary_override,
            continuity_summary_kind=continuity_summary_kind,
        )
        # The assembled context compiled the provider view with a payload-aware
        # budget, so its window owns the honest compaction counts for these
        # segments (the pre-assembly window never sized the full request).
        effective_context_window = assembled_context.context_window or context_window
        context_window_payload = {
            **effective_context_window.metadata_payload(),
            **assembled_context.metadata,
        }
        session = session_with_context_window_payload_metadata(
            session,
            context_window_payload,
        )
        context_transform_payloads = context_transform_applied_payloads(
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
        return session, assembled_context, effective_context_window

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
                    event_type=RUNTIME_PROVIDER_CONTEXT_POLICY,
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
        graph_step: GraphStep,
        provider_attempt: int,
        tool_results: list[ToolResult],
    ) -> Generator[RuntimeStreamChunk, None, tuple[bool, SessionState, SessionState, int, bool]]:
        is_final_step = graph_step.is_finished or graph_step.output is not None
        # New delegated children must finish through the terminal ``yield``
        # tool. Keep-alive turns are the only exception: they park as
        # resumable ``interrupted`` sessions without a terminal yield.
        if is_final_step and session.session.parent_id is not None and session.metadata.get("keep_alive_turn") is not True:
            if not tool_results or not _is_terminal_yield_result(tool_results[-1]):
                raise ValueError("delegated child must call yield before completing")
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
            # no terminal yield) so the background-task worker can park the
            # task idle awaiting steer; one-shot children complete.
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

    def _reminder_suppression(
        self,
        *,
        session: SessionState,
        available_tools: tuple[ToolDefinition, ...],
    ) -> ReminderSuppression:
        """Shared "the loop is already parked" predicate for every reminder kind."""
        delegated_child = session.session.parent_id is not None
        waits_for_user = _runtime_waits_for_user(session)
        pending_background_task = (
            not delegated_child and not waits_for_user and self._surface.has_pending_background_tasks(parent_session_id=session.session.id)
        )
        return ReminderSuppression(
            delegated_child=delegated_child,
            runtime_waits_for_user=waits_for_user,
            pending_background_task=pending_background_task,
            plan_mode=runtime_mode_from_metadata(session.metadata) == "plan",
            todo_tool_available=any(definition.name == TODO_REMINDER_KIND for definition in available_tools),
        )

    def _todo_mid_run_nudge_step(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        active_graph_request: GraphRunRequest,
        tool_registry: ToolRegistry,
        effective_runtime_config: EffectiveRuntimeConfig,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, ContextSegment | None]]:
        """Insert one mid-run todo nudge before the next provider call.

        Upstream ``takeMidRunNudge``: while a turn is still running, a stale todo
        list (at least ``TODO_MID_RUN_MUTATION_THRESHOLD`` mutation-tool results
        since the last todo touch) earns a per-call nudge, at most
        ``TODO_MID_RUN_MAX_PER_CYCLE`` per cycle. It rides the same tail-segment
        channel as the completion reminder: never persisted, never in the cache
        prefix, and it does not touch output/tool-result semantics.
        """
        if not effective_runtime_config.reminders.enabled or effective_runtime_config.execution_engine != "provider":
            return session, sequence, None
        todo_state = todo_state_from_session_metadata(session.metadata)
        phases = runtime_todo_phases_from_payload(todo_state["phases"]) if todo_state is not None else ()
        read_only_tool_names = frozenset(definition.name for definition in tool_registry.definitions() if is_read_tier(definition.effects))
        stored_state = todo_mid_run_state_from_metadata(session.metadata)
        decision = decide_todo_mid_run_nudge(
            state=stored_state,
            run_id=runtime_state_run_id(session.metadata),
            mutations=todo_mutation_count(tool_results, read_only_tool_names=read_only_tool_names),
            incomplete_count=sum(len(contents) for _name, contents in incomplete_todo_phases(phases)),
            suppression=self._reminder_suppression(session=session, available_tools=active_graph_request.available_tools),
        )
        if decision.state != stored_state:
            session = session_with_reminder_state(session, mid_run=decision.state)
        if not decision.injects:
            return session, sequence, None
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_REMINDER_INJECTED,
            source="runtime",
            payload={
                "reminder_type": TODO_MID_RUN_KIND,
                "attempt": decision.attempt,
                "max_attempts": decision.max_attempts,
                "mutation_count": decision.mutation_count,
                "incomplete_todo_count": decision.incomplete_count,
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return session, envelope.sequence, todo_mid_run_segment(decision)

    def _todo_reminder_step(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_results: list[ToolResult],
        available_tools: tuple[ToolDefinition, ...],
        effective_runtime_config: EffectiveRuntimeConfig,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, ContextSegment | None]]:
        """Decide, persist and announce the terminal-turn todo reminder.

        Returns the per-call tail segment the continuation turn must append, or
        ``None`` when the channel stays silent (disabled, a non-provider engine
        whose graph has no model call to append to, no unfinished todo, previous
        reminder still awaiting progress, attempt budget spent, waiting on the
        user, a pending background task that will re-wake the loop, or a
        delegated child, which must terminate through ``yield``).
        """
        if not effective_runtime_config.reminders.enabled or effective_runtime_config.execution_engine != "provider":
            return session, sequence, None
        todo_state = todo_state_from_session_metadata(session.metadata)
        phases = runtime_todo_phases_from_payload(todo_state["phases"]) if todo_state is not None else ()
        stored_state = todo_reminder_state_from_metadata(session.metadata)
        decision = decide_todo_reminder(
            max_per_cycle=effective_runtime_config.reminders.todo.max_per_cycle,
            state=stored_state,
            run_id=runtime_state_run_id(session.metadata),
            incomplete_phases=incomplete_todo_phases(phases),
            tool_result_count=len(tool_results),
            suppression=self._reminder_suppression(
                session=session,
                available_tools=available_tools,
            ),
        )
        if decision.state != stored_state:
            session = session_with_reminder_state(session, todo=decision.state)
        if not decision.injects:
            return session, sequence, None
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_REMINDER_INJECTED,
            source="runtime",
            payload={
                "reminder_type": TODO_REMINDER_KIND,
                "attempt": decision.attempt,
                "max_attempts": decision.max_attempts,
                "incomplete_todo_count": decision.incomplete_count,
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return session, envelope.sequence, todo_reminder_segment(decision)

    def _invoke_provider_step(
        self,
        *,
        active_graph_request: GraphRunRequest,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        session: SessionState,
        sequence: int,
        reasoning_capture_state: ReasoningCaptureState,
        graph: RuntimeGraph,
        attempt_stream_visibility: _AttemptStreamVisibility,
    ) -> Generator[RuntimeStreamChunk, None, tuple[Any | None, int, list[str]]]:
        graph_request = graph_request_for_session(active_graph_request, session)
        streamed_reasoning_texts: list[str] = []
        # A new provider attempt starts unseen; anything the previous attempt
        # surfaced was already handled by the error policy.
        attempt_stream_visibility.surfaced = False
        if _is_abort_requested(active_graph_request):
            yield from self._emit_interrupted_failure(
                session=session,
                sequence=sequence,
                active_graph_request=active_graph_request,
            )
            return None, sequence, streamed_reasoning_texts
        if active_graph_request.metadata.get("provider_stream") is True and isinstance(graph, StreamableGraph):
            graph_step = None
            partial_fragments: dict[str, list[str]] = {}
            partial_fragment_chars: dict[str, int] = {}
            partial_tool_names: dict[str, str] = {}

            def decorate_live_event(event: GraphEvent) -> GraphEvent:
                if event.event_type not in {"graph.tool_call_start", "graph.tool_call_delta", "graph.tool_call_end"}:
                    return event
                payload = dict(event.payload)
                raw_call_id = payload.get("tool_call_id")
                call_id = raw_call_id if isinstance(raw_call_id, str) and raw_call_id else None
                raw_name = payload.get("tool_name")
                tool_name = raw_name if isinstance(raw_name, str) else None
                tracking_id = call_id or f"anonymous:{tool_name or 'unknown'}"
                if call_id is not None or tool_name in WRITE_PREVIEW_TOOLS:
                    partial_tool_names[tracking_id] = tool_name or partial_tool_names.get(tracking_id, "")
                    fragments = partial_fragments.setdefault(tracking_id, [])
                    raw_delta = payload.get("arguments_delta")
                    if isinstance(raw_delta, str):
                        chars = partial_fragment_chars.get(tracking_id, 0)
                        if chars < PREVIEW_SNAPSHOT_MAX_BYTES:
                            fragment = raw_delta[: PREVIEW_SNAPSHOT_MAX_BYTES - chars]
                            fragments.append(fragment)
                            partial_fragment_chars[tracking_id] = chars + len(fragment)
                    raw_parsed = payload.get("parsed_arguments")
                    parsed = raw_parsed if isinstance(raw_parsed, dict) else None
                    tool_name = partial_tool_names.get(tracking_id) or tool_name
                    if tool_name in WRITE_PREVIEW_TOOLS:
                        if "diff_preview" not in payload:
                            try:
                                diff_preview = build_partial_tool_call_preview(
                                    workspace=self._workspace,
                                    tool_name=tool_name,
                                    argument_text="".join(fragments),
                                    parsed_arguments=parsed,
                                )
                            except Exception:
                                diff_preview = None
                            if diff_preview is not None:
                                payload["diff_preview"] = diff_preview
                        # The write payload can include arbitrary source or
                        # secret-like content; diff_preview is the canonical,
                        # bounded projection for these lifecycle events.
                        payload.pop("arguments_delta", None)
                        payload.pop("parsed_arguments", None)
                return GraphEvent(event_type=event.event_type, source=event.source, payload=payload)

            stream_request = replace(
                graph_request,
                tool_call_preview=lambda tool_name, fragments, parsed: build_partial_tool_call_preview(
                    workspace=self._workspace,
                    tool_name=tool_name,
                    argument_text="".join(fragments),
                    parsed_arguments=parsed,
                ),
            )
            for streamed_item in graph.stream_step(
                stream_request,
                tuple(tool_results),
                session=graph_request.session,
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
                    streamed_item = decorate_live_event(streamed_item)
                    # Content-bearing live events are about to reach the client;
                    # from here on the attempt has user-visible stream output and
                    # must not be silently replayed (retry/fallback).
                    if _live_event_surfaces_output(streamed_item):
                        attempt_stream_visibility.surfaced = True
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
                graph_request,
                tool_results=tuple(tool_results),
                session=graph_request.session,
            )
            # Non-streaming turns (background children) carry the turn's
            # reasoning on the step; aggregate it like the streamed deltas
            # so one bounded runtime.reasoning_part is persisted below.
            reasoning_text = graph_step.reasoning
            if reasoning_text:
                reasoning_capture_state.stream_observed = True
                reasoning_capture_state.reasoning_observed = True
                reasoning_capture_state.part_count += 1
                reasoning_capture_state.text_char_count += len(reasoning_text)
                streamed_reasoning_texts.append(reasoning_text)
        return graph_step, sequence, streamed_reasoning_texts

    def _context_limit_recovery_step(
        self,
        *,
        session: SessionState,
        sequence: int,  # noqa: ARG002 - the persisted envelope owns the sequence, mirroring the fallback branches.
        provider_error: ProviderExecutionError,
        active_graph_request: GraphRunRequest,
        context_window: RuntimeContextWindow,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        context_limit_recovery: _ContextLimitRecoveryState,
        graph: RuntimeGraph,
    ) -> Generator[RuntimeStreamChunk, None, _ProviderErrorPolicyVerdict | None]:
        """One bounded-pruning recovery for a ``context_limit`` failure.

        Rebuilds the provider view with a recovery budget (the whole view must fit
        the catalog window minus reserve) and returns a retry verdict when that
        actually shrinks it. When nothing is prunable it returns ``None`` so the
        generic policy promotes through the existing fallback chain, or fails
        resumably. The lever is one-shot per turn: ``context_limit_recovery`` is
        run-local state, so a second context_limit goes straight to promotion.
        """
        assembled = self._surface.reassemble_provider_context_for_overflow(
            prompt=active_graph_request.prompt,
            tool_results=tool_results,
            session_metadata=session.metadata,
            replayed_conversation_segments=_replayed_conversation_segments(active_graph_request),
        )
        window = assembled.context_window
        reclaimed = window.dropped_tool_result_count if window is not None else 0
        payload: dict[str, object] = {
            "reason": provider_error.kind,
            "provider": provider_error.provider_name,
            "model": provider_error.model_name,
            "tool_result_count": len(tool_results),
            "dropped_tool_result_count": reclaimed,
            "usage_tokens_before": window.usage_tokens_before if window is not None else None,
            "usage_tokens_after": window.usage_tokens_after if window is not None else None,
            "usage_tokens_estimated": window.estimate_won if window is not None else True,
            "measured_anchor_tokens": window.measured_anchor_tokens if window is not None else None,
            "estimated_delta_tokens": window.estimated_delta_tokens if window is not None else None,
            "compaction_reason": window.compaction_reason if window is not None else None,
            **({"provider_error_details": provider_error.details} if provider_error.details is not None else {}),
        }
        context_limit_recovery.prune_attempted = True
        if reclaimed == 0:
            unavailable = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_PROVIDER_CONTEXT_RECOVERY,
                source="runtime",
                payload={**payload, "mode": "prune", "outcome": "unavailable"},
            )
            yield RuntimeStreamChunk(kind="event", session=session, event=unavailable)
            return None
        context_limit_recovery.pruned = True
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type=RUNTIME_PROVIDER_CONTEXT_RECOVERY,
            source="runtime",
            payload={**payload, "mode": "prune", "outcome": "retry"},
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        session = session_with_context_window_payload_metadata(session, dict(assembled.metadata))
        retry_request = graph_request_for_session(
            GraphRunRequest(
                session=graph_session_snapshot(session),
                prompt=active_graph_request.prompt,
                available_tools=active_graph_request.available_tools,
                context_window=window if window is not None else context_window,
                assembled_context=assembled,
                metadata=active_graph_request.metadata,
                abort_signal=active_graph_request.abort_signal,
                tool_call_preview=self._tool_call_preview,
                run_step=active_graph_request.run_step,
            ),
            session,
        )
        return {
            "action": "retry",
            "provider_attempt": provider_attempt_from_metadata(active_graph_request.metadata),
            "provider_retry_attempt": provider_retry_attempt_from_metadata(active_graph_request.metadata),
            "graph": graph,
            "session": session,
            "graph_request": retry_request,
        }

    def _apply_provider_error_policy(
        self,
        *,
        exc: Exception,
        session: SessionState,
        tool_results: tuple[ToolResult | ToolResultView, ...],
        context_limit_recovery: _ContextLimitRecoveryState,
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
        attempt_stream_visibility: _AttemptStreamVisibility,
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
            # The context_limit lane prefers a strictly larger-window candidate
            # (its own lane policy, see ``context_limit_promotion_for_provider_error``);
            # every other provider error keeps the generic chain-order selection.
            promotion: ContextLimitPromotion | None = None
            if provider_error.kind == "context_limit":
                promotion = context_limit_promotion_for_provider_error(
                    error=provider_error,
                    provider_chain=effective_runtime_config.resolved_provider.target_chain,
                    config=effective_runtime_config,
                    provider_attempt=current_provider_attempt,
                )
                fallback_selection = promotion.selection
            if provider_error.kind == "context_limit":
                # One bounded-pruning retry per turn: the lever is spent once the
                # run-local state records it; a second overflow escalates instead.
                recovery_verdict = (
                    None
                    if context_limit_recovery.prune_attempted
                    else (
                        yield from self._context_limit_recovery_step(
                            session=session,
                            sequence=sequence,
                            provider_error=provider_error,
                            active_graph_request=active_graph_request,
                            context_window=context_window,
                            tool_results=tool_results,
                            context_limit_recovery=context_limit_recovery,
                            graph=graph,
                        )
                    )
                )
                if recovery_verdict is not None:
                    return recovery_verdict
                if context_limit_recovery.prune_attempted and context_limit_recovery.promoted:
                    # Both levers of this turn are spent: fail terminally (the
                    # failure stays resumable) instead of cycling the fallback
                    # chain on a request that is over budget for every target.
                    failed_chunk, _ = self._persist_chunk(
                        chunk_builders.failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=provider_error.message,
                            payload=_terminal_provider_error_payload(
                                {"provider_error_kind": provider_error.kind},
                                recovery=context_limit_recovery,
                                error=provider_error,
                            ),
                        )
                    )
                    yield failed_chunk
                    return {"action": "exit"}
                if context_limit_recovery.prune_attempted and not context_limit_recovery.promoted:
                    # A prunable view was unavailable or already retried; either
                    # way this turn escalates once.
                    context_limit_recovery.promoted = True
                    assert promotion is not None
                    envelope = self._persist_event(
                        session_id=session.session.id,
                        event_type=RUNTIME_PROVIDER_CONTEXT_RECOVERY,
                        source="runtime",
                        payload={
                            "mode": "promote",
                            "reason": provider_error.kind,
                            "provider": provider_error.provider_name,
                            "model": provider_error.model_name,
                            "fallback_target_present": fallback_selection is not None,
                            "promotion_reason": promotion.promotion_reason,
                            "window_tokens_before": promotion.window_tokens_before,
                            "window_tokens_after": promotion.window_tokens_after,
                            "candidate_count": promotion.candidate_count,
                            **({"provider_error_details": provider_error.details} if provider_error.details is not None else {}),
                        },
                    )
                    sequence = envelope.sequence
                    yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
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
                retry_payload = provider_decision.event_payload()
                if attempt_stream_visibility.surfaced:
                    # The client already rendered live deltas from the attempt
                    # that is now being restarted; the runtime announces it so the
                    # client can discard that in-flight projection (it is not
                    # persisted truth). Absent for attempts that surfaced nothing.
                    retry_payload["discarded_streamed_output"] = True
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_PROVIDER_TRANSIENT_RETRY,
                    source="runtime",
                    payload=retry_payload,
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
                active_graph_request = graph_request_for_session(
                    GraphRunRequest(
                        session=graph_session_snapshot(session),
                        prompt=current_prompt,
                        available_tools=current_available_tools,
                        context_window=context_window,
                        assembled_context=active_graph_request.assembled_context,
                        metadata=retry_metadata,
                        abort_signal=current_abort_signal,
                        tool_call_preview=self._tool_call_preview,
                        run_step=active_graph_request.run_step,
                    ),
                    session,
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
                fallback_payload = provider_decision.event_payload()
                if attempt_stream_visibility.surfaced:
                    # Same contract as the transient-retry announcement: the
                    # fallback target restarts a turn whose live deltas the
                    # client already rendered, so it must discard that
                    # in-flight projection (never the persisted events).
                    fallback_payload["discarded_streamed_output"] = True
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_PROVIDER_FALLBACK,
                    source="runtime",
                    payload=fallback_payload,
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                provider_attempt = fallback_selection.provider_attempt
                provider_retry_attempt = 0
                fallback_prompt: str = current_prompt
                fallback_available_tools: tuple[ToolDefinition, ...] = current_available_tools
                fallback_context_window = context_window
                fallback_assembled_context: AssembledContext = active_graph_request.assembled_context
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
                active_graph_request = graph_request_for_session(
                    GraphRunRequest(
                        prompt=fallback_prompt,
                        session=graph_session_snapshot(session),
                        available_tools=fallback_available_tools,
                        context_window=fallback_context_window,
                        assembled_context=fallback_assembled_context,
                        metadata=fallback_metadata,
                        abort_signal=fallback_abort_signal,
                        tool_call_preview=self._tool_call_preview,
                        run_step=active_graph_request.run_step,
                    ),
                    session,
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
                        payload=_terminal_provider_error_payload(provider_decision.payload, recovery=context_limit_recovery, error=provider_error),
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
                    payload=_terminal_provider_error_payload(provider_decision.payload, recovery=context_limit_recovery, error=provider_error),
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
                payload=({"kind": "provider_context_limit"} if isinstance(classified_error, ProviderContextLimitError) else None),
            )
        )
        yield failed_chunk
        if isinstance(classified_error, ProviderContextLimitError):
            return {"action": "exit"}
        return {"action": "reraise", "exc": exc}

    def _prepare_typed_tool_call(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_registry: ToolRegistry,
        tool_call: ToolCall,
        tool: Tool,
        is_resume: bool,
    ) -> tuple[ToolCall, Tool, ToolInputHookOutcome]:
        """Apply the shared typed-input gate before permission/approval.

        This is deliberately only the pre-execution candidate phase. The
        caller still owns event ordering and permission/approval; each tool
        retains its private Pydantic/schema and guard validation.
        """
        if tool_call.tool_name == "invoke_tool":
            return tool_call, tool, ToolInputHookOutcome(tool_call=tool_call)
        outcome = self._tool_input_handler_registry.apply(
            event=ToolInputEvent(
                session_id=session.session.id,
                tool_call=tool_call,
                tool=tool.definition,
                sequence=sequence,
                session_status=session.status,
                is_resume=is_resume,
                mode=str(session.metadata.get("mode", "normal")),
                read_only=runtime_read_only_from_metadata(session.metadata),
            )
        )
        if outcome.action != "rewrite":
            return outcome.tool_call, tool, outcome
        try:
            validate_tool_input_schema(tool.definition, outcome.tool_call.arguments)
        except ValueError as exc:
            return (
                outcome.tool_call,
                tool,
                ToolInputHookOutcome(
                    tool_call=outcome.tool_call,
                    action="block",
                    diagnostics=outcome.diagnostics,
                    handler_names=outcome.handler_names,
                    blocked_reason=str(exc),
                ),
            )
        final_call = outcome.tool_call
        try:
            final_tool = tool_registry.resolve(final_call.tool_name)
        except Exception as exc:
            return (
                final_call,
                tool,
                ToolInputHookOutcome(
                    tool_call=final_call,
                    action="block",
                    diagnostics=outcome.diagnostics,
                    handler_names=outcome.handler_names,
                    blocked_reason=f"rewritten tool lookup failed: {exc}",
                ),
            )
        return final_call, final_tool, outcome

    def _plan_tool_step(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_registry: ToolRegistry,
        graph_step: GraphStep,
        is_resume: bool = False,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolCall, Tool, str, int, ToolInputHookOutcome]]:
        runtime = self._surface
        plan_tool_call = graph_step.tool_call
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
        if not isinstance(plan_tool_call, ToolCall):
            raise TypeError("graph step tool_call must be a ToolCall")
        original_tool_call = plan_tool_call
        explicit_tool_call_id = plan_tool_call.tool_call_id
        tool_call_id = explicit_tool_call_id or f"runtime-tool-{uuid4().hex}"
        graph_payload: dict[str, object] = {
            "tool": original_tool_call.tool_name,
            "arguments": dict(original_tool_call.arguments),
            **({"path": path} if isinstance((path := original_tool_call.arguments.get("path")), str) else {}),
        }
        if original_tool_call.tool_name in WRITE_PREVIEW_TOOLS:
            try:
                diff_preview = build_tool_call_preview(
                    workspace=self._workspace,
                    tool_name=original_tool_call.tool_name,
                    arguments=original_tool_call.arguments,
                    phase="final",
                )
            except Exception:
                diff_preview = None
            if diff_preview is not None:
                graph_payload["diff_preview"] = diff_preview
        if explicit_tool_call_id is not None or runtime.effective_runtime_config_from_metadata(session.metadata).execution_engine == "provider":
            graph_payload["tool_call_id"] = tool_call_id
        envelope = self._persist_event(
            session_id=session.session.id,
            event_type="graph.tool_request_created",
            source="graph",
            payload=graph_payload,
        )
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        delegation_policy_error = runtime.delegation_tool_policy_error(session=session, tool_name=plan_tool_call.tool_name)
        if delegation_policy_error is not None:
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=delegation_policy_error,
                    payload={"kind": "delegation_tool_policy_denied", "tool": plan_tool_call.tool_name},
                )
            )
            yield failed_chunk
            raise ValueError(delegation_policy_error)

        tool_policy_denial = runtime.tool_policy_denial(session=session, tool_name=plan_tool_call.tool_name)
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

        lookup_envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_lookup_succeeded",
            source="runtime",
            payload={"tool": plan_tool_call.tool_name},
        )
        sequence = lookup_envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=lookup_envelope)

        plan_tool_call, tool, input_hook_outcome = self._prepare_typed_tool_call(
            session=session,
            sequence=sequence,
            tool_registry=tool_registry,
            tool_call=plan_tool_call,
            tool=tool,
            is_resume=is_resume,
        )

        if input_hook_outcome.action != "unchanged":
            policy = hook_execution_policy_from_metadata(session.metadata)
            trace_payload: dict[str, object] = {
                "surface": "typed_input",
                "session_id": session.session.id,
                "tool_name": original_tool_call.tool_name,
                "hook_status": "blocked" if input_hook_outcome.action == "block" else "ok",
                "policy": {"mode": policy.mode, "read_only": policy.read_only},
                "action": input_hook_outcome.action,
                "handler_names": list(input_hook_outcome.handler_names),
                "diagnostics": list(input_hook_outcome.diagnostics),
            }
            if input_hook_outcome.action == "rewrite":
                trace_payload["rewrite"] = tool_input_rewrite_metadata(original=original_tool_call, outcome=input_hook_outcome)
            if input_hook_outcome.blocked_reason is not None:
                trace_payload["reason"] = input_hook_outcome.blocked_reason
            trace_event = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_TOOL_INPUT_PROCESSED,
                source="runtime",
                payload=trace_payload,
            )
            sequence = trace_event.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=trace_event)
        if input_hook_outcome.action == "block":
            return input_hook_outcome.tool_call, tool, tool_call_id, sequence, input_hook_outcome
        return input_hook_outcome.tool_call, tool, tool_call_id, sequence, input_hook_outcome

    def _resolve_permission_for_tool(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool: Tool,
        plan_tool_call: ToolCall,
        tool_call_id: str,
        approval_resolution: tuple[PendingApproval, PermissionResolution] | None,
        active_permission_policy: PermissionPolicy,
        effective_runtime_config: EffectiveRuntimeConfig,
        tool_results: list[ToolResult],
        continue_after_denial: bool = True,
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
            if denied_replayed_tool_changed or effective_runtime_config.execution_engine != "provider" or not continue_after_denial:
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
            surface: RuntimeHookSurface = "pre_tool" if phase == "pre" else "post_tool"
            failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                session=session,
                sequence=sequence,
                surface=surface,
                error=hook_outcome.failed_error,
                hooks=self._config.hooks,
            )
            if failed_chunk is not None:
                persisted_failed, _ = self._persist_chunk(failed_chunk)
                yield persisted_failed
                raise RuntimeError(hook_outcome.failed_error)
        self._note_hook_guidance(hook_outcome.guidance)
        if hook_outcome.action == "cancel":
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=(hook_blocked_reason(hook_outcome, tool_name=tool_name) if phase == "pre" else f"run cancelled by {phase}-tool hook"),
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
        plan_tool_call: ToolCall,
        tool: Tool,
        tool_call_id: str,
        tool_timeout: int | None,
        tool_results: list[ToolResult],
        active_graph_request: GraphRunRequest,
        tool_exception_recovery_enabled: bool,
    ) -> Generator[RuntimeStreamChunk, None, tuple[str, ToolResult | None, SessionState, int]]:
        sequence = yield from self._emit_started_tool_event(
            session=session,
            tool_call=plan_tool_call,
            tool_call_id=tool_call_id,
        )
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
            tool_outcome, sequence = yield from self._execute_resolved_tool_call(
                resolved_call=_ResolvedToolCall(
                    tool=tool,
                    tool_call=plan_tool_call,
                    tool_call_id=tool_call_id,
                ),
                read_paths=read_tracking.read_paths,
                read_lines=read_tracking.read_lines,
                tool_timeout=tool_timeout,
                session=session,
                start_sequence=sequence + 1,
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
                partial_result = exc.partial_result
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
                timeout_facts = _tool_timeout_execution_facts(exc)
                envelope = self._persist_event(
                    session_id=session.session.id,
                    event_type=RUNTIME_TOOL_TIMEOUT,
                    source="runtime",
                    payload={
                        "tool": plan_tool_call.tool_name,
                        "timeout_seconds": tool_timeout,
                        **timeout_facts,
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
                        **timeout_facts,
                        **_tool_error_payload(
                            tool_name=plan_tool_call.tool_name,
                            error=partial_timeout_error or exc.error_message,
                            error_kind="tool_timeout",
                            extra_details={
                                "timed_out": True,
                                "timeout_seconds": tool_timeout,
                                **timeout_facts,
                            },
                        ),
                        "display": failed_display,
                        "tool_status": failed_status,
                    },
                )
                sequence = envelope.sequence
                yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
                failed_chunk, _ = self._persist_chunk(
                    chunk_builders.failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=exc.error_message,
                        payload={
                            "kind": "tool_timeout",
                            "tool": plan_tool_call.tool_name,
                            "timeout_seconds": tool_timeout,
                            **timeout_facts,
                        },
                    )
                )
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
            error_kind: str | None = None
            error_details: dict[str, object] = {}
            retry_guidance: str | None = _tool_error_retry_guidance(str(exc))
            if isinstance(exc, ToolDiagnosticError):
                error_kind = exc.error_kind
                error_details = dict(exc.error_details)
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
                diagnostics=ToolDiagnostics(
                    kind=error_kind,
                    summary=_tool_error_summary(str(exc)),
                    details={"tool_name": plan_tool_call.tool_name, **error_details},
                    guidance=retry_guidance,
                ),
            )
        return "ok", tool_result, session, sequence

    def _finalize_tool_result(
        self,
        *,
        session: SessionState,
        sequence: int,
        plan_tool_call: ToolCall,
        tool_call_id: str,
        tool_result: ToolResult,
        active_graph_request: GraphRunRequest,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolResult, bool, dict[str, object], SessionState, int, bool]]:
        tool_result, runtime_tool_result_data = _normalized_tool_result(
            tool_result=tool_result,
            session=session,
            plan_tool_call=plan_tool_call,
            sequence=sequence,
            tool_call_id=tool_call_id,
        )
        todo_mutated = plan_tool_call.tool_name == "todo" and tool_result.status == "ok" and runtime_tool_result_data.get("mutated") is True
        tool_result = self._number_yield_progress(session=session, tool_result=tool_result)
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
            return tool_result, todo_mutated, runtime_tool_result_data, session, sequence, True
        return tool_result, todo_mutated, runtime_tool_result_data, session, sequence, False

    def _handle_question_outcome(
        self,
        *,
        session: SessionState,
        plan_tool_call: ToolCall,
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
            try:
                hook_outcome = run_lifecycle_hooks_for_session(
                    hooks=self._config.hooks,
                    workspace=self._workspace,
                    session=session,
                    surface="question_asked",
                    recursion_env_var=HOOK_RECURSION_ENV_VAR,
                    sequence=envelope.sequence,
                    payload={
                        "request_id": pending_question.request_id,
                        "tool": pending_question.tool_name,
                        "question_count": len(pending_question.prompts),
                        "argument_keys": sorted(dict(plan_tool_call.arguments)),
                        "arguments_sha256": tool_input_arguments_sha256(dict(plan_tool_call.arguments)),
                    },
                    policy=hook_execution_policy_from_metadata(session.metadata),
                )
            except Exception as exc:
                logging.getLogger(__name__).warning("question_asked hook failed: %s", exc)
                return True
            yield from self._persist_chunks(
                hook_outcome.chunks,
                fallback_sequence=hook_outcome.last_sequence,
            )
            if hook_outcome.failed_error is not None:
                return True
            self._note_hook_guidance(hook_outcome.guidance)
            return True
        return False

    def _emit_tool_completed_events(
        self,
        *,
        session: SessionState,
        sequence: int,
        plan_tool_call: ToolCall,
        tool_call_id: str,
        sanitized_arguments: dict[str, object],
        tool_result: ToolResult,
        runtime_tool_result_data: dict[str, object],
        todo_mutated: bool,
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
        session = clear_tool_execution_intent(self._session_store, self._workspace, session)

        if plan_tool_call.tool_name == "skill" and tool_result.status == "ok":
            skill_payload = completed_payload.get("skill")
            if isinstance(skill_payload, dict):
                typed_skill_payload = skill_payload
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

        if plan_tool_call.tool_name == "todo" and todo_mutated:
            revision = sequence + 1
            raw_phases = runtime_tool_result_data.get("phases")
            session, todo_payload = session_with_todo_state(
                session,
                raw_phases=raw_phases,
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
        extra_details: dict[str, object] | None = None,
    ) -> Generator[RuntimeStreamChunk, None, int]:
        """Emit a failed ``runtime.tool_completed`` and append an error result.

        Tool-level feedback for dispatched tools: the run continues after an
        unknown, denied, or hook-cancelled dispatch instead of failing the
        session. ``extra_details`` carries additive execution facts into both
        the persisted payload and the model-visible tool result.
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
                **dict(extra_details or {}),
            },
            diagnostics=ToolDiagnostics(
                kind=error_kind,
                summary=_tool_error_summary(error),
                details=_tool_error_details(tool_name=tool_name, extra=extra_details),
                guidance="Check the tool name and arguments, then retry.",
            ),
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
                "diagnostics": tool_result.diagnostics.as_payload() if tool_result.diagnostics is not None else None,
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
        is_resume: bool = False,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int]]:
        """Execute an ``invoke_tool(name, arguments)`` dispatch call.

        The inner tool is resolved from the runtime registry and executed
        through the same boundary as a provider-native tool call: delegation
        policy, runtime policy (allowlist / read-only), permission resolution
        (approval pause for mutating tools), pre/post hooks, and the shared
        tool executor. Tool-level failures (unknown name, denied, cancelled)
        surface as model-visible feedback rather than terminating the run, with
        one exception: a pre-tool hook failing under ``hooks.failure_mode="fail"``
        escalates like the primary pre_tool path, so the gate cannot silently
        degrade.
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
            return session, sequence

        inner_name = parsed.name
        inner_arguments = dict(parsed.arguments or {})

        original_inner_call = ToolCall(
            tool_name=inner_name,
            arguments=dict(inner_arguments),
            tool_call_id=outer_call_id,
        )
        inner_call = original_inner_call
        tool_request_payload: dict[str, object] = {
            "tool": original_inner_call.tool_name,
            "arguments": dict(original_inner_call.arguments),
        }
        if original_inner_call.tool_name in WRITE_PREVIEW_TOOLS:
            try:
                diff_preview = build_tool_call_preview(
                    workspace=self._workspace,
                    tool_name=original_inner_call.tool_name,
                    arguments=original_inner_call.arguments,
                    phase="final",
                )
            except Exception:
                diff_preview = None
            if diff_preview is not None:
                tool_request_payload["diff_preview"] = diff_preview
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
            return session, sequence

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
            return session, sequence

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
            return session, sequence

        lookup_envelope = self._persist_event(
            session_id=session.session.id,
            event_type="runtime.tool_lookup_succeeded",
            source="runtime",
            payload={"tool": inner_name},
        )
        sequence = lookup_envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=lookup_envelope)

        inner_call, tool, input_hook_outcome = self._prepare_typed_tool_call(
            session=session,
            sequence=sequence,
            tool_registry=tool_registry,
            tool_call=inner_call,
            tool=tool,
            is_resume=is_resume,
        )

        if input_hook_outcome.action != "unchanged":
            policy = hook_execution_policy_from_metadata(session.metadata)
            trace_payload: dict[str, object] = {
                "surface": "typed_input",
                "session_id": session.session.id,
                "tool_name": inner_name,
                "hook_status": "blocked" if input_hook_outcome.action == "block" else "ok",
                "policy": {"mode": policy.mode, "read_only": policy.read_only},
                "action": input_hook_outcome.action,
                "handler_names": list(input_hook_outcome.handler_names),
                "diagnostics": list(input_hook_outcome.diagnostics),
            }
            if input_hook_outcome.action == "rewrite":
                trace_payload["rewrite"] = tool_input_rewrite_metadata(original=original_inner_call, outcome=input_hook_outcome)
            if input_hook_outcome.blocked_reason is not None:
                trace_payload["reason"] = input_hook_outcome.blocked_reason
            trace_event = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_TOOL_INPUT_PROCESSED,
                source="runtime",
                payload=trace_payload,
            )
            sequence = trace_event.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=trace_event)

        if input_hook_outcome.action == "block":
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(input_hook_outcome.tool_call.arguments),
                tool_results=tool_results,
                error=input_hook_outcome.blocked_reason or "tool input handler blocked the call",
                error_kind="tool_input_handler_blocked",
            )
            return session, sequence

        permission_action, session, sequence = yield from self._resolve_permission_for_tool(
            session=session,
            sequence=sequence,
            tool=tool,
            plan_tool_call=inner_call,
            tool_call_id=outer_call_id,
            approval_resolution=None,
            active_permission_policy=permission_policy or self._permission_policy,
            effective_runtime_config=runtime.effective_runtime_config_from_metadata(session.metadata),
            tool_results=tool_results,
            continue_after_denial=False,
        )
        if permission_action != "ok":
            return session, sequence

        inner_call, outer_call_id, _intent_payload, session = self._persist_resolved_tool_intent(
            session=session,
            tool=tool,
            tool_call=inner_call,
            tool_call_id=outer_call_id,
        )
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
            if chunk_builders.hook_failures_are_fatal(self._config.hooks):
                # Honor hooks.failure_mode=fail exactly like the primary pre_tool
                # path: persist the failure visibly, then escalate out of the
                # graph loop so the run fails instead of silently degrading the
                # gate. `warn` keeps the tool-level feedback below.
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
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                tool_results=tool_results,
                error=pre_hook_outcome.failed_error,
                error_kind="hook_failed",
            )
            return session, sequence
        self._note_hook_guidance(pre_hook_outcome.guidance)
        if pre_hook_outcome.action == "cancel":
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                tool_results=tool_results,
                error=hook_blocked_reason(pre_hook_outcome, tool_name=inner_name),
                error_kind="hook_cancelled",
            )
            return session, sequence

        tool_timeout = runtime.effective_runtime_config_from_metadata(session.metadata).tool_timeout_seconds
        sequence = yield from self._emit_started_tool_event(
            session=session,
            tool_call=inner_call,
            tool_call_id=outer_call_id,
        )
        if _is_abort_signal_requested(abort_signal):
            yield from self._started_tool_abort_chunks(
                session=session,
                sequence=sequence,
                tool_call=inner_call,
                tool_call_id=outer_call_id,
                abort_signal=abort_signal,
            )
            return session, sequence

        try:
            read_tracking = read_tracking_for_tool_results(
                tool_results=tuple(tool_results),
                workspace=self._workspace,
            )
            tool_outcome, sequence = yield from self._execute_resolved_tool_call(
                resolved_call=_ResolvedToolCall(
                    tool=tool,
                    tool_call=inner_call,
                    tool_call_id=outer_call_id,
                ),
                read_paths=read_tracking.read_paths,
                read_lines=read_tracking.read_lines,
                tool_timeout=tool_timeout,
                session=session,
                start_sequence=sequence + 1,
                abort_signal=abort_signal,
                parent_session_id=session.session.parent_id,
                delegation_depth=delegation_depth_from_metadata(session.metadata),
                remaining_spawn_budget=remaining_spawn_budget_from_metadata(session.metadata),
                model=session_model_identity(session.metadata)[0],
            )
            if isinstance(tool_outcome, Exception):
                raise tool_outcome
            tool_result = tool_outcome
        except RuntimeToolTimeoutError as exc:
            timeout_facts = _tool_timeout_execution_facts(exc)
            envelope = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_TOOL_TIMEOUT,
                source="runtime",
                payload={"tool": inner_name, "timeout_seconds": tool_timeout, **timeout_facts},
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                tool_results=tool_results,
                error=exc.error_message,
                error_kind="tool_timeout",
                extra_details=timeout_facts,
            )
            return session, sequence
        except Exception as exc:
            yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                tool_results=tool_results,
                error=str(exc),
                error_kind="tool_error",
            )
            return session, sequence

        runtime_tool_result_data = dict(tool_result.data)
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
        tool_result = self._number_yield_progress(session=session, tool_result=tool_result)

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
            return session, sequence

        completed_payload = {
            **_tool_completed_identity_payload(session),
            **tool_result.data,
            "tool_call_id": outer_call_id,
            "arguments": sanitized_arguments,
            "status": tool_result.status,
            "content": tool_result.content,
            "error": tool_result.error,
        }
        if tool_result.diagnostics is not None:
            completed_payload["diagnostics"] = tool_result.diagnostics.as_payload()
        completed_payload["tool"] = tool_result.tool_name

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
        session = clear_tool_execution_intent(self._session_store, self._workspace, session)

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
            return session, sequence

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
                return session, sequence
            self._note_hook_guidance(post_hook_outcome.guidance)

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
        if inner_name == "todo" and tool_result.status == "ok" and runtime_tool_result_data.get("mutated") is True:
            session, todo_payload = session_with_todo_state(
                session,
                raw_phases=runtime_tool_result_data.get("phases"),
                revision=sequence + 1,
            )
            todo_event = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_TODO_UPDATED,
                source="runtime",
                payload=todo_payload,
            )
            sequence = todo_event.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=todo_event)
        return session, sequence

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
            diagnostics=ToolDiagnostics(
                kind="permission_denied",
                summary=_tool_error_summary(error),
                details=_tool_error_details(
                    tool_name=tool_call.tool_name,
                    extra={
                        "permission_denied": True,
                        **({"denied_by": denied_by} if denied_by is not None else {}),
                    },
                ),
                guidance="Adjust the request or approval settings, then retry.",
            ),
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
                "diagnostics": tool_result.diagnostics.as_payload() if tool_result.diagnostics is not None else None,
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
    def _build_context_compacted_payload(
        context_window: RuntimeContextWindow,
    ) -> dict[str, object] | None:
        if not context_window.compacted:
            return None
        return {
            "reason": context_window.compaction_reason,
            "original_tool_result_count": context_window.original_tool_result_count,
            "retained_tool_result_count": context_window.retained_tool_result_count,
            "dropped_tool_result_count": context_window.dropped_tool_result_count,
            "truncated_tool_result_count": context_window.truncated_tool_result_count,
            "usage_tokens_before": context_window.usage_tokens_before,
            "usage_tokens_after": context_window.usage_tokens_after,
            "usage_tokens_estimated": context_window.estimate_won,
            "measured_anchor_tokens": context_window.measured_anchor_tokens,
            "estimated_delta_tokens": context_window.estimated_delta_tokens,
            "pruned_savings_tokens": context_window.pruned_savings_tokens,
            "compacted": True,
            "summary_anchor": context_window.summary_anchor,
            "projection_id": context_window.summary_anchor,
            "summary_source": context_window.summary_source,
            "summary_kind": context_window.summary_kind,
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
        return isinstance(graph, SafeBoundaryGraph) and graph.is_at_safe_boundary()

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
