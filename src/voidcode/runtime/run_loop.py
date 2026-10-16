from __future__ import annotations

import json
import logging
import time
from collections.abc import Generator, Iterable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NotRequired, TypedDict, cast
from uuid import uuid4

from pydantic import ValidationError

from ..core.engine import (
    CallOutcome,
    CallPaused,
    CallReported,
    CallStoppedAfterReport,
    CallStoppedBeforeReport,
    EngineState,
    TurnBatch,
    TurnEngine,
    TurnObservation,
)
from ..core.todos import TodoPhase
from ..core.tool_context import ArtifactBinary, ArtifactInvalid, ArtifactMissing, ArtifactUnavailable, ToolContext
from ..core.transcript import AssembledContext, ContextSegment, ToolResultView, project_report
from ..core.turns import (
    CallSeed,
    FinalTurn,
    ModelTurnFact,
    ReportedCall,
    StreamFact,
    StreamingTurnProducer,
    ToolCompletedFact,
    ToolRequestedFact,
    ToolTurn,
    TurnFact,
    TurnPlan,
    TurnProducer,
    TurnRequest,
    report_call,
)
from ..hook.config import RuntimeHookSurface
from ..hook.typed import (
    ToolInputEvent,
    ToolInputHandlerRegistry,
    ToolInputHookOutcome,
    tool_input_arguments_sha256,
    tool_input_rewrite_metadata,
    validate_tool_input_schema,
)
from ..provider.errors import ProviderExecutionError
from ..provider.protocol import (
    ProviderAbortSignal,
)
from ..tools._pydantic_args import format_validation_error
from ..tools._repair import ToolDiagnosticError
from ..tools.contracts import (
    AttachmentOutput,
    ConfirmedStop,
    EmptyOutput,
    ProgressYield,
    QuestionPrepared,
    RuntimeToolTimeoutError,
    TerminalYield,
    TerminalYieldFailure,
    TextOutput,
    Tool,
    ToolCall,
    ToolDefinition,
    ToolDiagnostics,
    ToolFailure,
    ToolInvocation,
    ToolResult,
    ToolSuccess,
    UnconfirmedStop,
    is_read_tier,
)
from ..tools.guards import read_tracking_for_tool_results
from ..tools.invoke_tool import InvokeToolArgs
from ..tools.output import (
    RASTER_IMAGE_MIMES,
    cap_tool_result_output,
    sanitize_tool_arguments,
)
from ..tools.question import QuestionTool
from ..tools.skill import SkillResultBody
from ..tools.todo import TodoResultBody
from .agent_capability import composition_ref_from_session_metadata
from .composition import LifecycleScope, PhaseContext
from .config.loading import RuntimeConfig
from .config.materializer import RunConfig
from .context.continuity import replayed_conversation_segments_from_segments
from .context.transforms import context_transform_applied_payloads
from .context.window import (
    BeforeCompactInput,
    ContextProjection,
    ContinuitySummaryKind,
    RuntimeAssembledContext,
    RuntimeContextWindow,
    continuity_summary_metadata,
)
from .contracts import RuntimeProviderContextPolicyDecision, RuntimeStreamChunk
from .event_envelopes import (
    ReasoningCaptureState,
    envelopes_for_acp_events,
    envelopes_for_lsp_events,
    envelopes_for_mcp_events,
)
from .events import (
    REASONING_PERSISTED_LIMIT_CHARS,
    RUNTIME_APPROVAL_RESOLVED,
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
    RUNTIME_TOOL_BATCH_REPEATED,
    RUNTIME_TOOL_INPUT_PROCESSED,
    RUNTIME_TOOL_PROGRESS,
    RUNTIME_TOOL_STARTED,
    RUNTIME_TOOL_TIMEOUT,
    EventEnvelope,
    EventSource,
    model_turn_batch_id,
    runtime_reasoning_part_from_provider_stream,
    runtime_reasoning_part_payload,
)
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
from .execution.report_codec import report_payload
from .execution.seams import (
    ContextLimitPromotion,
    RuntimeTurnProducerSelection,
    context_limit_promotion_for_provider_error,
    fallback_turn_producer_for_provider_error,
    select_turn_producer_for_config,
)
from .execution.tool_executor import RuntimeToolExecutor
from .execution.tool_replay import ToolExecutionIntent
from .execution.tool_result_projection import (
    _fit_numbered_progress_payload,
    _is_terminal_yield_result,
    _progress_payload_size,
    _serialized_tool_results,
    _tool_error_details,
    _tool_error_diagnostics,
    _tool_error_retry_guidance,
    _tool_error_summary,
)
from .execution.turn_adapter import turn_request_for_session, turn_session_snapshot
from .execution.turn_recovery import AnsweredQuestion, ApprovedInvocation, RuntimeContinuation, persisted_turn_batch, validated_composition_turn
from .execution_ownership import ExecutionOwnershipRevokedError
from .fact_codec import encode_fact, reported_call_from_event
from .fact_store import SqliteFactStore
from .hook_runtime import (
    HOOK_RECURSION_ENV_VAR,
    before_compact_input_from_hook_outcome,
    hook_blocked_reason,
    hook_execution_policy_from_metadata,
    run_lifecycle_hooks_for_session,
    run_tool_hooks_for_session,
)
from .permissions.policy import PendingApproval, PermissionPolicy, runtime_effective_read_only_from_metadata
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
from .schema_validation import validate_structured_output
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
    runtime_state_value,
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
from .storage import SessionEventRepository, SessionRecoveryRepository, SessionRepository
from .todos import runtime_todo_phases_from_payload, todo_state_from_session_metadata
from .tool_call_preview import PREVIEW_SNAPSHOT_MAX_BYTES, WRITE_PREVIEW_TOOLS, build_partial_tool_call_preview, build_tool_call_preview
from .tool_display import build_tool_display, build_tool_status
from .tools.scope import tool_policy_error

if TYPE_CHECKING:
    from .acp import AcpAdapter
    from .lsp import LspManager
    from .mcp import McpManager
    from .providers.catalog_query import RuntimeProviderCatalogQuery
    from .runtime_surface import RuntimeSurface
    from .tools.registry import ToolRegistry

from .execution import chunk_builders

logger = logging.getLogger(__name__)


class ToolBatchRepetition:
    """Count consecutive identical complete batches within one engine run."""

    def __init__(self) -> None:
        self._previous: tuple[str, ...] | None = None
        self._repeat_count = 0

    def reset(self) -> None:
        self._previous = None
        self._repeat_count = 0

    def observe(self, batch: TurnBatch) -> int | None:
        if len(batch.results) != len(batch.calls):
            return None
        if not batch.results:
            self.reset()
            return None
        observation: list[str] = []
        for report in batch.results:
            payload = report_payload(report)
            payload.pop("tool_call_id")
            observation.append(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False))
        current = tuple(observation)
        if current != self._previous:
            self._previous = current
            self._repeat_count = 1
            return None
        self._repeat_count += 1
        return self._repeat_count

    @staticmethod
    def tool_names(batch: TurnBatch) -> tuple[str, ...]:
        return tuple(report.final_tool_name for report in batch.results)


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
    config = runtime.run_config_from_metadata(session.metadata)
    active_target = config.resolved_provider.active_target.selection
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
    turn_plan: TurnPlan,
    is_final_step: bool,
    provider_attempt: int,
    record_provider_usage: bool = True,
) -> tuple[SessionState, int, SessionStatus]:
    """Final-step metadata resets and terminal-status derivation.

    Pure projection of the finalize chain: optionally record provider usage,
    reset retry/attempt cursors, and derive terminal status (keep-alive turns
    park ``interrupted``; one-shot children ``completed``). ``finish()`` skips
    usage because its terminal-only projection follows ``observe()`` for the same turn.
    """
    if record_provider_usage:
        session = session_with_provider_usage_metadata(
            session,
            turn_plan.provider_usage,
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
    request: TurnRequest,
) -> tuple[ContextSegment, ...]:
    assembled_context = request.assembled_context
    return replayed_conversation_segments_from_segments(assembled_context.segments)


