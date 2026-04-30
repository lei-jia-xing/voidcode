# pyright: reportPrivateUsage=false
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ..graph.contracts import GraphRunRequest, RuntimeGraph
from ..provider.errors import (
    ProviderExecutionError,
    SingleAgentContextLimitError,
    classify_provider_error,
    format_fallback_exhausted_error,
)
from ..provider.protocol import ProviderAbortSignal
from ..tools.contracts import RuntimeTimeoutAwareTool, RuntimeToolTimeoutError, ToolCall, ToolResult
from ..tools.output import (
    cap_tool_result_output,
    sanitize_tool_arguments,
    sanitize_tool_result_data,
)
from ..tools.question import QuestionTool
from ..tools.runtime_context import RuntimeToolInvocationContext, bind_runtime_tool_context
from .context_window import (
    RuntimeContextWindow,
    RuntimeContinuityState,
    continuity_summary_metadata,
)
from .contracts import RuntimeStreamChunk
from .events import (
    RUNTIME_CONTEXT_PRESSURE,
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_SKILL_LOADED,
    RUNTIME_TODO_UPDATED,
    RUNTIME_TOOL_STARTED,
    EventEnvelope,
)
from .permission import PendingApproval, PermissionPolicy, PermissionResolution
from .question import PendingQuestion
from .session import SessionState
from .tool_display import build_tool_display, build_tool_status

if TYPE_CHECKING:
    from .service import ToolRegistry, VoidCodeRuntime

logger = logging.getLogger(__name__)


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


class RuntimeRunLoopCoordinator:
    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime

    def _started_tool_abort_chunks(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_call: ToolCall,
        tool_call_id: str,
        abort_signal: ProviderAbortSignal | None,
    ) -> tuple[RuntimeStreamChunk, RuntimeStreamChunk]:
        runtime = self._runtime
        sanitized_args = sanitize_tool_arguments(dict(tool_call.arguments))
        failed_display = build_tool_display(tool_call.tool_name, sanitized_args)
        failed_status = build_tool_status(
            tool_call.tool_name,
            tool_call_id,
            phase="failed",
            status="failed",
            display=failed_display,
        )
        completed_chunk = RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence + 1,
                event_type="runtime.tool_completed",
                source="tool",
                payload={
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
        failed_chunk = runtime._failed_chunk(
            session=session,
            sequence=sequence + 2,
            error="run interrupted",
            payload={
                "kind": "interrupted",
                "cancelled": True,
                "run_id": runtime._run_id_from_session_metadata(session.metadata),
                "reason": _abort_signal_reason(abort_signal),
            },
        )
        return completed_chunk, failed_chunk

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
        runtime = self._runtime
        permission_chunks = runtime._approval_resolution_outcome(
            session=session,
            pending=pending,
            decision=decision,
            sequence=sequence + 1,
        )
        yield from permission_chunks.chunks
        if permission_chunks.chunks:
            session = permission_chunks.chunks[-1].session
        if permission_chunks.denied:
            return

        sequence = permission_chunks.last_sequence
        try:
            tool = tool_registry.resolve(tool_call.tool_name)
        except Exception as exc:
            yield runtime._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
            raise

        pre_hook_outcome = runtime._run_tool_hooks(
            session=session,
            sequence=sequence,
            tool_name=tool_call.tool_name,
            phase="pre",
        )
        yield from pre_hook_outcome.chunks
        sequence = pre_hook_outcome.last_sequence
        if pre_hook_outcome.failed_error is not None:
            yield runtime._failed_chunk(
                session=session,
                sequence=sequence + 1,
                error=pre_hook_outcome.failed_error,
            )
            raise RuntimeError(pre_hook_outcome.failed_error)

        tool_timeout = runtime._effective_runtime_config_from_metadata(
            session.metadata
        ).tool_timeout_seconds
        explicit_tool_call_id = tool_call.tool_call_id
        tool_call_id = explicit_tool_call_id or f"runtime-tool-{uuid4().hex}"
        sequence += 1
        start_args = dict(tool_call.arguments)
        started_display = build_tool_display(tool_call.tool_name, start_args)
        started_status = build_tool_status(
            tool_call.tool_name,
            tool_call_id,
            phase="running",
            status="running",
            display=started_display,
        )
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type=RUNTIME_TOOL_STARTED,
                source="runtime",
                payload={
                    "tool": tool_call.tool_name,
                    "tool_call_id": tool_call_id,
                    "display": started_display,
                    "tool_status": started_status,
                },
            ),
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

        tool_exception_recovery_enabled = (
            runtime._effective_runtime_config_from_metadata(session.metadata).execution_engine
            == "provider"
        )
        try:
            with bind_runtime_tool_context(
                RuntimeToolInvocationContext(
                    session_id=session.session.id,
                    parent_session_id=session.session.parent_id,
                    delegation_depth=runtime._delegation_depth_from_metadata(session.metadata),
                    remaining_spawn_budget=runtime._remaining_spawn_budget_from_metadata(
                        session.metadata
                    ),
                    abort_signal=abort_signal,
                )
            ):
                if tool_timeout is None:
                    tool_result = tool.invoke(tool_call, workspace=runtime._workspace)
                elif isinstance(tool, RuntimeTimeoutAwareTool):
                    tool_result = tool.invoke_with_runtime_timeout(
                        tool_call,
                        workspace=runtime._workspace,
                        timeout_seconds=tool_timeout,
                    )
                else:
                    tool_result = tool.invoke(tool_call, workspace=runtime._workspace)
        except Exception as exc:
            drained_chunks, session, sequence = self._drain_runtime_events(
                session=session,
                start_sequence=sequence + 1,
            )
            yield from drained_chunks
            if isinstance(exc, RuntimeToolTimeoutError):
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type="runtime.tool_timeout",
                        source="runtime",
                        payload={
                            "tool": tool_call.tool_name,
                            "timeout_seconds": tool_timeout,
                        },
                    ),
                )
                timeout_sanitized_args = sanitize_tool_arguments(dict(tool_call.arguments))
                failed_display = build_tool_display(tool_call.tool_name, timeout_sanitized_args)
                failed_status = build_tool_status(
                    tool_call.tool_name,
                    tool_call_id,
                    phase="failed",
                    status="failed",
                    display=failed_display,
                )
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type="runtime.tool_completed",
                        source="tool",
                        payload={
                            "tool": tool_call.tool_name,
                            "tool_call_id": tool_call_id,
                            "arguments": timeout_sanitized_args,
                            "status": "error",
                            "error": str(exc),
                            "display": failed_display,
                            "tool_status": failed_status,
                        },
                    ),
                )
                yield runtime._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
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
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type="runtime.tool_completed",
                        source="tool",
                        payload={
                            "tool": tool_call.tool_name,
                            "tool_call_id": tool_call_id,
                            "arguments": error_sanitized_args,
                            "status": "error",
                            "error": str(exc),
                            "display": failed_display,
                            "tool_status": failed_status,
                        },
                    ),
                )
                yield runtime._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise
            tool_result = ToolResult(
                tool_name=tool_call.tool_name,
                status="error",
                error=str(exc),
                data={
                    "tool_call_id": tool_call_id,
                    "arguments": dict(tool_call.arguments),
                },
            )

        sanitized_arguments = sanitize_tool_arguments(dict(tool_call.arguments))
        tool_result = cap_tool_result_output(tool_result, workspace=runtime._workspace)
        tool_result = replace(
            tool_result,
            data=sanitize_tool_result_data(tool_result.data),
        )

        drained_chunks, session, sequence = self._drain_runtime_events(
            session=session,
            start_sequence=sequence + 1,
        )
        yield from drained_chunks

        completed_payload = {
            **tool_result.data,
            "tool_call_id": tool_call_id,
            "arguments": sanitized_arguments,
            "status": tool_result.status,
            "content": tool_result.content,
            "error": tool_result.error,
        }
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

        sequence += 1
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence,
                event_type="runtime.tool_completed",
                source="tool",
                payload=completed_payload,
            ),
        )

        if tool_result.status == "ok":
            post_hook_outcome = runtime._run_tool_hooks(
                session=session,
                sequence=sequence,
                tool_name=tool_call.tool_name,
                phase="post",
            )
            yield from post_hook_outcome.chunks
            sequence = post_hook_outcome.last_sequence
            if post_hook_outcome.failed_error is not None:
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=post_hook_outcome.failed_error,
                )
                raise RuntimeError(post_hook_outcome.failed_error)

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
        preserved_continuity_state: RuntimeContinuityState | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        runtime = self._runtime
        active_permission_policy = permission_policy or runtime._permission_policy
        continuity_to_reinject: RuntimeContinuityState | None = preserved_continuity_state
        provider_attempt = runtime._provider_attempt_from_metadata(graph_request.metadata)
        reasoning_capture_state = runtime._reasoning_capture_state()
        while True:
            current_graph_request: GraphRunRequest = graph_request
            current_session = session
            base_context = runtime._prepare_provider_context_window(
                prompt=current_graph_request.prompt,
                tool_results=tuple(tool_results),
                session_metadata=current_session.metadata,
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
                    max_tool_result_count=base_context.max_tool_result_count,
                    original_tool_result_tokens=base_context.original_tool_result_tokens,
                    retained_tool_result_tokens=base_context.retained_tool_result_tokens,
                    dropped_tool_result_tokens=base_context.dropped_tool_result_tokens,
                    token_budget=base_context.token_budget,
                    token_estimate_source=base_context.token_estimate_source,
                    reserved_output_tokens=base_context.reserved_output_tokens,
                    truncated_tool_result_count=base_context.truncated_tool_result_count,
                    continuity_state=reinjected_continuity,
                    summary_anchor=summary_anchor,
                    summary_source=summary_source,
                )
            else:
                context_window = base_context
            continuity_to_reinject = None
            session = runtime._session_with_context_window_metadata(current_session, context_window)
            skill_prompt_context = ""
            preserved_system_segments: list[str] = []
            for segment in current_graph_request.assembled_context.segments:
                if segment.role != "system" or not isinstance(segment.content, str):
                    continue
                if segment.content.startswith("Runtime-managed todo state is active"):
                    continue
                preserved_system_segments.append(segment.content)
                if segment.content.startswith("Runtime-managed skills are active for this turn."):
                    skill_prompt_context = segment.content
            graph_request = GraphRunRequest(
                session=session,
                prompt=current_graph_request.prompt,
                available_tools=current_graph_request.available_tools,
                context_window=context_window,
                assembled_context=runtime._assemble_provider_context(
                    prompt=current_graph_request.prompt,
                    tool_results=context_window.tool_results,
                    session_metadata=session.metadata,
                    skill_prompt_context=skill_prompt_context,
                    preserved_system_segments=tuple(preserved_system_segments),
                ),
                metadata=current_graph_request.metadata,
                abort_signal=current_graph_request.abort_signal,
            )
            effective_runtime_config = runtime._effective_runtime_config_from_metadata(
                session.metadata
            )
            context_window_config = effective_runtime_config.context_window
            pressure_threshold = (
                context_window_config.context_pressure_threshold
                if context_window_config is not None
                else 0.7
            )
            pressure_cooldown_steps = (
                context_window_config.context_pressure_cooldown_steps
                if context_window_config is not None
                else 3
            )
            pressure_payload = self._build_context_pressure_payload(
                session=session,
                context_window=context_window,
                threshold=pressure_threshold,
            )
            if pressure_payload is not None and self._should_emit_context_pressure(
                session=session,
                pressure_ratio=cast(float, pressure_payload["pressure_ratio"]),
                threshold=pressure_threshold,
                cooldown_steps=pressure_cooldown_steps,
                tool_result_count=context_window.original_tool_result_count,
            ):
                session = self._session_with_context_pressure_state(
                    session=session,
                    pressure_ratio=cast(float, pressure_payload["pressure_ratio"]),
                    threshold=pressure_threshold,
                    tool_result_count=context_window.original_tool_result_count,
                )
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type=RUNTIME_CONTEXT_PRESSURE,
                        source="runtime",
                        payload=pressure_payload,
                    ),
                )
                hook_outcome = runtime._run_lifecycle_hooks(
                    session=session,
                    sequence=sequence,
                    surface="context_pressure",
                    payload=pressure_payload,
                )
                yield from hook_outcome.chunks
                sequence = hook_outcome.last_sequence
                if hook_outcome.failed_error is not None:
                    logger.warning(
                        "context_pressure hook failed for %s: %s",
                        session.session.id,
                        hook_outcome.failed_error,
                    )
            if (
                context_window.compacted
                and reinjected_continuity is None
                and self._should_emit_memory_refreshed(
                    session=session,
                    summary_anchor=context_window.summary_anchor,
                    original_tool_result_count=context_window.original_tool_result_count,
                    retained_tool_result_count=context_window.retained_tool_result_count,
                )
            ):
                memory_payload = self._build_memory_refreshed_payload(context_window)
                if memory_payload is not None:
                    session = self._session_with_memory_refreshed_state(
                        session=session,
                        summary_anchor=context_window.summary_anchor,
                        original_tool_result_count=context_window.original_tool_result_count,
                        retained_tool_result_count=context_window.retained_tool_result_count,
                    )
                    sequence += 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type=RUNTIME_MEMORY_REFRESHED,
                            source="runtime",
                            payload=memory_payload,
                        ),
                    )
            tool_exception_recovery_enabled = (
                effective_runtime_config.execution_engine == "provider"
            )
            try:
                if _is_abort_requested(graph_request):
                    yield runtime._failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error="run interrupted",
                        payload={
                            "kind": "interrupted",
                            "cancelled": True,
                            "run_id": runtime._run_id_from_session_metadata(session.metadata),
                            "reason": _abort_reason(graph_request),
                        },
                    )
                    return
                graph_step = graph.step(
                    graph_request,
                    tool_results=tuple(tool_results),
                    session=session,
                )
            except Exception as exc:
                current_provider_attempt = runtime._provider_attempt_from_metadata(
                    {"provider_attempt": provider_attempt}
                )
                provider_error = exc if isinstance(exc, ProviderExecutionError) else None
                if provider_error is not None:
                    if provider_error.kind == "cancelled":
                        yield runtime._failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=str(provider_error),
                            payload={
                                "provider_error_kind": provider_error.kind,
                                "provider": provider_error.provider_name,
                                "model": provider_error.model_name,
                                "cancelled": True,
                            },
                        )
                        return
                    if (
                        provider_error.kind == "rate_limit"
                        and graph_request.metadata.get("background_rate_limit_retry") is True
                    ):
                        yield runtime._failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=str(provider_error),
                            payload={
                                "provider_error_kind": provider_error.kind,
                                "provider": provider_error.provider_name,
                                "model": provider_error.model_name,
                                "background_retry_deferred_fallback": True,
                                **(
                                    {"provider_error_details": provider_error.details}
                                    if provider_error.details is not None
                                    else {}
                                ),
                            },
                        )
                        return
                    fallback_selection = runtime._fallback_graph_selection(
                        error=provider_error,
                        session_metadata=session.metadata,
                        provider_attempt=current_provider_attempt,
                    )
                    next_attempt = current_provider_attempt + 1
                    if fallback_selection is not None:
                        next_target = fallback_selection.provider_target
                        logger.info(
                            (
                                "provider fallback for session %s: %s/%s -> %s/%s "
                                "(reason=%s, attempt=%s)"
                            ),
                            session.session.id,
                            provider_error.provider_name,
                            provider_error.model_name,
                            next_target.selection.provider,
                            next_target.selection.model,
                            provider_error.kind,
                            next_attempt,
                        )
                        sequence += 1
                        yield RuntimeStreamChunk(
                            kind="event",
                            session=session,
                            event=EventEnvelope(
                                session_id=session.session.id,
                                sequence=sequence,
                                event_type="runtime.provider_fallback",
                                source="runtime",
                                payload={
                                    "reason": provider_error.kind,
                                    "from_provider": provider_error.provider_name,
                                    "from_model": provider_error.model_name,
                                    "to_provider": next_target.selection.provider,
                                    "to_model": next_target.selection.model,
                                    "attempt": next_attempt,
                                    **(
                                        {"provider_error_details": provider_error.details}
                                        if provider_error.details is not None
                                        else {}
                                    ),
                                },
                            ),
                        )
                        provider_attempt = fallback_selection.provider_attempt
                        session = SessionState(
                            session=session.session,
                            status=session.status,
                            turn=session.turn,
                            metadata={**session.metadata, "provider_attempt": provider_attempt},
                        )
                        graph = fallback_selection.graph
                        graph_request = GraphRunRequest(
                            session=session,
                            prompt=graph_request.prompt,
                            available_tools=graph_request.available_tools,
                            context_window=graph_request.context_window,
                            assembled_context=graph_request.assembled_context,
                            metadata={
                                **graph_request.metadata,
                                "provider_attempt": provider_attempt,
                            },
                            abort_signal=graph_request.abort_signal,
                        )
                        continue
                    if provider_error.kind in {
                        "missing_auth",
                        "rate_limit",
                        "invalid_model",
                        "transient_failure",
                        "unsupported_feature",
                        "stream_tool_feedback_shape",
                    }:
                        yield runtime._failed_chunk(
                            session=session,
                            sequence=sequence + 1,
                            error=format_fallback_exhausted_error(
                                provider_name=provider_error.provider_name,
                                model_name=provider_error.model_name,
                                attempt=next_attempt,
                            ),
                            payload={
                                "provider_error_kind": provider_error.kind,
                                "provider": provider_error.provider_name,
                                "model": provider_error.model_name,
                                "fallback_exhausted": True,
                                **(
                                    {"provider_error_details": provider_error.details}
                                    if provider_error.details is not None
                                    else {}
                                ),
                            },
                        )
                        return
                if provider_error is not None:
                    yield runtime._failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=str(provider_error),
                        payload={
                            "provider_error_kind": provider_error.kind,
                            "provider": provider_error.provider_name,
                            "model": provider_error.model_name,
                            **(
                                {"provider_error_details": provider_error.details}
                                if provider_error.details is not None
                                else {}
                            ),
                        },
                    )
                    return
                classified_error = classify_provider_error(exc)
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=str(exc),
                    payload=(
                        {"kind": "provider_context_limit"}
                        if isinstance(classified_error, SingleAgentContextLimitError)
                        else None
                    ),
                )
                if isinstance(classified_error, SingleAgentContextLimitError):
                    return
                raise

            is_final_step = (
                getattr(graph_step, "is_finished", False)
                or getattr(graph_step, "output", None) is not None
            )
            if _is_abort_requested(graph_request):
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload={
                        "kind": "interrupted",
                        "cancelled": True,
                        "run_id": runtime._run_id_from_session_metadata(session.metadata),
                        "reason": _abort_reason(graph_request),
                    },
                )
                return
            session = runtime._session_with_provider_usage_metadata(
                session,
                getattr(graph_step, "provider_usage", None),
            )
            current_chunk_session = session
            if is_final_step:
                current_chunk_session = runtime._session_with_plan_state(
                    SessionState(
                        session=session.session,
                        status="completed",
                        turn=session.turn,
                        metadata=session.metadata,
                    ),
                    status="completed",
                )

            for event in runtime._renumber_events(
                getattr(graph_step, "events", ()),
                session_id=session.session.id,
                start_sequence=sequence + 1,
                reasoning_capture_state=reasoning_capture_state,
            ):
                sequence = event.sequence
                yield RuntimeStreamChunk(kind="event", session=current_chunk_session, event=event)

            if is_final_step:
                reasoning_diagnostic = runtime._reasoning_output_diagnostic(
                    session=current_chunk_session,
                    capture_state=reasoning_capture_state,
                )
                if reasoning_diagnostic is not None:
                    sequence += 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=current_chunk_session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.reasoning_diagnostic",
                            source="runtime",
                            payload=reasoning_diagnostic,
                        ),
                    )
                if getattr(graph_step, "output", None) is not None:
                    yield RuntimeStreamChunk(
                        kind="output",
                        session=current_chunk_session,
                        output=graph_step.output,
                    )
                break

            plan_tool_call = getattr(graph_step, "tool_call", None)
            if plan_tool_call is None:
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="graph step did not produce a tool call or output",
                )
                raise ValueError("graph step did not produce a tool call or output")

            explicit_tool_call_id = plan_tool_call.tool_call_id
            tool_call_id = explicit_tool_call_id or f"runtime-tool-{uuid4().hex}"
            sequence += 1
            tool_request_payload: dict[str, object] = {
                "tool": plan_tool_call.tool_name,
                "arguments": dict(plan_tool_call.arguments),
                **(
                    {"path": path}
                    if isinstance((path := plan_tool_call.arguments.get("path")), str)
                    else {}
                ),
            }
            if (
                explicit_tool_call_id is not None
                or runtime._effective_runtime_config_from_metadata(
                    session.metadata
                ).execution_engine
                == "provider"
            ):
                tool_request_payload["tool_call_id"] = tool_call_id
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="graph.tool_request_created",
                    source="graph",
                    payload=tool_request_payload,
                ),
            )

            delegation_policy_error = runtime._delegation_tool_policy_error(
                session=session,
                tool_name=plan_tool_call.tool_name,
            )
            if delegation_policy_error is not None:
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=delegation_policy_error,
                    payload={
                        "kind": "delegation_tool_policy_denied",
                        "tool": plan_tool_call.tool_name,
                    },
                )
                raise ValueError(delegation_policy_error)

            try:
                tool = tool_registry.resolve(plan_tool_call.tool_name)
            except Exception as exc:
                yield runtime._failed_chunk(session=session, sequence=sequence + 1, error=str(exc))
                raise

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_lookup_succeeded",
                    source="runtime",
                    payload={"tool": plan_tool_call.tool_name},
                ),
            )

            sequence += 1
            if approval_resolution is not None:
                pending, decision = approval_resolution
                if (
                    plan_tool_call.tool_name == pending.tool_name
                    and dict(plan_tool_call.arguments) == pending.arguments
                ):
                    sequence += 1
                    permission_chunks = runtime._approval_resolution_outcome(
                        session=session,
                        pending=pending,
                        decision=decision,
                        sequence=sequence,
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
                        permission_chunks = runtime._approval_resolution_outcome(
                            session=session,
                            pending=pending,
                            decision=decision,
                            sequence=sequence,
                        )
                    else:
                        permission_chunks = runtime._resolve_permission(
                            session=session,
                            tool=tool.definition,
                            tool_call=plan_tool_call,
                            sequence=sequence,
                            permission_policy=active_permission_policy,
                        )
            else:
                permission_chunks = runtime._resolve_permission(
                    session=session,
                    tool=tool.definition,
                    tool_call=plan_tool_call,
                    sequence=sequence,
                    permission_policy=active_permission_policy,
                )
            yield from permission_chunks.chunks
            if permission_chunks.chunks:
                session = permission_chunks.chunks[-1].session
            if permission_chunks.pending_approval is not None:
                return
            if permission_chunks.denied:
                return

            sequence = permission_chunks.last_sequence

            pre_hook_outcome = runtime._run_tool_hooks(
                session=session,
                sequence=sequence,
                tool_name=plan_tool_call.tool_name,
                phase="pre",
            )
            yield from pre_hook_outcome.chunks
            sequence = pre_hook_outcome.last_sequence
            if pre_hook_outcome.failed_error is not None:
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=pre_hook_outcome.failed_error,
                )
                raise RuntimeError(pre_hook_outcome.failed_error)

            tool_timeout = runtime._effective_runtime_config_from_metadata(
                session.metadata
            ).tool_timeout_seconds
            sequence += 1
            start_args = dict(plan_tool_call.arguments)
            started_display = build_tool_display(plan_tool_call.tool_name, start_args)
            started_status = build_tool_status(
                plan_tool_call.tool_name,
                tool_call_id,
                phase="running",
                status="running",
                display=started_display,
            )
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type=RUNTIME_TOOL_STARTED,
                    source="runtime",
                    payload={
                        "tool": plan_tool_call.tool_name,
                        "tool_call_id": tool_call_id,
                        "display": started_display,
                        "tool_status": started_status,
                    },
                ),
            )
            if _is_abort_requested(graph_request):
                yield from self._started_tool_abort_chunks(
                    session=session,
                    sequence=sequence,
                    tool_call=plan_tool_call,
                    tool_call_id=tool_call_id,
                    abort_signal=graph_request.abort_signal,
                )
                return
            try:
                with bind_runtime_tool_context(
                    RuntimeToolInvocationContext(
                        session_id=session.session.id,
                        parent_session_id=session.session.parent_id,
                        delegation_depth=runtime._delegation_depth_from_metadata(session.metadata),
                        remaining_spawn_budget=runtime._remaining_spawn_budget_from_metadata(
                            session.metadata
                        ),
                        abort_signal=graph_request.abort_signal,
                    )
                ):
                    if tool_timeout is None:
                        tool_result = tool.invoke(plan_tool_call, workspace=runtime._workspace)
                    elif isinstance(tool, RuntimeTimeoutAwareTool):
                        tool_result = tool.invoke_with_runtime_timeout(
                            plan_tool_call,
                            workspace=runtime._workspace,
                            timeout_seconds=tool_timeout,
                        )
                    else:
                        tool_result = tool.invoke(plan_tool_call, workspace=runtime._workspace)
            except Exception as exc:
                drained_chunks, session, sequence = self._drain_runtime_events(
                    session=session,
                    start_sequence=sequence + 1,
                )
                yield from drained_chunks
                if isinstance(exc, RuntimeToolTimeoutError):
                    sequence += 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.tool_timeout",
                            source="runtime",
                            payload={
                                "tool": plan_tool_call.tool_name,
                                "timeout_seconds": tool_timeout,
                            },
                        ),
                    )
                    timeout_sanitized_args = sanitize_tool_arguments(dict(plan_tool_call.arguments))
                    failed_display = build_tool_display(
                        plan_tool_call.tool_name, timeout_sanitized_args
                    )
                    failed_status = build_tool_status(
                        plan_tool_call.tool_name,
                        tool_call_id,
                        phase="failed",
                        status="failed",
                        display=failed_display,
                    )
                    sequence += 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.tool_completed",
                            source="tool",
                            payload={
                                "tool": plan_tool_call.tool_name,
                                "tool_call_id": tool_call_id,
                                "arguments": timeout_sanitized_args,
                                "status": "error",
                                "error": str(exc),
                                "display": failed_display,
                                "tool_status": failed_status,
                            },
                        ),
                    )
                    yield runtime._failed_chunk(
                        session=session, sequence=sequence + 1, error=str(exc)
                    )
                    return
                if not tool_exception_recovery_enabled and not _is_tool_timeout_like_exception(exc):
                    error_sanitized_args = sanitize_tool_arguments(dict(plan_tool_call.arguments))
                    failed_display = build_tool_display(
                        plan_tool_call.tool_name, error_sanitized_args
                    )
                    failed_status = build_tool_status(
                        plan_tool_call.tool_name,
                        tool_call_id,
                        phase="failed",
                        status="failed",
                        display=failed_display,
                    )
                    sequence += 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type="runtime.tool_completed",
                            source="tool",
                            payload={
                                "tool": plan_tool_call.tool_name,
                                "tool_call_id": tool_call_id,
                                "arguments": error_sanitized_args,
                                "status": "error",
                                "error": str(exc),
                                "display": failed_display,
                                "tool_status": failed_status,
                            },
                        ),
                    )
                    yield runtime._failed_chunk(
                        session=session, sequence=sequence + 1, error=str(exc)
                    )
                    raise
                tool_result = ToolResult(
                    tool_name=plan_tool_call.tool_name,
                    status="error",
                    error=str(exc),
                    data={
                        "tool_call_id": tool_call_id,
                        "arguments": dict(plan_tool_call.arguments),
                    },
                )

            runtime_tool_result_data = dict(tool_result.data)
            if _is_abort_requested(graph_request):
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload={
                        "kind": "interrupted",
                        "cancelled": True,
                        "run_id": runtime._run_id_from_session_metadata(session.metadata),
                        "reason": _abort_reason(graph_request),
                    },
                )
                return

            sanitized_arguments = sanitize_tool_arguments(dict(plan_tool_call.arguments))
            tool_result = cap_tool_result_output(tool_result, workspace=runtime._workspace)
            tool_result = replace(
                tool_result,
                data=sanitize_tool_result_data(tool_result.data),
            )

            drained_chunks, session, sequence = self._drain_runtime_events(
                session=session,
                start_sequence=sequence + 1,
            )
            yield from drained_chunks

            if (
                plan_tool_call.tool_name == QuestionTool.definition.name
                and tool_result.status == "ok"
            ):
                pending_question = PendingQuestion(
                    request_id=f"question-{uuid4().hex}",
                    tool_name=plan_tool_call.tool_name,
                    arguments=dict(plan_tool_call.arguments),
                    prompts=QuestionTool.parse_prompts(plan_tool_call.arguments),
                )
                waiting_session = runtime._session_with_plan_state(
                    SessionState(
                        session=session.session,
                        status="waiting",
                        turn=session.turn,
                        metadata=session.metadata,
                    ),
                    status="waiting_question",
                    blocked_tool=pending_question.tool_name,
                )
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=waiting_session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
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
                    ),
                )
                return

            completed_payload = {
                **tool_result.data,
                "tool_call_id": tool_call_id,
                "arguments": sanitized_arguments,
                "status": tool_result.status,
                "content": tool_result.content,
                "error": tool_result.error,
            }
            completed_payload.setdefault("tool", tool_result.tool_name)

            completed_display = build_tool_display(
                plan_tool_call.tool_name,
                sanitized_arguments,
                result_data=tool_result.data,
            )
            completed_status = build_tool_status(
                plan_tool_call.tool_name,
                tool_call_id,
                phase="completed" if tool_result.status == "ok" else "failed",
                status="completed" if tool_result.status == "ok" else "failed",
                display=completed_display,
            )
            completed_payload["display"] = completed_display
            completed_payload["tool_status"] = completed_status

            sequence += 1
            yield RuntimeStreamChunk(
                kind="event",
                session=session,
                event=EventEnvelope(
                    session_id=session.session.id,
                    sequence=sequence,
                    event_type="runtime.tool_completed",
                    source="tool",
                    payload=completed_payload,
                ),
            )

            if plan_tool_call.tool_name == "skill" and tool_result.status == "ok":
                skill_payload = completed_payload.get("skill")
                if isinstance(skill_payload, dict):
                    typed_skill_payload = cast(dict[str, object], skill_payload)
                    skill_name: object | None = typed_skill_payload.get("name")
                    skill_source_path: object | None = typed_skill_payload.get("source_path")
                    sequence += 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=sequence,
                            event_type=RUNTIME_SKILL_LOADED,
                            source="runtime",
                            payload={
                                "name": skill_name if isinstance(skill_name, str) else None,
                                "source": "tool",
                                "source_path": (
                                    skill_source_path
                                    if isinstance(skill_source_path, str)
                                    else None
                                ),
                            },
                        ),
                    )

            if plan_tool_call.tool_name == "todo_write" and tool_result.status == "ok":
                revision = sequence + 1
                session, todo_payload = runtime._session_with_todo_state(
                    session,
                    raw_todos=runtime_tool_result_data.get("todos"),
                    revision=revision,
                )
                sequence = revision
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type=RUNTIME_TODO_UPDATED,
                        source="runtime",
                        payload=todo_payload,
                    ),
                )

            if tool_result.status == "ok":
                post_hook_outcome = runtime._run_tool_hooks(
                    session=session,
                    sequence=sequence,
                    tool_name=plan_tool_call.tool_name,
                    phase="post",
                )
                yield from post_hook_outcome.chunks
                sequence = post_hook_outcome.last_sequence
                if post_hook_outcome.failed_error is not None:
                    yield runtime._failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error=post_hook_outcome.failed_error,
                    )
                    raise RuntimeError(post_hook_outcome.failed_error)

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

    @staticmethod
    def _build_context_pressure_payload(
        *,
        session: SessionState,
        context_window: RuntimeContextWindow,
        threshold: float,
    ) -> dict[str, object] | None:
        budget = context_window.token_budget
        estimated_tokens = context_window.original_tool_result_tokens
        if budget is None or estimated_tokens is None or budget <= 0 or estimated_tokens <= 0:
            return None
        pressure_ratio = estimated_tokens / budget
        payload: dict[str, object] = {
            "kind": "pressure_signal",
            "session_id": session.session.id,
            "estimated_tokens": estimated_tokens,
            "budget_max_tokens": budget,
            "pressure_ratio": pressure_ratio,
            "threshold": threshold,
            "reason": "token_budget_ratio_exceeded",
            "compacted": context_window.compacted,
            "token_estimate_source": context_window.token_estimate_source,
            "original_tool_result_count": context_window.original_tool_result_count,
            "retained_tool_result_count": context_window.retained_tool_result_count,
        }
        if context_window.summary_anchor is not None:
            payload["summary_anchor"] = context_window.summary_anchor
        if context_window.summary_source is not None:
            payload["summary_source"] = context_window.summary_source
        if context_window.continuity_state is not None:
            payload["continuity_state"] = context_window.continuity_state.metadata_payload()
        return payload

    @staticmethod
    def _build_memory_refreshed_payload(
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
            "summary_source": context_window.summary_source,
            "continuity_state": (
                context_window.continuity_state.metadata_payload()
                if context_window.continuity_state is not None
                else None
            ),
        }

    @staticmethod
    def _should_emit_memory_refreshed(
        *,
        session: SessionState,
        summary_anchor: str | None,
        original_tool_result_count: int,
        retained_tool_result_count: int,
    ) -> bool:
        raw_runtime_state = session.metadata.get("runtime_state")
        runtime_state = (
            cast(dict[str, object], raw_runtime_state)
            if isinstance(raw_runtime_state, dict)
            else {}
        )
        current_run_id_raw = runtime_state.get("run_id")
        current_run_id = current_run_id_raw if isinstance(current_run_id_raw, str) else None
        raw_memory_state = runtime_state.get("memory_refreshed")
        memory_state = (
            cast(dict[str, object], raw_memory_state) if isinstance(raw_memory_state, dict) else {}
        )
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
    def _session_with_memory_refreshed_state(
        *,
        session: SessionState,
        summary_anchor: str | None,
        original_tool_result_count: int,
        retained_tool_result_count: int,
    ) -> SessionState:
        raw_runtime_state = session.metadata.get("runtime_state")
        runtime_state = (
            dict(cast(dict[str, object], raw_runtime_state))
            if isinstance(raw_runtime_state, dict)
            else {}
        )
        runtime_state["memory_refreshed"] = {
            "last_summary_anchor": summary_anchor,
            "last_original_tool_result_count": original_tool_result_count,
            "last_retained_tool_result_count": retained_tool_result_count,
            "last_emitted_run_id": (
                runtime_state.get("run_id")
                if isinstance(runtime_state.get("run_id"), str)
                else None
            ),
        }
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={**session.metadata, "runtime_state": runtime_state},
        )

    @staticmethod
    def _should_emit_context_pressure(
        *,
        session: SessionState,
        pressure_ratio: float,
        threshold: float,
        cooldown_steps: int,
        tool_result_count: int,
    ) -> bool:
        if pressure_ratio < threshold:
            return False
        raw_runtime_state = session.metadata.get("runtime_state")
        runtime_state = (
            cast(dict[str, object], raw_runtime_state)
            if isinstance(raw_runtime_state, dict)
            else {}
        )
        current_run_id_raw = runtime_state.get("run_id")
        current_run_id = current_run_id_raw if isinstance(current_run_id_raw, str) else None
        raw_pressure_state = runtime_state.get("context_pressure")
        pressure_state = (
            cast(dict[str, object], raw_pressure_state)
            if isinstance(raw_pressure_state, dict)
            else {}
        )
        last_count_raw = pressure_state.get("last_emitted_tool_result_count")
        last_count = (
            last_count_raw
            if isinstance(last_count_raw, int) and not isinstance(last_count_raw, bool)
            else None
        )
        if last_count is None:
            return True
        last_run_id_raw = pressure_state.get("last_emitted_run_id")
        last_run_id = last_run_id_raw if isinstance(last_run_id_raw, str) else None
        if current_run_id is not None and last_run_id is not None and current_run_id != last_run_id:
            return True
        return (tool_result_count - last_count) >= cooldown_steps

    @staticmethod
    def _session_with_context_pressure_state(
        *,
        session: SessionState,
        pressure_ratio: float,
        threshold: float,
        tool_result_count: int,
    ) -> SessionState:
        raw_runtime_state = session.metadata.get("runtime_state")
        runtime_state = (
            dict(cast(dict[str, object], raw_runtime_state))
            if isinstance(raw_runtime_state, dict)
            else {}
        )
        runtime_state["context_pressure"] = {
            "last_emitted_tool_result_count": tool_result_count,
            "last_pressure_ratio": pressure_ratio,
            "threshold": threshold,
            "last_emitted_run_id": (
                runtime_state.get("run_id")
                if isinstance(runtime_state.get("run_id"), str)
                else None
            ),
        }
        return SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata={**session.metadata, "runtime_state": runtime_state},
        )

    def _drain_runtime_events(
        self,
        *,
        session: Any,
        start_sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        runtime = self._runtime
        emitted: list[RuntimeStreamChunk] = []
        sequence = start_sequence - 1
        current_session = session
        for acp_event in runtime._envelopes_for_acp_events(
            session_id=session.session.id,
            start_sequence=start_sequence,
            acp_events=runtime._acp_adapter.drain_events(),
        ):
            sequence = acp_event.sequence
            current_session = runtime._session_with_current_acp_metadata(current_session)
            emitted.append(
                RuntimeStreamChunk(kind="event", session=current_session, event=acp_event)
            )
        for mcp_event in runtime._envelopes_for_mcp_events(
            session_id=session.session.id,
            start_sequence=sequence + 1,
            mcp_events=runtime._mcp_manager.drain_events(),
        ):
            sequence = mcp_event.sequence
            emitted.append(
                RuntimeStreamChunk(kind="event", session=current_session, event=mcp_event)
            )
        for lsp_event in runtime._envelopes_for_lsp_events(
            session_id=session.session.id,
            start_sequence=sequence + 1,
            lsp_events=runtime._lsp_manager.drain_events(),
        ):
            sequence = lsp_event.sequence
            emitted.append(
                RuntimeStreamChunk(kind="event", session=current_session, event=lsp_event)
            )
        return tuple(emitted), current_session, sequence