def _turn_request_without_provider_attempt(
    request: TurnRequest,
    *,
    session: SessionState,
) -> TurnRequest:
    return turn_request_for_session(
        TurnRequest(
            session=turn_session_snapshot(session),
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


def _hydrate_reported_call_media(report: ReportedCall, *, session_id: str, runtime: RuntimeSurface) -> ReportedCall:

    output = report.result.output
    if not isinstance(output, AttachmentOutput):
        return report
    if output.mime not in RASTER_IMAGE_MIMES and output.mime != "application/pdf":
        unavailable = f"[Attachment MIME {output.mime} is not supported as model media]"
        return replace(report, result=replace(report.result, output=replace(output, data_uri=None, presentation=unavailable)))
    if output.data_uri:
        return report
    reference = output.bounds.reference
    reference_artifact = reference.artifact if reference is not None else None
    artifact = None
    if reference_artifact is not None:
        nested_output = reference_artifact.get("output_artifact")
        nested_artifact = reference_artifact.get("artifact")
        candidate = nested_output if isinstance(nested_output, Mapping) else nested_artifact
        artifact = candidate if isinstance(candidate, Mapping) else reference_artifact
    artifact_id = artifact.get("artifact_id") if artifact is not None else None
    artifact_session = artifact.get("session_id") if artifact is not None else None
    source_call_id = artifact.get("tool_call_id") if artifact is not None else None
    if not isinstance(artifact_id, str) or artifact_session != session_id:
        unavailable = "attachment artifact is missing or belongs to another session"
    else:
        artifact_result = runtime.read_tool_output_artifact(
            session_id=session_id,
            artifact_id=artifact_id,
            tool_call_id=source_call_id if isinstance(source_call_id, str) else None,
        )
        if isinstance(artifact_result, ArtifactBinary):
            if artifact_result.mime != output.mime:
                unavailable = "attachment artifact MIME does not match the report"
            else:
                if artifact_result.data_uri is not None:
                    presentation = output.presentation
                    if presentation is not None and "requires runtime hydration" in presentation:
                        presentation = f"{output.mime} attachment hydrated from {reference.uri if reference is not None else artifact_id}"
                    return replace(
                        report,
                        result=replace(
                            report.result,
                            output=replace(
                                output,
                                data_uri=artifact_result.data_uri,
                                presentation=presentation,
                            ),
                        ),
                    )
                presentation = output.presentation
                if not presentation:
                    raw_uri = artifact.get("uri") if artifact is not None else None
                    if isinstance(raw_uri, str) and raw_uri.startswith("voidcode://artifact/"):
                        presentation = (
                            f"{artifact_result.mime} attachment ({artifact_result.byte_count} bytes, SHA-256 {artifact_result.sha256}); {raw_uri}"
                        )
                    else:
                        unavailable = "attachment artifact has no canonical raw URI"
                if presentation:
                    return replace(
                        report,
                        result=replace(
                            report.result,
                            output=replace(output, data_uri=None, presentation=presentation),
                        ),
                    )
        elif isinstance(artifact_result, (ArtifactMissing, ArtifactInvalid, ArtifactUnavailable)):
            reason = artifact_result.reason if isinstance(artifact_result, (ArtifactInvalid, ArtifactUnavailable)) else "artifact not found"
            unavailable = f"attachment artifact unavailable: {reason}"
        else:
            unavailable = "attachment reference does not resolve to binary media"
    missing_presentation = f"[{unavailable}: {reference.uri if reference is not None else 'no artifact reference'}]"
    return replace(report, result=replace(report.result, output=replace(output, data_uri=None, presentation=missing_presentation)))


def _provider_attempt_reset_after_tool_result(
    *,
    provider_attempt: int,
    selection: RuntimeTurnProducerSelection | None,
    turn_request: TurnRequest,
    session: SessionState,
) -> _ProviderAttemptReset | None:
    if provider_attempt == 0:
        return None
    if selection is None:
        return None
    clean_session = _session_without_provider_attempt(session)
    clean_request = _turn_request_without_provider_attempt(
        turn_request,
        session=clean_session,
    )
    return _ProviderAttemptReset(
        provider_attempt=selection.provider_attempt,
        producer=selection.producer,
        turn_request=clean_request,
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
    producer: TurnProducer
    turn_request: TurnRequest
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


@dataclass(frozen=True, slots=True)
class PermissionDenialBody:
    approval_request_id: str | None = None
    path_scope: str | None = None
    operation_class: str | None = None
    canonical_path: str | None = None
    matched_rule: str | None = None
    policy_surface: str | None = None
    denied_by: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"permission_denied": True}
        for key in (
            "approval_request_id",
            "path_scope",
            "operation_class",
            "canonical_path",
            "matched_rule",
            "policy_surface",
            "denied_by",
        ):
            if (value := getattr(self, key)) is not None:
                payload[key] = value
        if self.approval_request_id is not None:
            payload["approval_decision"] = "deny"
        return payload


def _is_tool_timeout_like_exception(exc: Exception) -> bool:
    return isinstance(exc, TimeoutError)


def _tool_timeout_execution_facts(exc: RuntimeToolTimeoutError) -> dict[str, object]:
    """Additive execution facts every timeout surface must carry.

    ``error_kind='tool_timeout'`` alone reads as "the call failed"; the facts
    say whether the runtime cancelled the invocation, whether it confirmed the
    execution stopped, and therefore what may be claimed about side effects.
    """
    return dict(exc.execution_facts())


def _is_abort_requested(request: TurnRequest) -> bool:
    return bool(request.abort_signal is not None and request.abort_signal.cancelled)


def _is_abort_signal_requested(abort_signal: ProviderAbortSignal | None) -> bool:
    return bool(abort_signal is not None and abort_signal.cancelled)


def _abort_signal_reason(abort_signal: ProviderAbortSignal | None) -> str | None:
    if abort_signal is None:
        return None
    return abort_signal.reason or None


def _abort_reason(request: TurnRequest) -> str | None:
    return _abort_signal_reason(request.abort_signal)


def _live_event_surfaces_output(event: TurnFact) -> bool:
    """Whether a live-only stream fact renders part of the provider response.

    Only content-bearing deltas and tool-call lifecycle events reach the client as
    assistant output. ``error``/``done`` markers carry no output, so they must not
    block a retry/fallback.
    """
    if not isinstance(event, StreamFact):
        return False
    if event.kind == "provider_stream":
        return event.event.kind in {"delta", "content"} and event.event.channel in {"text", "reasoning"} and bool(event.event.text)
    return True


class _ProviderErrorPolicyVerdict(TypedDict):
    action: Literal["exit", "reraise", "retry", "fallback"]
    exc: NotRequired[BaseException]
    provider_attempt: NotRequired[int]
    provider_retry_attempt: NotRequired[int]
    producer: NotRequired[TurnProducer]
    session: NotRequired[SessionState]
    turn_request: NotRequired[TurnRequest]


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
        events: SessionEventRepository,
        sessions: SessionRepository,
        recovery: SessionRecoveryRepository,
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
        self._events = events
        self._sessions = sessions
        self._recovery = recovery
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
        self._composition_invocations: set[tuple[str, str, str]] = set()

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
        approval_checkpoint_plan_state: dict[str, object] | None = None,
    ) -> tuple[EventEnvelope, ...]:
        return self._events.append_session_events(
            workspace=self._workspace,
            session_id=session_id,
            events=events,
            approval_checkpoint_plan_state=approval_checkpoint_plan_state,
        )

    def _persist_event(
        self,
        *,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
        approval_checkpoint_plan_state: dict[str, object] | None = None,
    ) -> EventEnvelope:
        return self._persist_events(
            session_id=session_id,
            events=((event_type, source, payload, dedupe_key),),
            approval_checkpoint_plan_state=approval_checkpoint_plan_state,
        )[0]

    def _fresh_composition_session(self, session: SessionState) -> SessionState:
        return replace(session, metadata=self._surface.composition_session_metadata(session.session.id, session.metadata))

    def _composition_turn_context(self, session: SessionState) -> PhaseContext:
        raw = validated_composition_turn(session.metadata, session_id=session.session.id)
        return PhaseContext(self._workspace, session.session.id, cast(str, raw["run_id"]), "turn", cast(str, raw["scope_id"]))

    def _tool_composition_phase(
        self, name: str, value: ToolCall | ReportedCall, session: SessionState, *, restore_only: bool = False
    ) -> SessionState:
        turn_context = self._composition_turn_context(session)
        call_id = value.tool_call_id
        if not isinstance(call_id, str) or not call_id.strip():
            raise ValueError("tool lifecycle requires its actual call identity")
        context = PhaseContext(self._workspace, session.session.id, turn_context.run_id, "tool", f"{turn_context.scope_id}:{call_id}")
        self._surface.run_composition_phase(
            name, value, scope="tool", scope_id=context.scope_id, session_id=session.session.id, context=context, restore_only=restore_only
        )
        return self._fresh_composition_session(session)

    def _begin_composition_tool(self, session: SessionState, call: ToolCall) -> SessionState:
        session = self._fresh_composition_session(session)
        session = self._tool_composition_phase("before_tool", call, session)
        turn = runtime_state_value(session.metadata, "composition_turn")
        assert isinstance(turn, dict)
        call_id = call.tool_call_id
        assert call_id is not None
        invoked_ids = cast(list[str], turn["invoked_call_ids"])
        if call_id not in invoked_ids:
            session = replace(
                session,
                metadata=session_metadata_with_runtime_state_updates(
                    session.metadata, updates={"composition_turn": {**turn, "invoked_call_ids": [*invoked_ids, call_id]}}
                ),
            )
            self._sessions.update_session_metadata(workspace=self._workspace, session_id=session.session.id, metadata=session.metadata)
        self._composition_invocations.add((session.session.id, self._composition_turn_context(session).scope_id, call_id))
        return session

    def _complete_composition_turn(self, session: SessionState, plan: TurnPlan) -> SessionState:
        session = self._fresh_composition_session(session)
        raw = runtime_state_value(session.metadata, "composition_turn")
        context = self._composition_turn_context(session)
        assert isinstance(raw, dict)
        if raw.get("completed") is True:
            return session
        self._surface.run_composition_phase(
            "after_turn", plan, scope="turn", scope_id=context.scope_id, session_id=session.session.id, context=context
        )
        session = self._fresh_composition_session(session)
        session = replace(
            session, metadata=session_metadata_with_runtime_state_updates(session.metadata, updates={"composition_turn": {**raw, "completed": True}})
        )
        self._sessions.update_session_metadata(workspace=self._workspace, session_id=session.session.id, metadata=session.metadata)
        return session

    def _fact_store(self, session: SessionState) -> SqliteFactStore:
        return SqliteFactStore(
            events=self._events, recovery=self._recovery, workspace=self._workspace, session_id=session.session.id, session=session
        )

    def _persist_fact(self, *, session: SessionState, fact: TurnFact) -> EventEnvelope:
        session = self._fresh_composition_session(session)
        if isinstance(fact, ToolCompletedFact):
            invocation_key = (session.session.id, self._composition_turn_context(session).scope_id, fact.report.tool_call_id)
            if invocation_key in self._composition_invocations:
                session = self._tool_composition_phase("after_tool", fact.report, session)
                self._composition_invocations.remove(invocation_key)
            raw_batch = runtime_state_value(session.metadata, "turn_batch")
            if isinstance(raw_batch, dict):
                calls = raw_batch.get("calls")
                if isinstance(calls, list) and calls and isinstance(calls[-1], dict) and calls[-1].get("tool_call_id") == fact.report.tool_call_id:
                    plan = ToolTurn(
                        calls=tuple(
                            ToolCall(tool_name=call["tool_name"], tool_call_id=call["tool_call_id"], arguments=call["arguments"]) for call in calls
                        ),
                        reasoning=raw_batch.get("reasoning"),
                    )
                    session = self._complete_composition_turn(session, plan)
        return self._fact_store(session).append_for_publication((fact,))[0]

    def _persist_chunk(self, chunk: RuntimeStreamChunk) -> tuple[RuntimeStreamChunk, int]:
        event = chunk.event
        if event is None:
            return chunk, 0
        plan_state = chunk.session.metadata.get("plan_state") if event.event_type == RUNTIME_APPROVAL_RESOLVED else None
        envelope = self._persist_event(
            session_id=event.session_id,
            event_type=event.event_type,
            source=event.source,
            payload=event.payload,
            approval_checkpoint_plan_state=plan_state if isinstance(plan_state, dict) else None,
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
            persisted_chunk, persisted_sequence = self._persist_chunk(chunk)
            if chunk.event is not None:
                sequence = persisted_sequence
            yield persisted_chunk
        return sequence

    def _number_yield_progress(self, *, session: SessionState, tool_result: ToolResult) -> ToolResult:
        if tool_result.tool_name != "yield" or not isinstance(tool_result, ToolSuccess) or not isinstance(tool_result.control, ProgressYield):
            return tool_result
        control = tool_result.control
        # The event log is the sole ordinal/retention authority. Storage
        # failures must propagate rather than silently restarting at ordinal 1.
        stored = self._sessions.load_session(workspace=self._workspace, session_id=session.session.id)
        prior_progress: list[ProgressYield] = []
        for event in stored.events:
            report = reported_call_from_event(event)
            if (
                report is not None
                and report.final_tool_name == "yield"
                and isinstance(report.result, ToolSuccess)
                and isinstance(report.result.control, ProgressYield)
            ):
                prior_progress.append(report.result.control)
        prior_count = len(prior_progress)
        prior_bytes = sum(_progress_payload_size(item.as_payload()) for item in prior_progress)
        from ..tools.yield_tool import YIELD_PROGRESS_MAX_RETAINED_CHARS, YIELD_PROGRESS_MAX_SECTION_CHARS, YIELD_PROGRESS_MAX_SECTIONS

        if prior_count >= YIELD_PROGRESS_MAX_SECTIONS:
            raise ValueError(f"yield progress limit exceeded: at most {YIELD_PROGRESS_MAX_SECTIONS} sections")
        raw_size = _progress_payload_size(control.as_payload())
        progress = control.as_payload()
        progress["data"] = {**control.data, "ordinal": prior_count + 1, "retained_chars": prior_bytes + raw_size}
        for _ in range(3):
            progress = _fit_numbered_progress_payload(progress, max_chars=YIELD_PROGRESS_MAX_SECTION_CHARS)
            final_size = _progress_payload_size(progress)
            if prior_bytes + final_size > YIELD_PROGRESS_MAX_RETAINED_CHARS:
                raise ValueError(f"yield progress limit exceeded: retained sections are capped at {YIELD_PROGRESS_MAX_RETAINED_CHARS} characters")
            updated_retained = prior_bytes + final_size
            data = cast(dict[str, object], progress["data"])
            if data.get("retained_chars") == updated_retained:
                break
            progress["data"] = {**data, "retained_chars": updated_retained}
        if _progress_payload_size(progress) > YIELD_PROGRESS_MAX_SECTION_CHARS:
            raise ValueError(f"yield progress section must be at most {YIELD_PROGRESS_MAX_SECTION_CHARS} characters")
        return replace(
            tool_result,
            control=replace(
                control,
                types=tuple(cast(list[str], progress["type"])),
                result=cast(str | None, progress["result"]),
                data=cast(dict[str, object], progress["data"]),
            ),
        )

    def _capture_interrupted_checkpoint(
        self,
        *,
        session: SessionState,
        prompt: str,
        tool_results: list[ReportedCall],
        last_event_sequence: int,
    ) -> None:
        current_turn_results = tool_results
        session = self._fresh_composition_session(session)
        self._recovery.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=session.session.id,
            prompt=prompt,
            session_metadata=session.metadata,
            tool_results=_serialized_tool_results(current_turn_results),
            last_event_sequence=last_event_sequence,
            composition_ref=composition_ref_from_session_metadata(session.metadata),
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
        result = ToolFailure(tool_name=tool_call.tool_name, error="run interrupted")
        report = report_call(
            replace(tool_call, tool_call_id=tool_call_id),
            result,
            final_tool_name=tool_call.tool_name,
            final_arguments=tool_call.arguments,
        )
        completion = self._persist_fact(session=session, fact=ToolCompletedFact(report))
        completed_chunk = RuntimeStreamChunk(kind="event", session=session, event=completion)
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
        read_lines: Mapping[tuple[str, str], frozenset[int]],
        read_whole_files: frozenset[tuple[str, str]],
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
        session = self._fresh_composition_session(session)
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
                read_whole_files=read_whole_files,
                read_hash=(
                    cast(str | None, resolved_call.tool_call.arguments.get("expectedHash"))
                    if isinstance(resolved_call.tool_call.arguments.get("expectedHash"), str)
                    else None
                ),
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
                result = completed.value
                if _is_abort_signal_requested(invocation.context.abort_signal):
                    stopped = ConfirmedStop(cancellation_signalled=True)
                    if isinstance(result, ToolSuccess):
                        result = ToolFailure(
                            tool_name=resolved_call.tool_call.tool_name,
                            error="run interrupted",
                            output=TextOutput("run interrupted"),
                            execution=stopped,
                        )
                    elif isinstance(result, ToolFailure) and result.execution is None:
                        result = replace(result, execution=stopped)
                delegation = session.metadata.get("delegation")
                output_schema = delegation.get("output_schema") if isinstance(delegation, Mapping) else None
                schema_mode = delegation.get("schema_mode") if isinstance(delegation, Mapping) else None
                if (
                    resolved_call.tool_call.tool_name == "yield"
                    and isinstance(result, ToolSuccess)
                    and isinstance(result.control, TerminalYield)
                    and isinstance(output_schema, dict)
                ):
                    if schema_mode == "strict":
                        validation = validate_structured_output(
                            data=dict(result.control.data),
                            schema=output_schema,
                            schema_mode="strict",
                        )
                        if not validation.valid:
                            result = ToolFailure(
                                tool_name=result.tool_name,
                                error=(
                                    "delegated child schema validation failed: "
                                    f"{validation.error or 'structured output does not satisfy the declared outputSchema'}"
                                ),
                                output=result.output,
                                control=TerminalYieldFailure(result.control.data),
                            )
                    elif schema_mode == "permissive":
                        _ = validate_structured_output(
                            data=dict(result.control.data),
                            schema=output_schema,
                            schema_mode="permissive",
                        )
                return result, sequence
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
        session = self._fresh_composition_session(session)
        if not isinstance(tool_call_id, str) or not tool_call_id.strip() or tool_call.tool_call_id != tool_call_id:
            raise ValueError("resolved tool intent requires its authentic normalized call identity")
        canonical_tool_call_id = tool_call_id
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
        persist_tool_execution_intent(self._sessions, self._workspace, session, intent_payload)
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

    def execute_turn_engine(
        self,
        *,
        producer: TurnProducer,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        turn_request: TurnRequest,
        tool_results: list[ReportedCall],
        permission_policy: PermissionPolicy | None = None,
        preserved_continuity_state: ContextProjection | None = None,
        continuation: RuntimeContinuation | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        run_id = turn_request.run_id or runtime_state_run_id(session.metadata)
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("execution requires its actual run identity")
        if turn_request.run_id != run_id:
            turn_request = replace(turn_request, run_id=run_id)
        host = RuntimeHost(
            self,
            producer=producer,
            tool_registry=tool_registry,
            session=session,
            sequence=sequence,
            turn_request=turn_request,
            tool_results=tool_results,
            permission_policy=permission_policy,
            preserved_continuity_state=preserved_continuity_state,
            continuation=continuation,
        )
        engine = TurnEngine(producer).run(
            turn_request, host=host, tool_results=tool_results, seed=continuation.batch if continuation is not None else None
        )
        interrupted_emitted = False
        failed_emitted = False
        try:
            entry_phases: tuple[tuple[str, LifecycleScope, str], ...] = (
                ("session_start", "session", session.session.id),
                ("before_run", "run", run_id),
            )
            for name, scope, scope_id in entry_phases:
                context = PhaseContext(self._workspace, session.session.id, run_id, scope, scope_id)
                restore_only = name == "session_start" and any(
                    record["scope"] == "session" and record["scope_id"] == scope_id
                    for record in self._surface.phase_records(session.session.id).values()
                )
                self._surface.run_composition_phase(
                    name, turn_request, scope=scope, scope_id=scope_id, session_id=session.session.id, context=context, restore_only=restore_only
                )
                host.session = self._fresh_composition_session(host.session)
            while True:
                try:
                    chunk = next(engine)
                except StopIteration as completed:
                    result = completed.value
                    break
                if chunk.event is not None and chunk.event.event_type == "runtime.failed":
                    interrupted_emitted = chunk.session.status == "interrupted"
                    failed_emitted = chunk.session.status == "failed"
                    if failed_emitted:
                        host.session = self._fresh_composition_session(chunk.session)
                        host.sequence = chunk.event.sequence
                        try:
                            host._terminal_composition_phases(chunk.event)
                        except Exception:
                            failed_emitted = False
                            raise
                        chunk = replace(chunk, session=self._fresh_composition_session(chunk.session))
                yield chunk
            if result.status == "aborted" and not interrupted_emitted:
                yield from self._emit_interrupted_failure(session=host.session, sequence=host.sequence, active_turn_request=host.active_turn_request)
        except Exception as error:
            if not failed_emitted and not interrupted_emitted and not _is_abort_requested(host.active_turn_request):
                try:
                    host._terminal_composition_phases(error)
                except Exception as terminal_error:
                    failure_text = f"{error}; terminal lifecycle failed: {terminal_error}"
                else:
                    failure_text = str(error)
                try:
                    host.session = self._fresh_composition_session(host.session)
                    failure, host.sequence = self._persist_chunk(
                        chunk_builders.failed_chunk(
                            session=host.session, sequence=host.sequence + 1, error=failure_text, payload={"kind": "execution_failed"}
                        )
                    )
                except ExecutionOwnershipRevokedError:
                    raise error from None
                except Exception as storage_error:
                    raise storage_error from error
                yield failure
            raise
        finally:
            engine.close()
            if host.state is not None:
                tool_results[:] = host.state.results

    def _capture_iteration_checkpoint(
        self,
        *,
        at_safe_boundary: bool,
        session: SessionState,
        turn_request: TurnRequest,
        tool_results: list[ReportedCall],
        sequence: int,
        checkpoint_tool_result_count: int,
    ) -> int:
        if len(tool_results) > checkpoint_tool_result_count and at_safe_boundary:
            self._capture_interrupted_checkpoint(session=session, prompt=turn_request.prompt, tool_results=tool_results, last_event_sequence=sequence)
            checkpoint_tool_result_count = len(tool_results)
        return checkpoint_tool_result_count

    def _yield_terminal(
        self,
        *,
        session: SessionState,
        tool_results: list[ReportedCall],
        sequence: int,  # noqa: ARG002 — retained for terminalization call-shape symmetry; persisted envelope owns sequence.
    ) -> Generator[RuntimeStreamChunk, None, int]:
        terminal_report = tool_results[-1]
        terminal_result = terminal_report.result
        control = terminal_result.control
        if isinstance(control, TerminalYieldFailure):
            assert isinstance(terminal_result, ToolFailure)
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=terminal_result.error,
                    payload={"kind": "delegated_yield_error", "terminal": True},
                )
            )
            yield failed_chunk
            return failed_chunk.event.sequence if failed_chunk.event is not None else sequence
        if not isinstance(control, TerminalYield) or not control.summary.strip():
            raise ValueError("yield completed without a non-empty terminal summary")
        terminal_output = control.summary
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
            event_type="core.response_ready",
            source="core",
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
        turn_plan: TurnPlan,
        current_chunk_session: SessionState,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, EventEnvelope | None]]:
        if any(isinstance(fact, (ToolRequestedFact, ToolCompletedFact)) for fact in turn_plan.facts):
            raise ValueError("producer facts cannot claim governed runtime tool execution")
        step_events = tuple(fact for fact in turn_plan.facts if not isinstance(fact, StreamFact))
        persisted_events = self._fact_store(session).append_for_publication(step_events)
        model_turn_events = tuple(event for event in persisted_events if event.event_type == "core.model_turn")
        if len(model_turn_events) > 1:
            raise ValueError("one provider step cannot persist multiple model-turn identities")
        for envelope in persisted_events:
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=current_chunk_session, event=envelope)
        return sequence, model_turn_events[0] if model_turn_events else None

    def _emit_final_step_artifacts(
        self,
        *,
        runtime: RuntimeSurface,
        session: SessionState,
        turn_plan: FinalTurn,
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
        yield RuntimeStreamChunk(kind="output", session=session, output=turn_plan.output)

    def _emit_interrupted_failure(
        self,
        *,
        session: SessionState,
        sequence: int,
        active_turn_request: TurnRequest,
    ) -> Generator[RuntimeStreamChunk]:
        failed_chunk, _ = self._persist_chunk(
            chunk_builders.failed_chunk(
                session=session,
                sequence=sequence + 1,
                error="run interrupted",
                payload=chunk_builders.user_interrupted_payload(
                    run_id=run_id_from_session_metadata(session.metadata),
                    reason=_abort_reason(active_turn_request),
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
        tool_results: list[ReportedCall],
        turn_index: int,
        provider_attempt: int,
        provider_retry_attempt: int,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, bool, tuple[str, ...]]]:
        turn_progress_payload: dict[str, object] = {
            "turn": turn_index,
            "tool_result_count": len(tool_results),
            "provider_attempt": provider_attempt,
            "provider_retry_attempt": provider_retry_attempt,
        }
        sequence, terminated, guidance = yield from self._run_turn_hook_phase(
            session=session,
            sequence=sequence,
            surface="turn_progress",
            payload=turn_progress_payload,
            cancel_message="run cancelled by turn-progress hook",
        )
        return sequence, terminated, () if terminated else guidance

    def _run_before_compact_hook_phase(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_results: tuple[ReportedCall, ...],
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
                    "tool_names": sorted({result.final_tool_name for result in tool_results}),
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
        active_turn_request: TurnRequest,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        session: SessionState,
        continuity_to_reinject: ContextProjection | None,
        first_iteration: bool,
        before_compact: BeforeCompactInput | None = None,
    ) -> tuple[RuntimeContextWindow, bool]:
        runtime = self._surface
        current_turn_request = active_turn_request
        current_prompt = current_turn_request.prompt
        current_abort_signal = current_turn_request.abort_signal
        current_session_metadata: dict[str, object] = session.metadata
        if first_iteration:
            # Boundary: the turn request's context window uses the provider Protocol,
            # which does not declare the runtime-only counters read below; the runtime
            # created this field, so the concrete window is the truth.
            prebuilt_context = cast(RuntimeContextWindow, current_turn_request.context_window)
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
        active_turn_request: TurnRequest,
        context_window: RuntimeContextWindow,
        session: SessionState,
        hook_guidance: Iterable[str] | None = None,
        reminder_segment: ContextSegment | None = None,
        before_compact: BeforeCompactInput | None = None,
        continuity_summary_override: str | None = None,
        continuity_summary_kind: ContinuitySummaryKind | None = None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, AssembledContext, RuntimeContextWindow]]:
        runtime = self._surface
        current_turn_request = active_turn_request
        current_prompt = current_turn_request.prompt
        session = session_with_context_window_metadata(session, context_window)
        persisted_skill_snapshot = skill_snapshot_from_metadata(session.metadata)
        skill_prompt_context = persisted_skill_snapshot.skill_prompt_context if persisted_skill_snapshot is not None else ""
        if (
            not skill_prompt_context
            and persisted_skill_snapshot is not None
            and persisted_skill_snapshot.source == "run"
            and current_turn_request.metadata.get("runtime_resume") is not True
            and current_turn_request.assembled_context is not None
        ):
            for segment in current_turn_request.assembled_context.segments:
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
            replayed_conversation_segments=_replayed_conversation_segments(current_turn_request),
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
        active_turn_request: TurnRequest,
        config: RunConfig,
        context_window: RuntimeContextWindow,
        continuity_to_reinject: ContextProjection | None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, bool]]:
        runtime = self._surface
        reinjected_continuity = continuity_to_reinject
        provider_context_policy_decision: RuntimeProviderContextPolicyDecision | None = runtime.provider_context_policy_decision_for_turn_request(
            turn_request=active_turn_request,
            config=config,
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
        active_turn_request: TurnRequest,
        turn_plan: TurnPlan,
        provider_attempt: int,
        tool_results: list[ReportedCall],
        complete: bool = True,
        record_provider_usage: bool = True,
    ) -> Generator[RuntimeStreamChunk, None, tuple[bool, SessionState, SessionState, int, bool]]:
        session = self._fresh_composition_session(session)
        is_final_step = complete and isinstance(turn_plan, FinalTurn)
        if is_final_step and session.session.parent_id is not None and (session.metadata.get("keep_alive_turn") is not True):
            if not tool_results or not _is_terminal_yield_result(tool_results[-1]):
                raise ValueError("delegated child must call yield before completing")
        if _is_abort_requested(active_turn_request):
            yield from self._emit_interrupted_failure(session=session, sequence=sequence, active_turn_request=active_turn_request)
            return (False, session, session, provider_attempt, True)
        session, provider_attempt, final_step_status = _finalized_step_session(
            session=session,
            turn_plan=turn_plan,
            is_final_step=is_final_step,
            provider_attempt=provider_attempt,
            record_provider_usage=record_provider_usage,
        )
        current_chunk_session = session
        if is_final_step:
            current_chunk_session = session_with_plan_state(
                SessionState(session=session.session, status=final_step_status, turn=session.turn, metadata=session.metadata),
                status=final_step_status,
            )
        return (is_final_step, session, current_chunk_session, provider_attempt, False)

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
            todo_tool_available=any(definition.name == TODO_REMINDER_KIND for definition in available_tools),
        )

    def _todo_mid_run_nudge_step(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_results: tuple[ReportedCall, ...],
        active_turn_request: TurnRequest,
        tool_registry: ToolRegistry,
        config: RunConfig,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, ContextSegment | None]]:
        """Insert one mid-run todo nudge before the next provider call.

        Upstream ``takeMidRunNudge``: while a turn is still running, a stale todo
        list (at least ``TODO_MID_RUN_MUTATION_THRESHOLD`` mutation-tool results
        since the last todo touch) earns a per-call nudge, at most
        ``TODO_MID_RUN_MAX_PER_CYCLE`` per cycle. It rides the same tail-segment
        channel as the completion reminder: never persisted, never in the cache
        prefix, and it does not touch output/tool-result semantics.
        """
        if not config.reminders.enabled:
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
            suppression=self._reminder_suppression(session=session, available_tools=active_turn_request.available_tools),
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
        tool_results: list[ReportedCall],
        available_tools: tuple[ToolDefinition, ...],
        config: RunConfig,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, ContextSegment | None]]:
        """Decide, persist and announce the terminal-turn todo reminder.

        Returns the per-call tail segment the continuation turn must append, or
        ``None`` when disabled, no unfinished todo exists, a previous reminder
        is still awaiting progress, the attempt budget is spent, the run is
        waiting on the user, a pending background task will re-wake the loop,
        or a delegated child must terminate through ``yield``.
        """
        if not config.reminders.enabled:
            return session, sequence, None
        todo_state = todo_state_from_session_metadata(session.metadata)
        phases = runtime_todo_phases_from_payload(todo_state["phases"]) if todo_state is not None else ()
        stored_state = todo_reminder_state_from_metadata(session.metadata)
        decision = decide_todo_reminder(
            max_per_cycle=config.reminders.todo.max_per_cycle,
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
        active_turn_request: TurnRequest,
        tool_results: tuple[ReportedCall, ...],
        session: SessionState,
        sequence: int,
        reasoning_capture_state: ReasoningCaptureState,
        producer: TurnProducer,
        attempt_stream_visibility: _AttemptStreamVisibility,
    ) -> Generator[RuntimeStreamChunk, None, tuple[Any | None, int, list[str]]]:
        turn_request = turn_request_for_session(active_turn_request, session)
        streamed_reasoning_texts: list[str] = []
        # A new provider attempt starts unseen; anything the previous attempt
        # surfaced was already handled by the error policy.
        attempt_stream_visibility.surfaced = False
        if _is_abort_requested(active_turn_request):
            yield from self._emit_interrupted_failure(
                session=session,
                sequence=sequence,
                active_turn_request=active_turn_request,
            )
            return None, sequence, streamed_reasoning_texts
        if active_turn_request.metadata.get("provider_stream") is True and isinstance(producer, StreamingTurnProducer):
            turn_plan = None
            partial_fragments: dict[str, list[str]] = {}
            partial_fragment_chars: dict[str, int] = {}
            partial_tool_names: dict[str, str] = {}

            def decorate_live_event(event: TurnFact) -> TurnFact:
                if not isinstance(event, StreamFact) or event.kind == "provider_stream":
                    return event
                raw_event = event.event
                call_id = raw_event.tool_call_id
                tool_name = event.tool_name or raw_event.tool_name
                tracking_id = call_id or f"anonymous:{tool_name or 'unknown'}"
                preview = event.diff_preview
                if call_id is not None or tool_name in WRITE_PREVIEW_TOOLS:
                    partial_tool_names[tracking_id] = tool_name or partial_tool_names.get(tracking_id, "")
                    fragments = partial_fragments.setdefault(tracking_id, [])
                    if raw_event.arguments_delta is not None:
                        chars = partial_fragment_chars.get(tracking_id, 0)
                        if chars < PREVIEW_SNAPSHOT_MAX_BYTES:
                            fragment = raw_event.arguments_delta[: PREVIEW_SNAPSHOT_MAX_BYTES - chars]
                            fragments.append(fragment)
                            partial_fragment_chars[tracking_id] = chars + len(fragment)
                    tool_name = partial_tool_names.get(tracking_id) or tool_name
                    if tool_name in WRITE_PREVIEW_TOOLS and preview is None:
                        try:
                            preview = build_partial_tool_call_preview(
                                workspace=self._workspace,
                                tool_name=tool_name,
                                argument_text="".join(fragments),
                                parsed_arguments=raw_event.parsed_arguments,
                            )
                        except Exception:
                            preview = None
                return replace(event, diff_preview=preview, tool_name=tool_name)

            stream_request = replace(
                turn_request,
                tool_call_preview=lambda tool_name, fragments, parsed: build_partial_tool_call_preview(
                    workspace=self._workspace,
                    tool_name=tool_name,
                    argument_text="".join(fragments),
                    parsed_arguments=parsed,
                ),
            )
            for streamed_item in producer.stream_produce(
                stream_request,
                tuple(project_report(report) for report in tool_results),
                session=turn_request.session,
            ):
                if _is_abort_requested(active_turn_request):
                    # Terminal-seal guard for provider deltas: once this
                    # run is interrupted, every remaining stream delta is
                    # a late event — drop it instead of streaming it to
                    # the client. Keep consuming the generator so a
                    # turn-producer-raised provider error (e.g. an abort-aware
                    # provider surfacing a ``cancelled`` failure) still
                    # propagates through the normal exception handler
                    # instead of being masked by the interrupt.
                    if not isinstance(streamed_item, TurnFact):
                        turn_plan = streamed_item
                    continue
                if isinstance(streamed_item, TurnFact):
                    streamed_item = decorate_live_event(streamed_item)
                    encoded = encode_fact(streamed_item, session=session)
                    if encoded.persistable:
                        raise ValueError("producer streaming yields live provider facts, not durable execution claims")
                    # Content-bearing live events are about to reach the client;
                    # from here on the attempt has user-visible stream output and
                    # must not be silently replayed (retry/fallback).
                    if _live_event_surfaces_output(streamed_item):
                        attempt_stream_visibility.surfaced = True
                    # Live deltas are client-only. Aggregate reasoning separately
                    # for one durable runtime.reasoning_part after the stream.
                    if streamed_item.kind == "provider_stream":
                        reasoning_capture_state.stream_observed = True
                        reasoning_payload = runtime_reasoning_part_from_provider_stream(encoded.payload)
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
                            event_type=encoded.event_type,
                            source=encoded.source,
                            payload=encoded.payload,
                        ),
                    )
                else:
                    turn_plan = streamed_item
            if turn_plan is None:
                raise RuntimeError("turn producer stream ended without a terminal step")
        else:
            turn_plan = producer.produce(
                turn_request,
                tool_results=tuple(project_report(report) for report in tool_results),
                session=turn_request.session,
            )
            # Non-streaming turns (background children) carry the turn's
            # reasoning on the step; aggregate it like the streamed deltas
            # so one bounded runtime.reasoning_part is persisted below.
            reasoning_text = turn_plan.reasoning
            if reasoning_text:
                reasoning_capture_state.stream_observed = True
                reasoning_capture_state.reasoning_observed = True
                reasoning_capture_state.part_count += 1
                reasoning_capture_state.text_char_count += len(reasoning_text)
                streamed_reasoning_texts.append(reasoning_text)
        return turn_plan, sequence, streamed_reasoning_texts

    def _context_limit_recovery_step(
        self,
        *,
        session: SessionState,
        sequence: int,  # noqa: ARG002 - the persisted envelope owns the sequence, mirroring the fallback branches.
        provider_error: ProviderExecutionError,
        active_turn_request: TurnRequest,
        context_window: RuntimeContextWindow,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        context_limit_recovery: _ContextLimitRecoveryState,
        producer: TurnProducer,
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
            prompt=active_turn_request.prompt,
            tool_results=tool_results,
            session_metadata=session.metadata,
            replayed_conversation_segments=_replayed_conversation_segments(active_turn_request),
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
        retry_request = turn_request_for_session(
            TurnRequest(
                session=turn_session_snapshot(session),
                prompt=active_turn_request.prompt,
                available_tools=active_turn_request.available_tools,
                context_window=window if window is not None else context_window,
                assembled_context=assembled,
                metadata=active_turn_request.metadata,
                abort_signal=active_turn_request.abort_signal,
                tool_call_preview=self._tool_call_preview,
                run_step=active_turn_request.run_step,
            ),
            session,
        )
        return {
            "action": "retry",
            "provider_attempt": provider_attempt_from_metadata(active_turn_request.metadata),
            "provider_retry_attempt": provider_retry_attempt_from_metadata(active_turn_request.metadata),
            "producer": producer,
            "session": session,
            "turn_request": retry_request,
        }

    def _apply_provider_error_policy(
        self,
        *,
        exc: Exception,
        session: SessionState,
        tool_results: tuple[ReportedCall | ToolResultView, ...],
        context_limit_recovery: _ContextLimitRecoveryState,
        sequence: int,
        active_turn_request: TurnRequest,
        context_window: RuntimeContextWindow,
        config: RunConfig,
        provider_attempt: int,
        provider_retry_attempt: int,
        current_metadata: dict[str, object],
        current_prompt: str,
        current_available_tools: tuple[ToolDefinition, ...],
        current_abort_signal: ProviderAbortSignal | None,
        producer: TurnProducer,
        attempt_stream_visibility: _AttemptStreamVisibility,
    ) -> Generator[RuntimeStreamChunk, None, _ProviderErrorPolicyVerdict]:
        current_provider_attempt = provider_attempt_from_metadata({"provider_attempt": provider_attempt})
        provider_error = exc if isinstance(exc, ProviderExecutionError) else None
        if provider_error is not None:
            fallback_selection = fallback_turn_producer_for_provider_error(
                error=provider_error,
                provider_chain=config.resolved_provider.target_chain,
                config=config,
                provider_attempt=current_provider_attempt,
            )
            transient_retry_config = provider_transient_retry_config(
                providers=config.providers,
                provider_name=provider_error.provider_name,
            )
            # The context_limit lane prefers a strictly larger-window candidate
            # (its own lane policy, see ``context_limit_promotion_for_provider_error``);
            # every other provider error keeps the generic chain-order selection.
            promotion: ContextLimitPromotion | None = None
            if provider_error.kind == "context_limit":
                promotion = context_limit_promotion_for_provider_error(
                    error=provider_error,
                    provider_chain=config.resolved_provider.target_chain,
                    config=config,
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
                            active_turn_request=active_turn_request,
                            context_window=context_window,
                            tool_results=tool_results,
                            context_limit_recovery=context_limit_recovery,
                            producer=producer,
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
                background_rate_limit_retry=(active_turn_request.metadata.get("background_rate_limit_retry") is True),
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
                active_turn_request = turn_request_for_session(
                    TurnRequest(
                        session=turn_session_snapshot(session),
                        prompt=current_prompt,
                        available_tools=current_available_tools,
                        context_window=context_window,
                        assembled_context=active_turn_request.assembled_context,
                        metadata=retry_metadata,
                        abort_signal=current_abort_signal,
                        tool_call_preview=self._tool_call_preview,
                        run_step=active_turn_request.run_step,
                    ),
                    session,
                )
                return {
                    "action": "retry",
                    "provider_attempt": current_provider_attempt,
                    "provider_retry_attempt": provider_retry_attempt,
                    "producer": producer,
                    "session": session,
                    "turn_request": active_turn_request,
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
                fallback_assembled_context: AssembledContext = active_turn_request.assembled_context
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
                producer = fallback_selection.producer
                active_turn_request = turn_request_for_session(
                    TurnRequest(
                        prompt=fallback_prompt,
                        session=turn_session_snapshot(session),
                        available_tools=fallback_available_tools,
                        context_window=fallback_context_window,
                        assembled_context=fallback_assembled_context,
                        metadata=fallback_metadata,
                        abort_signal=fallback_abort_signal,
                        tool_call_preview=self._tool_call_preview,
                        run_step=active_turn_request.run_step,
                    ),
                    session,
                )
                return {
                    "action": "fallback",
                    "provider_attempt": provider_attempt,
                    "provider_retry_attempt": provider_retry_attempt,
                    "producer": fallback_selection.producer,
                    "session": session,
                    "turn_request": active_turn_request,
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
        failed_chunk, _ = self._persist_chunk(
            chunk_builders.failed_chunk(
                session=session,
                sequence=sequence + 1,
                error=str(exc),
            )
        )
        yield failed_chunk
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
        outcome = self._surface.tool_input_handler_registry_for_session(session).apply(
            event=ToolInputEvent(
                session_id=session.session.id,
                tool_call=tool_call,
                tool=tool.definition,
                sequence=sequence,
                session_status=session.status,
                is_resume=is_resume,
                read_only=runtime_effective_read_only_from_metadata(session.metadata),
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
                    handlers=outcome.handlers,
                    omitted_handler_count=outcome.omitted_handler_count,
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
                    handlers=outcome.handlers,
                    omitted_handler_count=outcome.omitted_handler_count,
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
        turn_plan: ToolTurn,
        is_resume: bool = False,
        approved: ApprovedInvocation | None = None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolCall, Tool, str, int, ToolInputHookOutcome]]:
        runtime = self._surface
        plan_tool_call = turn_plan.calls[0]
        original_tool_call = plan_tool_call
        if approved is not None:
            if plan_tool_call.tool_call_id != approved.call.tool_call_id:
                raise ValueError("approved invocation does not match the original core call identity")
            plan_tool_call = approved.call
        explicit_tool_call_id = plan_tool_call.tool_call_id
        if not isinstance(explicit_tool_call_id, str) or not explicit_tool_call_id.strip():
            raise ValueError("planned tool requires its authentic normalized native call identity")
        tool_call_id = explicit_tool_call_id
        if approved is None:
            diff_preview = None
            if original_tool_call.tool_name in WRITE_PREVIEW_TOOLS:
                try:
                    diff_preview = build_tool_call_preview(
                        workspace=self._workspace, tool_name=original_tool_call.tool_name, arguments=original_tool_call.arguments, phase="final"
                    )
                except Exception:
                    diff_preview = None
            envelope = self._persist_fact(session=session, fact=ToolRequestedFact(original_tool_call, diff_preview=diff_preview))
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
                    payload={"kind": "runtime_tool_policy_denied", "tool": plan_tool_call.tool_name, "tool_policy": tool_policy_denial.metadata()},
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
            session_id=session.session.id, event_type="runtime.tool_lookup_succeeded", source="runtime", payload={"tool": plan_tool_call.tool_name}
        )
        sequence = lookup_envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=lookup_envelope)
        if approved is None:
            plan_tool_call, tool, input_hook_outcome = self._prepare_typed_tool_call(
                session=session, sequence=sequence, tool_registry=tool_registry, tool_call=plan_tool_call, tool=tool, is_resume=is_resume
            )
        else:
            validate_tool_input_schema(tool.definition, plan_tool_call.arguments)
            input_hook_outcome = ToolInputHookOutcome(tool_call=plan_tool_call)
        if input_hook_outcome.action != "unchanged":
            policy = hook_execution_policy_from_metadata(session.metadata)
            trace_payload: dict[str, object] = {
                "surface": "typed_input",
                "session_id": session.session.id,
                "tool_name": original_tool_call.tool_name,
                "hook_status": "blocked" if input_hook_outcome.action == "block" else "ok",
                "policy": {"read_only": policy.read_only},
            }
            trace_payload.update(input_hook_outcome.metadata_payload())
            if input_hook_outcome.action == "rewrite":
                trace_payload["rewrite"] = tool_input_rewrite_metadata(original=original_tool_call, outcome=input_hook_outcome)
            trace_event = self._persist_event(
                session_id=session.session.id, event_type=RUNTIME_TOOL_INPUT_PROCESSED, source="runtime", payload=trace_payload
            )
            sequence = trace_event.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=trace_event)
        if input_hook_outcome.action == "block":
            return (input_hook_outcome.tool_call, tool, tool_call_id, sequence, input_hook_outcome)
        return (input_hook_outcome.tool_call, tool, tool_call_id, sequence, input_hook_outcome)

    def _resolve_permission_for_tool(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool: Tool,
        plan_tool_call: ToolCall,
        tool_call_id: str,
        approved: ApprovedInvocation | None,
        active_permission_policy: PermissionPolicy,
        continue_after_denial: bool = True,
    ) -> Generator[RuntimeStreamChunk, None, tuple[str, SessionState, int, ReportedCall | None]]:
        runtime = self._surface
        if approved is not None:
            if plan_tool_call != approved.call or tool_call_id != approved.call.tool_call_id:
                raise ValueError("approved invocation no longer matches its trusted final call identity")
            permission_chunks = runtime.approval_resolution_outcome(
                session=session, pending=approved.pending, decision=approved.decision, sequence=sequence + 1
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
        sequence = yield from self._persist_chunks(permission_chunks.chunks, fallback_sequence=permission_chunks.last_sequence)
        if permission_chunks.pending_approval is not None:
            return "paused", session, sequence, None
        if permission_chunks.denied:
            sequence, result = yield from self._permission_denied_tool_feedback_chunks(
                session=session, tool_call=plan_tool_call, pending=permission_chunks.denied_approval, tool_call_id=tool_call_id
            )
            action = "stopped" if not continue_after_denial else "result"
            return action, session, sequence, result
        return "ok", session, sequence, None

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
        tool_results: list[ReportedCall],
        active_turn_request: TurnRequest,
    ) -> Generator[RuntimeStreamChunk, None, tuple[str, ToolResult | None, SessionState, int]]:
        sequence = yield from self._emit_started_tool_event(
            session=session,
            tool_call=plan_tool_call,
            tool_call_id=tool_call_id,
        )
        if _is_abort_requested(active_turn_request):
            yield from self._started_tool_abort_chunks(
                session=session,
                sequence=sequence,
                tool_call=plan_tool_call,
                tool_call_id=tool_call_id,
                abort_signal=active_turn_request.abort_signal,
            )
            return "returned", None, session, sequence
        session = self._begin_composition_tool(session, plan_tool_call)
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
                read_whole_files=read_tracking.read_whole_files,
                tool_timeout=tool_timeout,
                session=session,
                start_sequence=sequence + 1,
                abort_signal=active_turn_request.abort_signal,
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
                partial_timeout_output = EmptyOutput()
                partial_timeout_body = None
                partial_result = exc.partial_result
                if isinstance(partial_result, (ToolSuccess, ToolFailure)):
                    capped_partial = cap_tool_result_output(
                        partial_result,
                        session_id=session.session.id,
                        tool_call_id=tool_call_id,
                    )
                    partial_timeout_output = capped_partial.output
                    partial_timeout_body = capped_partial.body
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
                timeout_error = exc.error_message
                timeout_result = ToolFailure(
                    tool_name=plan_tool_call.tool_name,
                    error=timeout_error,
                    output=partial_timeout_output,
                    body=partial_timeout_body,
                    diagnostics=_tool_error_diagnostics(
                        tool_name=plan_tool_call.tool_name,
                        error=timeout_error,
                        error_kind="tool_timeout",
                        extra_details={"timed_out": True, "timeout_seconds": tool_timeout, **timeout_facts},
                    ),
                    execution=(
                        ConfirmedStop(cancellation_signalled=exc.cancellation_signalled)
                        if exc.execution_stopped
                        else UnconfirmedStop(cancellation_signalled=exc.cancellation_signalled)
                    ),
                    timeout_seconds=tool_timeout,
                )
                envelope = self._persist_fact(
                    session=session,
                    fact=ToolCompletedFact(
                        report_call(
                            replace(plan_tool_call, tool_call_id=tool_call_id),
                            timeout_result,
                            final_tool_name=plan_tool_call.tool_name,
                            final_arguments=plan_tool_call.arguments,
                        )
                    ),
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
            error_kind: str | None = None
            error_details: dict[str, object] = {}
            retry_guidance: str | None = _tool_error_retry_guidance(str(exc))
            if isinstance(exc, ToolDiagnosticError):
                error_kind = exc.error_kind
                error_details = dict(exc.error_details)
                retry_guidance = exc.retry_guidance

            tool_result = ToolFailure(
                tool_name=plan_tool_call.tool_name,
                error=str(exc),
                output=TextOutput(_tool_error_content(plan_tool_call.tool_name, str(exc))),
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
        tool_call_id: str,
        tool_result: ToolResult,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolResult, SessionState, int]]:
        session = self._fresh_composition_session(session)
        tool_result = cap_tool_result_output(
            tool_result,
            session_id=session.session.id,
            tool_call_id=tool_call_id,
        )
        tool_result = self._number_yield_progress(session=session, tool_result=tool_result)
        drained_chunks, session, _ = self._drain_runtime_events(
            session=session,
            start_sequence=sequence + 1,
        )
        yield from drained_chunks

        # The executor has already applied its bounded cancellation reap. It
        # returns a terminal result or an explicit unknown-stop result; persist
        # that result before sealing the run as interrupted.
        return tool_result, session, sequence

    def _handle_question_outcome(
        self,
        *,
        session: SessionState,
        plan_tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> Generator[RuntimeStreamChunk, None, bool]:
        session = self._fresh_composition_session(session)
        if (
            plan_tool_call.tool_name == QuestionTool.definition.name
            and isinstance(tool_result, ToolSuccess)
            and isinstance(tool_result.control, QuestionPrepared)
        ):
            pending_question = PendingQuestion(
                request_id=f"question-{uuid4().hex}",
                tool_name=plan_tool_call.tool_name,
                arguments=dict(plan_tool_call.arguments),
                prompts=tool_result.control.prompts,
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
                    "tool_call_id": plan_tool_call.tool_call_id,
                    "question_count": len(pending_question.prompts),
                    "questions": [
                        {
                            "header": prompt.header,
                            "question": prompt.question,
                            "multiple": prompt.multiple,
                            "allow_freeform": prompt.allow_freeform,
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
        tool_result: ToolResult,
        batch: CallSeed | None = None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, SessionState]]:
        envelope = self._persist_fact(
            session=session,
            fact=ToolCompletedFact(
                report_call(
                    replace(plan_tool_call, tool_call_id=tool_call_id),
                    tool_result,
                    final_tool_name=plan_tool_call.tool_name,
                    final_arguments=plan_tool_call.arguments,
                ),
                batch=batch,
            ),
        )
        sequence = envelope.sequence
        session = self._fresh_composition_session(session)
        session = clear_tool_execution_intent(self._sessions, self._workspace, session)
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        if plan_tool_call.tool_name == "skill" and isinstance(tool_result, ToolSuccess) and isinstance(tool_result.body, SkillResultBody):
            envelope = self._persist_event(
                session_id=session.session.id,
                event_type=RUNTIME_SKILL_LOADED,
                source="runtime",
                payload={"name": tool_result.body.name, "source": "tool", "source_path": tool_result.body.source_path},
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=envelope)

        if (
            plan_tool_call.tool_name == "todo"
            and isinstance(tool_result, ToolSuccess)
            and isinstance(tool_result.body, TodoResultBody)
            and tool_result.body.mutated
        ):
            session, todo_payload = session_with_todo_state(
                session,
                raw_phases=list(tool_result.body.phases),
                revision=sequence + 1,
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
        error: str,
        error_kind: str,
        extra_details: dict[str, object] | None = None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, ReportedCall]]:
        """Persist failed tool feedback and return the result for core advancement."""
        tool_result = ToolFailure(
            tool_name=tool_name,
            error=error,
            output=TextOutput(_tool_error_content(tool_name, error)),
            diagnostics=ToolDiagnostics(
                kind=error_kind,
                summary=_tool_error_summary(error),
                details=_tool_error_details(tool_name=tool_name, extra=extra_details),
                guidance="Check the tool name and arguments, then retry.",
            ),
        )
        call = ToolCall(tool_name=tool_name, arguments=arguments, tool_call_id=tool_call_id)
        report = report_call(call, tool_result, final_tool_name=tool_name, final_arguments=arguments)
        envelope = self._persist_fact(session=session, fact=ToolCompletedFact(report))
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return (envelope.sequence, report)

    def _execute_invoked_tool(
        self,
        *,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        outer_call: ToolCall,
        outer_call_id: str,
        tool_results: list[ReportedCall],
        permission_policy: PermissionPolicy | None,
        abort_signal: ProviderAbortSignal | None,
        is_resume: bool = False,
    ) -> Generator[RuntimeStreamChunk, None, tuple[SessionState, int, CallOutcome]]:
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
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name="invoke_tool",
                tool_call_id=outer_call_id,
                arguments=dict(outer_call.arguments),
                error=format_validation_error("invoke_tool", exc),
                error_kind="invalid_arguments",
            )
            return (session, sequence, CallReported(published_result))
        inner_name = parsed.name
        inner_arguments = dict(parsed.arguments or {})
        original_inner_call = ToolCall(tool_name=inner_name, arguments=dict(inner_arguments), tool_call_id=outer_call_id)
        inner_call = original_inner_call
        diff_preview = None
        if original_inner_call.tool_name in WRITE_PREVIEW_TOOLS:
            try:
                diff_preview = build_tool_call_preview(
                    workspace=self._workspace, tool_name=original_inner_call.tool_name, arguments=original_inner_call.arguments, phase="final"
                )
            except Exception:
                diff_preview = None
        envelope = self._persist_fact(session=session, fact=ToolRequestedFact(original_inner_call, diff_preview=diff_preview))
        sequence = envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        delegation_policy_error = runtime.delegation_tool_policy_error(session=session, tool_name=inner_name)
        if delegation_policy_error is not None:
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                error=delegation_policy_error,
                error_kind="delegation_policy_denied",
            )
            return (session, sequence, CallReported(published_result))
        tool_policy_denial = runtime.tool_policy_denial(session=session, tool_name=inner_name)
        if tool_policy_denial is not None:
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                error=tool_policy_error(tool_policy_denial),
                error_kind="runtime_tool_policy_denied",
            )
            return (session, sequence, CallReported(published_result))
        try:
            tool = tool_registry.resolve(inner_name)
        except Exception as exc:
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=inner_arguments,
                error=f"unknown tool: {inner_name} ({exc})",
                error_kind="unknown_tool",
            )
            return (session, sequence, CallReported(published_result))
        lookup_envelope = self._persist_event(
            session_id=session.session.id, event_type="runtime.tool_lookup_succeeded", source="runtime", payload={"tool": inner_name}
        )
        sequence = lookup_envelope.sequence
        yield RuntimeStreamChunk(kind="event", session=session, event=lookup_envelope)
        inner_call, tool, input_hook_outcome = self._prepare_typed_tool_call(
            session=session, sequence=sequence, tool_registry=tool_registry, tool_call=inner_call, tool=tool, is_resume=is_resume
        )
        if input_hook_outcome.action != "unchanged":
            policy = hook_execution_policy_from_metadata(session.metadata)
            trace_payload: dict[str, object] = {
                "surface": "typed_input",
                "session_id": session.session.id,
                "tool_name": inner_name,
                "hook_status": "blocked" if input_hook_outcome.action == "block" else "ok",
                "policy": {"read_only": policy.read_only},
            }
            trace_payload.update(input_hook_outcome.metadata_payload())
            if input_hook_outcome.action == "rewrite":
                trace_payload["rewrite"] = tool_input_rewrite_metadata(original=original_inner_call, outcome=input_hook_outcome)
            trace_event = self._persist_event(
                session_id=session.session.id, event_type=RUNTIME_TOOL_INPUT_PROCESSED, source="runtime", payload=trace_payload
            )
            sequence = trace_event.sequence
            yield RuntimeStreamChunk(kind="event", session=session, event=trace_event)
        if input_hook_outcome.action == "block":
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(input_hook_outcome.tool_call.arguments),
                error=input_hook_outcome.blocked_reason or "tool input handler blocked the call",
                error_kind="tool_input_handler_blocked",
            )
            return (session, sequence, CallReported(published_result))
        permission_action, session, sequence, permission_result = yield from self._resolve_permission_for_tool(
            session=session,
            sequence=sequence,
            tool=tool,
            plan_tool_call=inner_call,
            tool_call_id=outer_call_id,
            approved=None,
            active_permission_policy=permission_policy or self._permission_policy,
        )
        if permission_action != "ok":
            if permission_result is not None:
                return (
                    session,
                    sequence,
                    CallStoppedAfterReport(permission_result) if permission_action == "stopped" else CallReported(permission_result),
                )
            return (session, sequence, CallPaused() if permission_action == "paused" else CallStoppedBeforeReport())
        inner_call, outer_call_id, _intent_payload, session = self._persist_resolved_tool_intent(
            session=session, tool=tool, tool_call=inner_call, tool_call_id=outer_call_id
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
        sequence = yield from self._persist_chunks(pre_hook_outcome.chunks, fallback_sequence=pre_hook_outcome.last_sequence)
        if pre_hook_outcome.failed_error is not None:
            if chunk_builders.hook_failures_are_fatal(self._config.hooks):
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=session, sequence=sequence, surface="pre_tool", error=pre_hook_outcome.failed_error, hooks=self._config.hooks
                )
                if failed_chunk is not None:
                    persisted_failed, _ = self._persist_chunk(failed_chunk)
                    yield persisted_failed
                raise RuntimeError(pre_hook_outcome.failed_error)
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                error=pre_hook_outcome.failed_error,
                error_kind="hook_failed",
            )
            return (session, sequence, CallReported(published_result))
        self._note_hook_guidance(pre_hook_outcome.guidance)
        if pre_hook_outcome.action == "cancel":
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                error=hook_blocked_reason(pre_hook_outcome, tool_name=inner_name),
                error_kind="hook_cancelled",
            )
            return (session, sequence, CallReported(published_result))
        tool_timeout = runtime.run_config_from_metadata(session.metadata).tool_timeout_seconds
        sequence = yield from self._emit_started_tool_event(session=session, tool_call=inner_call, tool_call_id=outer_call_id)
        if _is_abort_signal_requested(abort_signal):
            yield from self._started_tool_abort_chunks(
                session=session, sequence=sequence, tool_call=inner_call, tool_call_id=outer_call_id, abort_signal=abort_signal
            )
            return (session, sequence, CallStoppedBeforeReport())
        session = self._begin_composition_tool(session, inner_call)
        try:
            read_tracking = read_tracking_for_tool_results(
                tool_results=tuple(tool_results),
                workspace=self._workspace,
            )
            tool_outcome, sequence = yield from self._execute_resolved_tool_call(
                resolved_call=_ResolvedToolCall(tool=tool, tool_call=inner_call, tool_call_id=outer_call_id),
                read_paths=read_tracking.read_paths,
                read_lines=read_tracking.read_lines,
                read_whole_files=read_tracking.read_whole_files,
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
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                error=exc.error_message,
                error_kind="tool_timeout",
                extra_details=timeout_facts,
            )
            return (session, sequence, CallReported(published_result))
        except Exception as exc:
            sequence, published_result = yield from self._dispatch_error_feedback_chunks(
                session=session,
                tool_name=inner_name,
                tool_call_id=outer_call_id,
                arguments=dict(inner_call.arguments),
                error=str(exc),
                error_kind="tool_error",
            )
            return (session, sequence, CallReported(published_result))
        tool_result = cap_tool_result_output(
            tool_result,
            session_id=session.session.id,
            tool_call_id=outer_call_id,
        )
        tool_result = self._number_yield_progress(session=session, tool_result=tool_result)
        drained_chunks, session, _ = self._drain_runtime_events(session=session, start_sequence=sequence + 1)
        yield from drained_chunks
        report = report_call(
            replace(inner_call, tool_call_id=outer_call_id),
            tool_result,
            final_tool_name=inner_name,
            final_arguments=inner_call.arguments,
        )
        envelope = self._persist_fact(session=session, fact=ToolCompletedFact(report))
        sequence = envelope.sequence
        session = self._fresh_composition_session(session)
        session = clear_tool_execution_intent(self._sessions, self._workspace, session)
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        if _is_abort_signal_requested(abort_signal):
            failed_chunk, _ = self._persist_chunk(
                chunk_builders.failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload=chunk_builders.user_interrupted_payload(
                        run_id=run_id_from_session_metadata(session.metadata), reason=_abort_signal_reason(abort_signal)
                    ),
                    status="interrupted",
                )
            )
            yield failed_chunk
            return (session, sequence, CallStoppedAfterReport(report))
        if isinstance(tool_result, ToolSuccess):
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
            sequence = yield from self._persist_chunks(post_hook_outcome.chunks, fallback_sequence=post_hook_outcome.last_sequence)
            if post_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=session, sequence=sequence, surface="post_tool", error=post_hook_outcome.failed_error, hooks=self._config.hooks
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
                return (
                    session,
                    sequence,
                    CallStoppedAfterReport(report),
                )
            self._note_hook_guidance(post_hook_outcome.guidance)
        if (
            inner_name == "todo"
            and isinstance(tool_result, ToolSuccess)
            and isinstance(tool_result.body, TodoResultBody)
            and tool_result.body.mutated
        ):
            session, todo_payload = session_with_todo_state(
                session,
                raw_phases=tool_result.body.phases,
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
        return (session, sequence, CallReported(report))

    def _permission_denied_tool_feedback_chunks(
        self, *, session: SessionState, tool_call: ToolCall, pending: PendingApproval | None, tool_call_id: str | None = None
    ) -> Generator[RuntimeStreamChunk, None, tuple[int, ReportedCall]]:
        tool_feedback_id = tool_call.tool_call_id if tool_call_id is None else tool_call_id
        if not isinstance(tool_feedback_id, str) or not tool_feedback_id.strip():
            raise ValueError("permission feedback requires its actual native call identity")
        error = f"permission denied for tool: {tool_call.tool_name}"
        denied_by = "user" if pending is not None and pending.policy_mode == "ask" else None
        result = ToolFailure(
            tool_name=tool_call.tool_name,
            error=error,
            output=TextOutput(_tool_error_content(tool_call.tool_name, error)),
            body=PermissionDenialBody(
                approval_request_id=pending.request_id if pending is not None else None,
                path_scope=pending.path_scope if pending is not None else None,
                operation_class=pending.operation_class if pending is not None else None,
                canonical_path=pending.canonical_path if pending is not None else None,
                matched_rule=pending.matched_rule if pending is not None else None,
                policy_surface=pending.policy_surface if pending is not None else None,
                denied_by=denied_by,
            ),
            diagnostics=ToolDiagnostics(
                kind="permission_denied",
                summary=_tool_error_summary(error),
                details=_tool_error_details(
                    tool_name=tool_call.tool_name, extra={"permission_denied": True, **({"denied_by": denied_by} if denied_by is not None else {})}
                ),
                guidance="Adjust the request or approval settings, then retry.",
            ),
        )
        call = replace(tool_call, tool_call_id=tool_feedback_id)
        report = report_call(call, result, final_tool_name=tool_call.tool_name, final_arguments=tool_call.arguments)
        envelope = self._persist_fact(session=session, fact=ToolCompletedFact(report))
        yield RuntimeStreamChunk(kind="event", session=session, event=envelope)
        return envelope.sequence, report

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


class RuntimeHost:
    """Project core turns through runtime-owned governance and durable state."""

    def __init__(
        self,
        coordinator: RuntimeRunLoopCoordinator,
        *,
        producer: TurnProducer,
        tool_registry: ToolRegistry,
        session: SessionState,
        sequence: int,
        turn_request: TurnRequest,
        tool_results: list[ReportedCall],
        permission_policy: PermissionPolicy | None,
        preserved_continuity_state: ContextProjection | None,
        continuation: RuntimeContinuation | None,
    ) -> None:
        self.coordinator = coordinator
        self.runtime = coordinator._surface
        self.producer = producer
        self.tool_registry = tool_registry
        self.session = session
        self.sequence = sequence
        self.active_permission_policy = permission_policy or coordinator._permission_policy
        self.continuity_to_reinject = preserved_continuity_state
        self.provider_attempt = provider_attempt_from_metadata(turn_request.metadata)
        self.provider_retry_attempt = provider_retry_attempt_from_metadata(turn_request.metadata)
        self.reasoning_capture_state = ReasoningCaptureState()
        self.active_turn_request = turn_request
        self.attempt_stream_visibility = _AttemptStreamVisibility()
        self.first_iteration = True
        self.tool_batch_repetition = ToolBatchRepetition()
        self.observed_batch_count: int | None = None
        self.pending_reminder_segment: ContextSegment | None = None
        self.context_limit_recovery = _ContextLimitRecoveryState()
        self.checkpoint_tool_result_count = len(tool_results)
        self.config = self.runtime.run_config_from_metadata(session.metadata)
        self.context_window = turn_request.context_window
        self.current_chunk_session = session
        self.continuation = continuation
        self.state: EngineState | None = None
        self.current_batch: TurnBatch | None = None
        self.batch_snapshot: CallSeed | None = None
        self.batch_started_sequence = continuation.started_sequence if continuation is not None else sequence
        self._restored_pending_turn = False
        self._terminal_phase_attempts: set[str] = set()
        coordinator._pending_hook_guidance = []

    def _stage_batch(self, batch: TurnBatch) -> None:
        if self.current_batch is not batch:
            if self.current_batch is not None or self.continuation is None:
                self.batch_started_sequence = self.sequence
            self.current_batch = batch
        self.batch_snapshot = CallSeed(
            batch.calls,
            reasoning=batch.reasoning,
            run_step=batch.run_step,
            tool_batch_id=batch.tool_batch_id,
        )

    def _save_batch(self, batch: TurnBatch) -> None:
        self.session = self.coordinator._fresh_composition_session(self.session)
        self._stage_batch(batch)
        if self.batch_snapshot is None or self.batch_snapshot.tool_batch_id is None:
            raise ValueError("native batch checkpoint requires its persisted model-turn identity")
        self.session = replace(
            self.session,
            metadata=session_metadata_with_runtime_state_updates(
                self.session.metadata,
                updates={
                    "turn_batch": persisted_turn_batch(
                        self.batch_snapshot,
                        session_id=self.session.session.id,
                        run_id=self.coordinator._composition_turn_context(self.session).run_id,
                        started_sequence=self.batch_started_sequence,
                        completed_call_ids=tuple(result.tool_call_id for result in batch.results),
                    )
                },
            ),
        )
        self.coordinator._sessions.update_session_metadata(
            workspace=self.coordinator._workspace,
            session_id=self.session.session.id,
            metadata=self.session.metadata,
        )

    def _observe_tool_batch_repetitions(
        self,
        state: EngineState,
    ) -> Generator[RuntimeStreamChunk, None, bool]:
        if self.observed_batch_count is None:
            self.observed_batch_count = len(state.batches)
        while self.observed_batch_count < len(state.batches):
            batch = state.batches[self.observed_batch_count]
            if len(batch.results) != len(batch.calls):
                break
            self.observed_batch_count += 1
            repeat_count = self.tool_batch_repetition.observe(batch)
            if repeat_count is None:
                continue
            payload: dict[str, object] = {
                "repeat_count": repeat_count,
                "tool_names": list(self.tool_batch_repetition.tool_names(batch)),
                "batch_size": len(batch.results),
            }
            event = self.coordinator._persist_event(
                session_id=self.session.session.id,
                event_type=RUNTIME_TOOL_BATCH_REPEATED,
                source="runtime",
                payload=payload,
            )
            self.sequence = event.sequence
            yield RuntimeStreamChunk(kind="event", session=self.session, event=event)

            hook_run = self.coordinator._run_turn_hook_phase(
                session=self.session,
                sequence=self.sequence,
                surface="tool_batch_repeated",
                payload=payload,
                cancel_message="run cancelled by tool-batch-repeated hook",
            )
            while True:
                try:
                    chunk = next(hook_run)
                except StopIteration as outcome:
                    self.sequence, terminated, guidance = outcome.value
                    break
                yield chunk
            if terminated:
                return True
            self.coordinator._note_hook_guidance(guidance)
        return False

    def prepare(self, state: EngineState) -> Generator[RuntimeStreamChunk, None, TurnRequest | None]:
        self.state = state
        state.results[:] = [
            _hydrate_reported_call_media(report, session_id=self.session.session.id, runtime=self.runtime) for report in state.results
        ]
        self.session = self.coordinator._fresh_composition_session(self.session)
        self.active_turn_request = state.request
        if self.continuation is not None and not self._restored_pending_turn:
            context = self.coordinator._composition_turn_context(self.session)
            self.runtime.run_composition_phase(
                "before_turn",
                self.active_turn_request,
                scope="turn",
                scope_id=context.scope_id,
                session_id=self.session.session.id,
                context=context,
                restore_only=True,
            )
            self.session = self.coordinator._fresh_composition_session(self.session)
            raw_turn = runtime_state_value(self.session.metadata, "composition_turn")
            assert isinstance(raw_turn, dict)
            invoked_ids = cast(list[str], raw_turn["invoked_call_ids"])
            for report in self.continuation.batch.completed_reports:
                if report.tool_call_id not in invoked_ids:
                    continue
                restored_call = ToolCall(tool_name=report.final_tool_name, tool_call_id=report.tool_call_id, arguments=report.final_arguments)
                self.session = self.coordinator._tool_composition_phase("before_tool", restored_call, self.session, restore_only=True)
                self.session = self.coordinator._tool_composition_phase("after_tool", report, self.session, restore_only=True)
            raw_turn = runtime_state_value(self.session.metadata, "composition_turn")
            if isinstance(raw_turn, dict) and raw_turn.get("completed") is True:
                self.runtime.run_composition_phase(
                    "after_turn",
                    ToolTurn(calls=self.continuation.batch.calls, reasoning=self.continuation.batch.reasoning),
                    scope="turn",
                    scope_id=context.scope_id,
                    session_id=self.session.session.id,
                    context=context,
                    restore_only=True,
                )
            self._restored_pending_turn = True
            self.session = self.coordinator._fresh_composition_session(self.session)
        if state.batches:
            self._save_batch(state.batches[-1])
            if not state.pending:
                batch = state.batches[-1]
                self.session = self.coordinator._complete_composition_turn(
                    self.session, ToolTurn(calls=batch.calls, reasoning=batch.reasoning, provider_usage=batch.provider_usage)
                )
        repeated_terminated = yield from self._observe_tool_batch_repetitions(state)
        if repeated_terminated:
            return None
        new_tool_results = len(state.results) > self.checkpoint_tool_result_count
        self.checkpoint_tool_result_count = self.coordinator._capture_iteration_checkpoint(
            at_safe_boundary=state.at_safe_boundary,
            session=self.session,
            turn_request=state.request,
            tool_results=state.results,
            sequence=self.sequence,
            checkpoint_tool_result_count=self.checkpoint_tool_result_count,
        )
        if state.results and _is_terminal_yield_result(state.results[-1]):
            self._terminal_composition_phases(state.results[-1])
            self.sequence = yield from self.coordinator._yield_terminal(
                session=self.session,
                tool_results=state.results,
                sequence=self.sequence,
            )
            return None
        if self.provider_attempt and new_tool_results:
            reset = _provider_attempt_reset_after_tool_result(
                provider_attempt=self.provider_attempt,
                selection=select_turn_producer_for_config(config=self.config, provider_attempt=0),
                turn_request=state.request,
                session=self.session,
            )
            if reset is not None:
                self.provider_attempt, self.producer, self.session = reset.provider_attempt, reset.producer, reset.session
                self.active_turn_request = replace(reset.turn_request, run_step=state.request.run_step, prompt=state.request.prompt)
        self.sequence, terminated, guidance = yield from self.coordinator._run_turn_hooks(
            session=self.session,
            sequence=self.sequence,
            tool_results=state.results,
            turn_index=state.request.run_step,
            provider_attempt=self.provider_attempt,
            provider_retry_attempt=self.provider_retry_attempt,
        )
        if terminated:
            return None
        if state.pending:
            return turn_request_for_session(self.active_turn_request, self.session)
        provider_results = tuple(state.results)
        self.sequence, before_compact = yield from self.coordinator._run_before_compact_hook_phase(
            session=self.session,
            sequence=self.sequence,
            tool_results=provider_results,
        )
        summary: str | None = None
        summary_kind: ContinuitySummaryKind | None = None
        if (before_compact is None or not before_compact.cancel) and (
            self.active_turn_request.context_window is not None
            and cast(RuntimeContextWindow, self.active_turn_request.context_window).summary_enabled
        ):
            summary = self.runtime.summarize_continuity(tool_results=provider_results, session_metadata=self.session.metadata)
            summary_kind = "model" if summary is not None else "fallback"
        self.context_window, self.first_iteration = self.coordinator._resolve_turn_context_window(
            active_turn_request=self.active_turn_request,
            tool_results=provider_results,
            session=self.session,
            continuity_to_reinject=self.continuity_to_reinject,
            first_iteration=self.first_iteration,
            before_compact=before_compact,
        )
        self.config = self.runtime.run_config_from_metadata(self.session.metadata)
        self.session, self.sequence, nudge = yield from self.coordinator._todo_mid_run_nudge_step(
            session=self.session,
            sequence=self.sequence,
            tool_results=provider_results,
            active_turn_request=self.active_turn_request,
            tool_registry=self.tool_registry,
            config=self.config,
        )
        reminder_segment = self.pending_reminder_segment or nudge
        self.session, assembled, self.context_window = yield from self.coordinator._assemble_turn_context(
            active_turn_request=self.active_turn_request,
            context_window=self.context_window,
            session=self.session,
            hook_guidance=guidance,
            reminder_segment=reminder_segment,
            before_compact=before_compact,
            continuity_summary_override=summary,
            continuity_summary_kind=summary_kind,
        )
        self.pending_reminder_segment = None
        context = cast(RuntimeAssembledContext, assembled)
        timeline = state.transcript_segments(context.tool_results)
        core_members = {
            (segment.tool_batch_id, segment.tool_call_id)
            for segment in timeline
            if segment.tool_batch_id is not None and segment.tool_call_id is not None
        }
        core_batch_ids = {batch_id for batch_id, _call_id in core_members}
        replayed_batch_ids = {
            segment.tool_batch_id
            for segment in context.segments
            if segment.tool_batch_id is not None
            and segment.role in {"assistant", "tool"}
            and segment.tool_call_id is not None
            and (segment.metadata or {}).get("source") == "replayed_conversation"
        }
        duplicate_replayed_batch_ids = replayed_batch_ids & core_batch_ids
        retained = {
            (segment.role, segment.tool_batch_id, segment.tool_call_id): segment
            for segment in context.segments
            if (segment.metadata or {}).get("source") == "retained_tool_result"
        }
        projected = tuple(
            replace(
                segment,
                metadata={
                    **(retained.get((segment.role, segment.tool_batch_id, segment.tool_call_id), segment).metadata or {}),
                    **(segment.metadata or {}),
                },
            )
            if segment.tool_call_id is not None
            else segment
            for segment in timeline
        )
        prior_results = tuple(
            segment for (role, batch_id, call_id), segment in retained.items() if role == "tool" and (batch_id, call_id) not in core_members
        )
        segments: list[ContextSegment] = []
        for segment in context.segments:
            metadata = segment.metadata or {}
            source = metadata.get("source")
            replayed_current_prompt = source == "replayed_conversation" and metadata.get("kind") == "current_user_prompt"
            if source == "replayed_conversation" and segment.tool_batch_id in duplicate_replayed_batch_ids:
                continue
            if source == "current_user_prompt" or replayed_current_prompt:
                prompt_segment = projected[0]
                if replayed_current_prompt:
                    prompt_segment = replace(prompt_segment, content=segment.content)
                segments.append(replace(prompt_segment, metadata=segment.metadata))
                segments.extend(prior_results)
            elif source != "retained_tool_result" and segment is not reminder_segment:
                segments.append(segment)
        segments.extend(projected[1:])
        if reminder_segment is not None:
            segments.append(reminder_segment)
        context = replace(context, segments=tuple(segments))
        self.active_turn_request = turn_request_for_session(
            replace(
                self.active_turn_request,
                prompt=state.request.prompt,
                assembled_context=context,
                context_window=self.context_window,
                tool_call_preview=self.coordinator._tool_call_preview,
                run_step=state.request.run_step,
            ),
            self.session,
        )
        self.session, self.sequence, terminated = yield from self.coordinator._emit_turn_context_events(
            session=self.session,
            sequence=self.sequence,
            active_turn_request=self.active_turn_request,
            config=self.config,
            context_window=self.context_window,
            continuity_to_reinject=self.continuity_to_reinject,
        )
        self.continuity_to_reinject = None
        return None if terminated else self.active_turn_request

    def invoke(self, producer: TurnProducer, state: EngineState) -> Generator[RuntimeStreamChunk, None, TurnPlan | None]:
        del producer
        while True:
            self.session = self.coordinator._fresh_composition_session(self.session)
            occurrence = runtime_state_value(self.session.metadata, "composition_turn_occurrence")
            if occurrence is None:
                occurrence = 0
            if type(occurrence) is not int or occurrence < 0:
                raise ValueError("composition turn cursor must be a nonnegative integer")
            occurrence += 1
            run_id = self.active_turn_request.run_id
            if not isinstance(run_id, str) or not run_id.strip():
                raise ValueError("producer turn requires its actual run identity")
            scope_id = f"{self.session.session.id}:{run_id}:{occurrence}"
            context = PhaseContext(self.coordinator._workspace, self.session.session.id, run_id, "turn", scope_id)
            turn_payload = {
                "session_id": context.session_id,
                "run_id": run_id,
                "scope_id": context.scope_id,
                "occurrence": occurrence,
                "completed": False,
                "invoked_call_ids": [],
            }
            self.session = replace(
                self.session,
                metadata=session_metadata_with_runtime_state_updates(
                    self.session.metadata,
                    updates={
                        "composition_turn_occurrence": occurrence,
                        "composition_turn": turn_payload,
                    },
                ),
            )
            self.coordinator._sessions.update_session_metadata(
                workspace=self.coordinator._workspace, session_id=self.session.session.id, metadata=self.session.metadata
            )
            self.runtime.run_composition_phase(
                "before_turn", self.active_turn_request, scope="turn", scope_id=context.scope_id, session_id=context.session_id, context=context
            )
            self.session = self.coordinator._fresh_composition_session(self.session)
            try:
                plan, self.sequence, reasoning = yield from self.coordinator._invoke_provider_step(
                    active_turn_request=self.active_turn_request,
                    tool_results=tuple(state.results),
                    session=self.session,
                    sequence=self.sequence,
                    reasoning_capture_state=self.reasoning_capture_state,
                    producer=self.producer,
                    attempt_stream_visibility=self.attempt_stream_visibility,
                )
                if plan is None:
                    return None
                self.provider_retry_attempt = 0
                self.sequence = yield from self.coordinator._persist_turn_reasoning(
                    session=self.session,
                    sequence=self.sequence,
                    streamed_reasoning_texts=reasoning,
                )
                state.request = self.active_turn_request
                return plan
            except Exception as exc:
                verdict = yield from self.coordinator._apply_provider_error_policy(
                    exc=exc,
                    session=self.session,
                    tool_results=tuple(state.results),
                    context_limit_recovery=self.context_limit_recovery,
                    sequence=self.sequence,
                    active_turn_request=self.active_turn_request,
                    context_window=cast(RuntimeContextWindow, self.context_window),
                    config=self.config,
                    provider_attempt=self.provider_attempt,
                    provider_retry_attempt=self.provider_retry_attempt,
                    current_metadata=self.active_turn_request.metadata,
                    current_prompt=self.active_turn_request.prompt,
                    current_available_tools=self.active_turn_request.available_tools,
                    current_abort_signal=self.active_turn_request.abort_signal,
                    producer=self.producer,
                    attempt_stream_visibility=self.attempt_stream_visibility,
                )
                if verdict["action"] == "exit":
                    return None
                if verdict["action"] == "reraise":
                    raise verdict["exc"] from None
                self.provider_attempt = verdict["provider_attempt"]
                self.provider_retry_attempt = verdict["provider_retry_attempt"]
                self.producer = verdict["producer"]
                self.session = verdict["session"]
                state.request = verdict["turn_request"]
                prepared = yield from self.prepare(state)
                if prepared is None:
                    return None
                self.active_turn_request = prepared
                state.request = prepared

    def observe(self, plan: TurnPlan, state: EngineState, batch: TurnBatch | None) -> Generator[RuntimeStreamChunk, None, TurnObservation]:
        is_new_batch = isinstance(plan, ToolTurn) and batch is not None and all(batch is not item for item in state.batches)
        if isinstance(plan, ToolTurn):
            if batch is None:
                raise ValueError("tool-turn observation requires its normalized candidate batch")
            if is_new_batch:
                self.session = self.coordinator._fresh_composition_session(self.session)
                self._stage_batch(batch)
                model_turn_facts = tuple(fact for fact in plan.facts if isinstance(fact, ModelTurnFact))
                if len(model_turn_facts) != 1 or model_turn_facts[0].run_step != batch.run_step:
                    raise ValueError("tool batch requires its authoritative model-turn fact")
            else:
                if batch.tool_batch_id is None:
                    raise ValueError("pending native batch has no persisted model-turn identity")
                self._save_batch(batch)
            facts: list[TurnFact] = []
            for fact in plan.facts:
                if isinstance(fact, ModelTurnFact):
                    if not is_new_batch:
                        raise ValueError("pending tool execution cannot persist a second model-turn fact")
                    fact = replace(
                        fact,
                        tool_batch_calls=batch.calls,
                        tool_batch_reasoning=batch.reasoning,
                    )
                facts.append(fact)
            if tuple(facts) != plan.facts:
                plan = replace(plan, facts=tuple(facts))
        else:
            batch = None
            self.tool_batch_repetition.reset()
            self.session = self.coordinator._complete_composition_turn(self.session, plan)
        _, self.session, self.current_chunk_session, self.provider_attempt, terminated = yield from self.coordinator._finalize_step_state(
            session=self.session,
            sequence=self.sequence,
            active_turn_request=state.request,
            turn_plan=plan,
            provider_attempt=self.provider_attempt,
            tool_results=state.results,
            complete=False,
        )
        if terminated:
            return TurnObservation(False)
        self.sequence, model_turn_event = yield from self.coordinator._persist_step_events(
            session=self.session,
            sequence=self.sequence,
            turn_plan=plan,
            current_chunk_session=self.current_chunk_session,
        )
        if is_new_batch:
            assert batch is not None
            if model_turn_event is None or model_turn_event.payload.get("run_step") != batch.run_step:
                raise ValueError("tool batch model-turn fact was not persisted at its observed run step")
            batch.tool_batch_id = model_turn_batch_id(model_turn_event.session_id, model_turn_event.sequence)
            self._save_batch(batch)
        elif isinstance(plan, ToolTurn) and model_turn_event is not None:
            raise ValueError("pending tool execution cannot create a new model-turn identity")
        return TurnObservation(True, None if batch is None else batch.tool_batch_id)

    def execute(self, call: ToolCall, state: EngineState) -> Generator[RuntimeStreamChunk, None, CallOutcome]:
        approved = self.continuation if isinstance(self.continuation, ApprovedInvocation) else None
        answered = self.continuation if isinstance(self.continuation, AnsweredQuestion) else None
        if approved is not None and (
            call.tool_call_id != approved.call.tool_call_id
            or approved.call.tool_name != approved.pending.tool_name
            or dict(approved.call.arguments) != approved.pending.arguments
        ):
            raise ValueError("approved invocation no longer matches its persisted final call identity")
        if answered is not None:
            if call.tool_call_id != answered.call.tool_call_id:
                raise ValueError("answered question does not match the original call identity")
            final_call, tool_result = answered.call, answered.result.result
            call_id = final_call.tool_call_id
            assert call_id is not None
            self.session = self.coordinator._tool_composition_phase("before_tool", final_call, self.session, restore_only=True)
            self.coordinator._composition_invocations.add(
                (self.session.session.id, self.coordinator._composition_turn_context(self.session).scope_id, call_id)
            )
        else:
            assert isinstance(state.plan, ToolTurn)
            final_call, tool, call_id, self.sequence, input_outcome = yield from self.coordinator._plan_tool_step(
                session=self.session,
                sequence=self.sequence,
                tool_registry=self.tool_registry,
                turn_plan=state.plan,
                is_resume=approved is not None or self.active_turn_request.metadata.get("resume") is True,
                approved=approved,
            )
            if input_outcome.action == "block":
                self.sequence, result = yield from self.coordinator._dispatch_error_feedback_chunks(
                    session=self.session,
                    tool_name=final_call.tool_name,
                    tool_call_id=call_id,
                    arguments=dict(final_call.arguments),
                    error=input_outcome.blocked_reason or "tool input handler blocked the call",
                    error_kind="tool_input_handler_blocked",
                )
                return CallReported(result)
            if final_call.tool_name == "invoke_tool":
                self.session, self.sequence, outcome = yield from self.coordinator._execute_invoked_tool(
                    tool_registry=self.tool_registry,
                    session=self.session,
                    sequence=self.sequence,
                    outer_call=final_call,
                    outer_call_id=call_id,
                    tool_results=state.results,
                    permission_policy=self.active_permission_policy,
                    abort_signal=state.request.abort_signal,
                    is_resume=self.active_turn_request.metadata.get("resume") is True,
                )
                return outcome
            action, self.session, self.sequence, denied = yield from self.coordinator._resolve_permission_for_tool(
                session=self.session,
                sequence=self.sequence,
                tool=tool,
                plan_tool_call=final_call,
                tool_call_id=call_id,
                approved=approved,
                active_permission_policy=self.active_permission_policy,
            )
            if action == "paused":
                return CallPaused()
            if action == "stopped":
                assert denied is not None
                return CallStoppedAfterReport(denied)
            if action == "result":
                assert denied is not None
                return CallReported(denied)
            final_call, call_id, _, self.session = self.coordinator._persist_resolved_tool_intent(
                session=self.session,
                tool=tool,
                tool_call=final_call,
                tool_call_id=call_id,
            )
            self.sequence, verdict = yield from self.coordinator._run_tool_hook_phase(
                session=self.session,
                sequence=self.sequence,
                tool_name=final_call.tool_name,
                phase="pre",
            )
            if verdict == "cancel":
                return CallStoppedBeforeReport()
            action, tool_result, self.session, self.sequence = yield from self.coordinator._execute_tool_and_recover(
                session=self.session,
                sequence=self.sequence,
                plan_tool_call=final_call,
                tool=tool,
                tool_call_id=call_id,
                tool_timeout=self.config.tool_timeout_seconds,
                tool_results=state.results,
                active_turn_request=state.request,
            )
            if action == "returned":
                return CallStoppedBeforeReport()
            assert tool_result is not None
        if approved is not None or answered is not None:
            self.coordinator._capture_interrupted_checkpoint(
                session=self.session,
                prompt=state.request.prompt,
                tool_results=state.results,
                last_event_sequence=self.sequence,
            )
        self.continuation = None
        tool_result, self.session, self.sequence = yield from self.coordinator._finalize_tool_result(
            session=self.session,
            sequence=self.sequence,
            tool_call_id=call_id,
            tool_result=tool_result,
        )
        if answered is None and (
            yield from self.coordinator._handle_question_outcome(
                session=self.session,
                plan_tool_call=final_call,
                tool_result=tool_result,
            )
        ):
            return CallPaused()
        self.sequence, self.session = yield from self.coordinator._emit_tool_completed_events(
            session=self.session,
            sequence=self.sequence,
            plan_tool_call=final_call,
            tool_call_id=call_id,
            tool_result=tool_result,
            batch=self.batch_snapshot,
        )
        report = report_call(
            final_call,
            tool_result,
            final_tool_name=final_call.tool_name,
            final_arguments=final_call.arguments,
        )
        if _is_abort_requested(state.request):
            yield from self.coordinator._emit_interrupted_failure(session=self.session, sequence=self.sequence, active_turn_request=state.request)
            return CallStoppedAfterReport(report)
        if isinstance(tool_result, ToolSuccess):
            self.sequence, verdict = yield from self.coordinator._run_tool_hook_phase(
                session=self.session,
                sequence=self.sequence,
                tool_name=final_call.tool_name,
                phase="post",
            )
            if verdict == "cancel":
                return CallStoppedAfterReport(report)
        return CallReported(report)

    def _terminal_composition_phases(self, value: object) -> None:
        run_id = self.active_turn_request.run_id
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("terminal lifecycle requires its authentic run identity")
        phases: tuple[tuple[str, LifecycleScope, str], ...] = (("after_run", "run", run_id), ("session_end", "session", self.session.session.id))
        for name, scope, scope_id in phases:
            if name in self._terminal_phase_attempts:
                continue
            self._terminal_phase_attempts.add(name)
            context = PhaseContext(self.coordinator._workspace, self.session.session.id, run_id, scope, scope_id)
            self.runtime.run_composition_phase(name, value, scope=scope, scope_id=scope_id, session_id=self.session.session.id, context=context)
            self.session = self.coordinator._fresh_composition_session(self.session)

    def finish(self, plan: FinalTurn, state: EngineState) -> Generator[RuntimeStreamChunk, None, str | None]:
        self.session, self.sequence, self.pending_reminder_segment = yield from self.coordinator._todo_reminder_step(
            session=self.session,
            sequence=self.sequence,
            tool_results=state.results,
            available_tools=state.request.available_tools,
            config=self.config,
        )
        if self.pending_reminder_segment is not None:
            return state.request.prompt
        self.session = self.coordinator._fresh_composition_session(self.session)
        self.session = replace(
            self.session,
            metadata=session_metadata_with_runtime_state_updates(
                self.session.metadata,
                removed=frozenset({"turn_batch"}),
            ),
        )
        _, self.session, self.current_chunk_session, self.provider_attempt, terminated = yield from self.coordinator._finalize_step_state(
            session=self.session,
            sequence=self.sequence,
            active_turn_request=state.request,
            turn_plan=plan,
            provider_attempt=self.provider_attempt,
            tool_results=state.results,
            record_provider_usage=False,
        )
        if not terminated:
            self._terminal_composition_phases(plan)
            self.current_chunk_session = self.coordinator._fresh_composition_session(self.current_chunk_session)
            yield from self.coordinator._emit_final_step_artifacts(
                runtime=self.runtime,
                session=self.current_chunk_session,
                turn_plan=plan,
                reasoning_capture_state=self.reasoning_capture_state,
            )
        return None

    def drain_messages(self, *, kind: Literal["steering", "followup"]) -> tuple[str, ...]:
        messages = self.runtime.drain_queued_messages(self.session.session.id, kind="follow_up" if kind == "followup" else "steering")
        if messages:
            stored = self.coordinator._sessions.load_session(workspace=self.coordinator._workspace, session_id=self.session.session.id)
            metadata = dict(self.session.metadata)
            for key in ("pending_messages", "runtime_interaction_delivery_cursor"):
                if key in stored.session.metadata:
                    metadata[key] = stored.session.metadata[key]
                else:
                    metadata.pop(key, None)
            self.session = replace(self.session, metadata=metadata)
        return messages
