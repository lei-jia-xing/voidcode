# pyright: reportPrivateUsage=false
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from collections.abc import Generator, Iterator, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from ..graph.contracts import GraphRunRequest, RuntimeGraph
from ..provider.errors import (
    ProviderExecutionError,
    SingleAgentContextLimitError,
    classify_provider_error,
    format_fallback_exhausted_error,
    format_provider_retry_exhausted_error,
)
from ..provider.protocol import (
    ProviderAbortSignal,
    ProviderAssembledContext,
    ProviderContextSegmentLike,
)
from ..tools.contracts import (
    RuntimeTimeoutAwareTool,
    RuntimeToolTimeoutError,
    ToolCall,
    ToolDefinition,
    ToolErrorDetails,
    ToolResult,
)
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
from .contracts import RuntimeProviderContextPolicyDecision, RuntimeStreamChunk
from .events import (
    RUNTIME_CONTEXT_PRESSURE,
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_PROVIDER_TRANSIENT_RETRY,
    RUNTIME_QUESTION_REQUESTED,
    RUNTIME_SKILL_LOADED,
    RUNTIME_TODO_UPDATED,
    RUNTIME_TOOL_PROGRESS,
    RUNTIME_TOOL_STARTED,
    EventEnvelope,
)
from .execution_seams import RuntimeGraphSelection
from .permission import PendingApproval, PermissionPolicy, PermissionResolution
from .question import PendingQuestion
from .session import SessionState
from .tool_display import build_tool_display, build_tool_status

if TYPE_CHECKING:
    from .service import ToolRegistry, VoidCodeRuntime


logger = logging.getLogger(__name__)

_TOOL_PROGRESS_QUEUE_MAX_ITEMS = 128
_TOOL_PROGRESS_POLL_SECONDS = 0.05
_PROVIDER_TRANSIENT_RETRYABLE_KINDS = frozenset({"rate_limit", "transient_failure"})


def _provider_transient_retry_delay_ms(
    *,
    retry_attempt: int,
    base_delay_ms: float,
    max_delay_ms: float,
    jitter: bool,
) -> int:
    capped_delay = min(base_delay_ms * (2 ** max(retry_attempt - 1, 0)), max_delay_ms)
    if jitter and capped_delay > 0:
        capped_delay = random.uniform(0, capped_delay)
    return max(0, int(round(capped_delay)))


def _tool_error_content(tool_name: str, error: str) -> str:
    return f"{tool_name} failed: {error}. Please correct the tool arguments and retry."


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
class _ToolProgressItem:
    payload: dict[str, object]


@dataclass(frozen=True, slots=True)
class _ToolResultItem:
    result: ToolResult


@dataclass(frozen=True, slots=True)
class _ToolExceptionItem:
    exception: Exception


@dataclass(frozen=True, slots=True)
class _ProviderAttemptReset:
    provider_attempt: int
    graph: RuntimeGraph
    graph_request: GraphRunRequest
    session: SessionState


type _ToolQueueItem = _ToolProgressItem | _ToolResultItem | _ToolExceptionItem


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

    @staticmethod
    def _is_progress_capable_tool(tool_name: str) -> bool:
        return tool_name == "shell_exec"

    def _invoke_tool_with_progress_events(
        self,
        *,
        tool: Any,
        tool_call: ToolCall,
        workspace: Any,
        tool_timeout: int | None,
        session: SessionState,
        start_sequence: int,
        tool_call_id: str,
        abort_signal: ProviderAbortSignal | None,
        parent_session_id: str | None,
        delegation_depth: int,
        remaining_spawn_budget: int | None,
    ) -> Generator[RuntimeStreamChunk, None, tuple[ToolResult | Exception, int]]:
        progress_queue: queue.Queue[_ToolQueueItem] = queue.Queue(
            maxsize=_TOOL_PROGRESS_QUEUE_MAX_ITEMS
        )
        dropped_progress_events = 0
        dropped_lock = threading.Lock()

        def emit_tool_progress(payload: Mapping[str, object]) -> None:
            nonlocal dropped_progress_events
            with dropped_lock:
                dropped = dropped_progress_events
                dropped_progress_events = 0
            progress_payload: dict[str, object] = {
                "tool": tool_call.tool_name,
                "tool_call_id": tool_call_id,
                **dict(payload),
            }
            if dropped:
                progress_payload["dropped_progress_events"] = dropped
            try:
                progress_queue.put_nowait(_ToolProgressItem(progress_payload))
            except queue.Full:
                with dropped_lock:
                    dropped_progress_events += 1 + dropped

        def invoke_tool() -> None:
            try:
                with bind_runtime_tool_context(
                    RuntimeToolInvocationContext(
                        session_id=session.session.id,
                        parent_session_id=parent_session_id,
                        delegation_depth=delegation_depth,
                        remaining_spawn_budget=remaining_spawn_budget,
                        abort_signal=abort_signal,
                        emit_tool_progress=emit_tool_progress,
                    )
                ):
                    if tool_timeout is None:
                        result = tool.invoke(tool_call, workspace=workspace)
                    elif isinstance(tool, RuntimeTimeoutAwareTool):
                        result = tool.invoke_with_runtime_timeout(
                            tool_call,
                            workspace=workspace,
                            timeout_seconds=tool_timeout,
                        )
                    else:
                        result = tool.invoke(tool_call, workspace=workspace)
                progress_queue.put(_ToolResultItem(result))
            except Exception as exc:
                progress_queue.put(_ToolExceptionItem(exc))

        worker = threading.Thread(
            target=invoke_tool,
            name=f"runtime-tool-{tool_call.tool_name}-worker",
            daemon=True,
        )
        worker.start()

        sequence = start_sequence - 1
        terminal_item: _ToolResultItem | _ToolExceptionItem | None = None
        while terminal_item is None:
            try:
                item = progress_queue.get(timeout=_TOOL_PROGRESS_POLL_SECONDS)
            except queue.Empty:
                continue
            if isinstance(item, _ToolProgressItem):
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type=RUNTIME_TOOL_PROGRESS,
                        source="tool",
                        payload=item.payload,
                    ),
                )
                continue
            terminal_item = item

        while True:
            try:
                item = progress_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, _ToolProgressItem):
                sequence += 1
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=EventEnvelope(
                        session_id=session.session.id,
                        sequence=sequence,
                        event_type=RUNTIME_TOOL_PROGRESS,
                        source="tool",
                        payload=item.payload,
                    ),
                )

        worker.join(timeout=1)
        if isinstance(terminal_item, _ToolExceptionItem):
            return terminal_item.exception, sequence
        return terminal_item.result, sequence

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
            yield from self._permission_denied_tool_feedback_chunks(
                session=session,
                sequence=permission_chunks.last_sequence,
                tool_call=tool_call,
                pending=permission_chunks.denied_approval or pending,
                tool_results=tool_results,
            )
            return

        sequence = permission_chunks.last_sequence
        workflow_policy_error = runtime._workflow_tool_policy_error(
            session=session,
            tool_name=tool_call.tool_name,
        )
        if workflow_policy_error is not None:
            yield runtime._failed_chunk(
                session=session,
                sequence=sequence + 1,
                error=workflow_policy_error,
                payload={
                    "kind": "workflow_tool_policy_denied",
                    "tool": tool_call.tool_name,
                },
            )
            raise ValueError(workflow_policy_error)
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
            if self._is_progress_capable_tool(tool_call.tool_name):
                tool_outcome, sequence = yield from self._invoke_tool_with_progress_events(
                    tool=tool,
                    tool_call=tool_call,
                    workspace=runtime._workspace,
                    tool_timeout=tool_timeout,
                    session=session,
                    start_sequence=sequence + 1,
                    tool_call_id=tool_call_id,
                    abort_signal=abort_signal,
                    parent_session_id=session.session.parent_id,
                    delegation_depth=runtime._delegation_depth_from_metadata(session.metadata),
                    remaining_spawn_budget=runtime._remaining_spawn_budget_from_metadata(
                        session.metadata
                    ),
                )
                if isinstance(tool_outcome, Exception):
                    raise tool_outcome
                tool_result = tool_outcome
            else:
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
                partial_timeout_payload: dict[str, object] = {}
                partial_timeout_content: str | None = None
                partial_timeout_error: str | None = None
                partial_result = getattr(exc, "partial_result", None)
                if isinstance(partial_result, ToolResult):
                    capped_partial = cap_tool_result_output(
                        partial_result,
                        workspace=runtime._workspace,
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
                            "content": _tool_error_content(tool_call.tool_name, str(exc)),
                            **_tool_error_payload(
                                tool_name=tool_call.tool_name,
                                error=str(exc),
                            ),
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
                content=_tool_error_content(tool_call.tool_name, str(exc)),
                error=str(exc),
                data={
                    "tool_call_id": tool_call_id,
                    "arguments": dict(tool_call.arguments),
                },
                error_summary=_tool_error_summary(str(exc)),
                error_details=_tool_error_details(tool_name=tool_call.tool_name, error=str(exc)),
                retry_guidance=_tool_error_retry_guidance(str(exc)),
            )

        sanitized_arguments = sanitize_tool_arguments(dict(tool_call.arguments))
        tool_result = cap_tool_result_output(
            tool_result,
            workspace=runtime._workspace,
            session_id=session.session.id,
            tool_call_id=tool_call_id,
        )
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

        if _is_abort_signal_requested(abort_signal):
            yield runtime._failed_chunk(
                session=session,
                sequence=sequence + 1,
                error="run interrupted",
                payload={
                    "kind": "interrupted",
                    "cancelled": True,
                    "run_id": runtime._run_id_from_session_metadata(session.metadata),
                    "reason": _abort_signal_reason(abort_signal),
                },
            )
            return

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
        provider_retry_attempt: int = runtime._provider_retry_attempt_from_metadata(
            graph_request.metadata
        )
        reasoning_capture_state = runtime._reasoning_capture_state()
        active_graph_request: GraphRunRequest = graph_request
        pending_provider_attempt_reset: _ProviderAttemptReset | None = None
        first_iteration = True
        while True:
            if pending_provider_attempt_reset is not None:
                provider_attempt = pending_provider_attempt_reset.provider_attempt
                graph = pending_provider_attempt_reset.graph
                active_graph_request = pending_provider_attempt_reset.graph_request
                session = pending_provider_attempt_reset.session
                pending_provider_attempt_reset = None
            sequence = int(sequence)
            current_graph_request: Any = active_graph_request
            current_prompt: str = cast(str, current_graph_request.prompt)
            current_available_tools: tuple[ToolDefinition, ...] = cast(
                tuple[ToolDefinition, ...], current_graph_request.available_tools
            )
            current_assembled_context: ProviderAssembledContext = cast(
                ProviderAssembledContext, current_graph_request.assembled_context
            )
            current_segments: tuple[ProviderContextSegmentLike, ...] = (
                current_assembled_context.segments
            )
            current_metadata: dict[str, object] = current_graph_request.metadata
            current_abort_signal: ProviderAbortSignal | None = current_graph_request.abort_signal
            current_session: SessionState = session
            current_session_metadata: dict[str, object] = current_session.metadata
            if first_iteration:
                prebuilt_context = cast(RuntimeContextWindow, current_graph_request.context_window)
                first_iteration = False
                if prebuilt_context.original_tool_result_count == len(tool_results):
                    base_context = prebuilt_context
                else:
                    base_context = runtime._prepare_provider_context_window(
                        prompt=current_prompt,
                        tool_results=tuple(tool_results),
                        session_metadata=current_session_metadata,
                        abort_signal=current_abort_signal,
                    )
            else:
                base_context = runtime._prepare_provider_context_window(
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
                    max_tool_result_count=base_context.max_tool_result_count,
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
            continuity_to_reinject = None
            session = runtime._session_with_context_window_metadata(current_session, context_window)
            skill_prompt_context = ""
            preserved_system_segments: list[str] = []
            for segment in current_segments:
                if segment.role != "system" or not isinstance(segment.content, str):
                    continue
                if segment.content.startswith("Runtime-managed todo state is active"):
                    continue
                preserved_system_segments.append(segment.content)
                if segment.content.startswith("Runtime-managed skills are active for this turn."):
                    skill_prompt_context = segment.content
            assembled_context = runtime._assemble_provider_context(
                prompt=current_prompt,
                tool_results=context_window.tool_results,
                session_metadata=session.metadata,
                skill_prompt_context=skill_prompt_context,
                preserved_system_segments=tuple(preserved_system_segments),
            )
            session = runtime._session_with_context_window_payload_metadata(
                session,
                assembled_context.metadata,
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
            effective_runtime_config = runtime._effective_runtime_config_from_metadata(
                session.metadata
            )
            provider_context_policy_decision: RuntimeProviderContextPolicyDecision | None = (
                runtime._provider_context_policy_decision_for_graph_request(
                    graph_request=active_graph_request,
                    effective_config=effective_runtime_config,
                )
            )
            if provider_context_policy_decision is not None:
                if provider_context_policy_decision.action == "warn":
                    policy_event_sequence: int = sequence + 1
                    yield RuntimeStreamChunk(
                        kind="event",
                        session=session,
                        event=EventEnvelope(
                            session_id=session.session.id,
                            sequence=policy_event_sequence,
                            event_type="runtime.provider_context_policy",
                            source="runtime",
                            payload={
                                "mode": provider_context_policy_decision.mode,
                                "action": provider_context_policy_decision.action,
                                "blocked": provider_context_policy_decision.blocked,
                                "diagnostic_count": (
                                    provider_context_policy_decision.diagnostic_count
                                ),
                                "diagnostic_codes": list(
                                    provider_context_policy_decision.diagnostic_codes
                                ),
                                "blocking_diagnostic_codes": list(
                                    provider_context_policy_decision.blocking_diagnostic_codes
                                ),
                                "message": provider_context_policy_decision.message,
                            },
                        ),
                    )
                    sequence = policy_event_sequence
                if provider_context_policy_decision.blocked:
                    policy_failure_sequence: int = sequence + 1
                    yield runtime._failed_chunk(
                        session=session,
                        sequence=policy_failure_sequence,
                        error=provider_context_policy_decision.message,
                        payload={
                            "kind": "provider_context_policy_blocked",
                            "provider_context_policy": {
                                "mode": provider_context_policy_decision.mode,
                                "action": provider_context_policy_decision.action,
                                "blocked": provider_context_policy_decision.blocked,
                                "diagnostic_count": (
                                    provider_context_policy_decision.diagnostic_count
                                ),
                                "diagnostic_codes": list(
                                    provider_context_policy_decision.diagnostic_codes
                                ),
                                "blocking_diagnostic_codes": list(
                                    provider_context_policy_decision.blocking_diagnostic_codes
                                ),
                            },
                        },
                    )
                    return
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
                include_provider_usage=False,
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
                if _is_abort_requested(active_graph_request):
                    yield runtime._failed_chunk(
                        session=session,
                        sequence=sequence + 1,
                        error="run interrupted",
                        payload={
                            "kind": "interrupted",
                            "cancelled": True,
                            "run_id": runtime._run_id_from_session_metadata(session.metadata),
                            "reason": _abort_reason(active_graph_request),
                        },
                    )
                    return
                graph_step = graph.step(
                    active_graph_request,
                    tool_results=tuple(tool_results),
                    session=session,
                )
                provider_retry_attempt = 0
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
                        and active_graph_request.metadata.get("background_rate_limit_retry") is True
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
                    transient_retry_config = runtime._provider_transient_retry_config(
                        provider_name=provider_error.provider_name,
                        session_metadata=session.metadata,
                    )
                    current_provider_retry_attempt: int = int(provider_retry_attempt)
                    if (
                        provider_error.kind in _PROVIDER_TRANSIENT_RETRYABLE_KINDS
                        and current_provider_retry_attempt < transient_retry_config.max_retries
                    ):
                        retry_attempt: int = current_provider_retry_attempt + 1
                        delay_ms = _provider_transient_retry_delay_ms(
                            retry_attempt=retry_attempt,
                            base_delay_ms=transient_retry_config.base_delay_ms,
                            max_delay_ms=transient_retry_config.max_delay_ms,
                            jitter=transient_retry_config.jitter,
                        )
                        logger.info(
                            (
                                "provider transient retry for session %s: %s/%s "
                                "(reason=%s, retry_attempt=%s, max_retries=%s, delay_ms=%s)"
                            ),
                            session.session.id,
                            provider_error.provider_name,
                            provider_error.model_name,
                            provider_error.kind,
                            retry_attempt,
                            transient_retry_config.max_retries,
                            delay_ms,
                        )
                        sequence += 1
                        yield RuntimeStreamChunk(
                            kind="event",
                            session=session,
                            event=EventEnvelope(
                                session_id=session.session.id,
                                sequence=sequence,
                                event_type=RUNTIME_PROVIDER_TRANSIENT_RETRY,
                                source="runtime",
                                payload={
                                    "reason": provider_error.kind,
                                    "provider": provider_error.provider_name,
                                    "model": provider_error.model_name,
                                    "retry_attempt": retry_attempt,
                                    "max_retries": transient_retry_config.max_retries,
                                    "delay_ms": delay_ms,
                                    **(
                                        {"provider_error_details": provider_error.details}
                                        if provider_error.details is not None
                                        else {}
                                    ),
                                },
                            ),
                        )
                        if delay_ms > 0:
                            time.sleep(delay_ms / 1000.0)
                        provider_retry_attempt = int(retry_attempt)
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
                        continue
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
                        provider_retry_attempt = 0
                        fallback_prompt: str = current_prompt
                        fallback_available_tools: tuple[ToolDefinition, ...] = (
                            current_available_tools
                        )
                        fallback_context_window = context_window
                        fallback_assembled_context: ProviderAssembledContext = (
                            active_graph_request.assembled_context
                        )
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
                            error=(
                                format_provider_retry_exhausted_error(
                                    provider_name=provider_error.provider_name,
                                    model_name=provider_error.model_name,
                                    retry_attempts=provider_retry_attempt,
                                )
                                if provider_error.kind in _PROVIDER_TRANSIENT_RETRYABLE_KINDS
                                else format_fallback_exhausted_error(
                                    provider_name=provider_error.provider_name,
                                    model_name=provider_error.model_name,
                                    attempt=next_attempt,
                                )
                            ),
                            payload={
                                "provider_error_kind": provider_error.kind,
                                "provider": provider_error.provider_name,
                                "model": provider_error.model_name,
                                "fallback_exhausted": True,
                                **(
                                    {
                                        "provider_retry_exhausted": True,
                                        "provider_retry_attempts": provider_retry_attempt,
                                    }
                                    if provider_error.kind in _PROVIDER_TRANSIENT_RETRYABLE_KINDS
                                    else {}
                                ),
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
            if _is_abort_requested(active_graph_request):
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload={
                        "kind": "interrupted",
                        "cancelled": True,
                        "run_id": runtime._run_id_from_session_metadata(session.metadata),
                        "reason": _abort_reason(active_graph_request),
                    },
                )
                return
            session = runtime._session_with_provider_usage_metadata(
                session,
                getattr(graph_step, "provider_usage", None),
            )
            if runtime._provider_retry_attempt_from_metadata(session.metadata) != 0:
                session = SessionState(
                    session=session.session,
                    status=session.status,
                    turn=session.turn,
                    metadata={**session.metadata, "provider_retry_attempt": 0},
                )
            pressure_payload = None
            if getattr(graph_step, "provider_usage", None) is not None:
                pressure_payload = self._build_context_pressure_payload(
                    session=session,
                    context_window=context_window,
                    threshold=pressure_threshold,
                    include_provider_usage=True,
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
            if is_final_step and provider_attempt != 0:
                provider_attempt = 0
                session = _session_without_provider_attempt(session)
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

            workflow_policy_error = runtime._workflow_tool_policy_error(
                session=session,
                tool_name=plan_tool_call.tool_name,
            )
            if workflow_policy_error is not None:
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error=workflow_policy_error,
                    payload={
                        "kind": "workflow_tool_policy_denied",
                        "tool": plan_tool_call.tool_name,
                    },
                )
                raise ValueError(workflow_policy_error)

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
                            tool_instance=tool,
                            tool_call=plan_tool_call,
                            sequence=sequence,
                            permission_policy=active_permission_policy,
                        )
            else:
                permission_chunks = runtime._resolve_permission(
                    session=session,
                    tool=tool.definition,
                    tool_instance=tool,
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
                denied_pending = permission_chunks.denied_approval
                denied_replayed_tool_changed = denied_pending is not None and (
                    plan_tool_call.tool_name != denied_pending.tool_name
                    or dict(plan_tool_call.arguments) != denied_pending.arguments
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
                yield from self._permission_denied_tool_feedback_chunks(
                    session=session,
                    sequence=permission_chunks.last_sequence,
                    tool_call=denied_tool_call,
                    pending=denied_pending,
                    tool_results=tool_results,
                    tool_call_id=tool_call_id,
                )
                sequence = permission_chunks.last_sequence + 1
                if (
                    denied_replayed_tool_changed
                    or effective_runtime_config.execution_engine != "provider"
                ):
                    return
                continue

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
            if _is_abort_requested(active_graph_request):
                yield from self._started_tool_abort_chunks(
                    session=session,
                    sequence=sequence,
                    tool_call=plan_tool_call,
                    tool_call_id=tool_call_id,
                    abort_signal=active_graph_request.abort_signal,
                )
                return
            try:
                if self._is_progress_capable_tool(plan_tool_call.tool_name):
                    tool_outcome, sequence = yield from self._invoke_tool_with_progress_events(
                        tool=tool,
                        tool_call=plan_tool_call,
                        workspace=runtime._workspace,
                        tool_timeout=tool_timeout,
                        session=session,
                        start_sequence=sequence + 1,
                        tool_call_id=tool_call_id,
                        abort_signal=active_graph_request.abort_signal,
                        parent_session_id=session.session.parent_id,
                        delegation_depth=runtime._delegation_depth_from_metadata(session.metadata),
                        remaining_spawn_budget=runtime._remaining_spawn_budget_from_metadata(
                            session.metadata
                        ),
                    )
                    if isinstance(tool_outcome, Exception):
                        raise tool_outcome
                    tool_result = tool_outcome
                else:
                    with bind_runtime_tool_context(
                        RuntimeToolInvocationContext(
                            session_id=session.session.id,
                            parent_session_id=session.session.parent_id,
                            delegation_depth=runtime._delegation_depth_from_metadata(
                                session.metadata
                            ),
                            remaining_spawn_budget=runtime._remaining_spawn_budget_from_metadata(
                                session.metadata
                            ),
                            abort_signal=active_graph_request.abort_signal,
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
                    partial_timeout_payload: dict[str, object] = {}
                    partial_timeout_content: str | None = None
                    partial_timeout_error: str | None = None
                    partial_result = getattr(exc, "partial_result", None)
                    if isinstance(partial_result, ToolResult):
                        capped_partial = cap_tool_result_output(
                            partial_result,
                            workspace=runtime._workspace,
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
                                "content": _tool_error_content(plan_tool_call.tool_name, str(exc)),
                                **_tool_error_payload(
                                    tool_name=plan_tool_call.tool_name,
                                    error=str(exc),
                                ),
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
                    content=_tool_error_content(plan_tool_call.tool_name, str(exc)),
                    error=str(exc),
                    data={
                        "tool_call_id": tool_call_id,
                        "arguments": dict(plan_tool_call.arguments),
                    },
                    error_summary=_tool_error_summary(str(exc)),
                    error_details=_tool_error_details(
                        tool_name=plan_tool_call.tool_name,
                        error=str(exc),
                    ),
                    retry_guidance=_tool_error_retry_guidance(str(exc)),
                )

            runtime_tool_result_data = dict(tool_result.data)

            sanitized_arguments = sanitize_tool_arguments(dict(plan_tool_call.arguments))
            tool_result = cap_tool_result_output(
                tool_result,
                workspace=runtime._workspace,
                session_id=session.session.id,
                tool_call_id=tool_call_id,
            )
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

            if _is_abort_requested(active_graph_request):
                yield runtime._failed_chunk(
                    session=session,
                    sequence=sequence + 1,
                    error="run interrupted",
                    payload={
                        "kind": "interrupted",
                        "cancelled": True,
                        "run_id": runtime._run_id_from_session_metadata(session.metadata),
                        "reason": _abort_reason(active_graph_request),
                    },
                )
                return

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
            if provider_attempt != 0:
                pending_provider_attempt_reset = _provider_attempt_reset_after_tool_result(
                    provider_attempt=provider_attempt,
                    selection=runtime._graph_selection_for_effective_config(
                        effective_runtime_config,
                        provider_attempt=0,
                    ),
                    graph_request=active_graph_request,
                    session=session,
                )

    def _permission_denied_tool_feedback_chunks(
        self,
        *,
        session: SessionState,
        sequence: int,
        tool_call: ToolCall,
        pending: PendingApproval | None,
        tool_results: list[ToolResult],
        tool_call_id: str | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
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
                extra={"permission_denied": True},
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
        yield RuntimeStreamChunk(
            kind="event",
            session=session,
            event=EventEnvelope(
                session_id=session.session.id,
                sequence=sequence + 1,
                event_type="runtime.tool_completed",
                source="tool",
                payload={
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
            ),
        )
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

    @staticmethod
    def _current_session_state(session: SessionState) -> SessionState:
        return session

    @staticmethod
    def _build_context_pressure_payload(
        *,
        session: SessionState,
        context_window: RuntimeContextWindow,
        threshold: float,
        include_provider_usage: bool = False,
    ) -> dict[str, object] | None:
        if include_provider_usage:
            provider_payload = (
                RuntimeRunLoopCoordinator._build_provider_usage_context_pressure_payload(
                    session=session,
                    context_window=context_window,
                    threshold=threshold,
                )
            )
            if provider_payload is not None:
                return provider_payload

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
    def _build_provider_usage_context_pressure_payload(
        *,
        session: SessionState,
        context_window: RuntimeContextWindow,
        threshold: float,
    ) -> dict[str, object] | None:
        budget = RuntimeRunLoopCoordinator._provider_usage_budget(context_window)
        provider_total_tokens = RuntimeRunLoopCoordinator._latest_current_provider_total_tokens(
            session
        )
        if budget is None or provider_total_tokens is None:
            return None
        if budget <= 0 or provider_total_tokens <= 0:
            return None
        pressure_ratio = provider_total_tokens / budget
        if pressure_ratio < threshold:
            return None
        payload: dict[str, object] = {
            "kind": "pressure_signal",
            "session_id": session.session.id,
            "estimated_tokens": provider_total_tokens,
            "provider_total_tokens": provider_total_tokens,
            "budget_max_tokens": budget,
            "pressure_ratio": pressure_ratio,
            "threshold": threshold,
            "reason": "provider_usage_ratio_exceeded",
            "compacted": context_window.compacted,
            "token_estimate_source": "provider_usage",
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
    def _provider_usage_budget(context_window: RuntimeContextWindow) -> int | None:
        model_window = context_window.model_context_window_tokens
        if model_window is None:
            return None
        reserved_output_tokens = context_window.reserved_output_tokens or 0
        return max(1, model_window - reserved_output_tokens)

    @staticmethod
    def _latest_current_provider_total_tokens(session: SessionState) -> int | None:
        raw_provider_usage = session.metadata.get("provider_usage")
        if not isinstance(raw_provider_usage, dict):
            return None
        provider_usage = cast(dict[str, object], raw_provider_usage)
        current_run_id = RuntimeRunLoopCoordinator._current_run_id(session)
        latest_run_id = provider_usage.get("latest_run_id")
        if not isinstance(current_run_id, str) or latest_run_id != current_run_id:
            return None
        current_provider_attempt = RuntimeRunLoopCoordinator._current_provider_attempt(session)
        latest_provider_attempt = provider_usage.get("latest_provider_attempt")
        if latest_provider_attempt != current_provider_attempt:
            return None
        raw_latest = provider_usage.get("latest")
        if not isinstance(raw_latest, dict):
            return None
        latest = cast(dict[str, object], raw_latest)
        total_tokens = 0
        for key in (
            "input_tokens",
            "output_tokens",
            "cache_creation_tokens",
            "cache_read_tokens",
        ):
            raw_value = latest.get(key, 0)
            if not isinstance(raw_value, int) or isinstance(raw_value, bool):
                return None
            total_tokens += raw_value
        return total_tokens

    @staticmethod
    def _current_run_id(session: SessionState) -> str | None:
        raw_runtime_state = session.metadata.get("runtime_state")
        if not isinstance(raw_runtime_state, dict):
            return None
        runtime_state = cast(dict[str, object], raw_runtime_state)
        run_id = runtime_state.get("run_id")
        return run_id if isinstance(run_id, str) and run_id else None

    @staticmethod
    def _current_provider_attempt(session: SessionState) -> int:
        raw_provider_attempt = session.metadata.get("provider_attempt", 0)
        if isinstance(raw_provider_attempt, int) and not isinstance(raw_provider_attempt, bool):
            return raw_provider_attempt
        return 0

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
        session: SessionState,
        start_sequence: int,
    ) -> tuple[tuple[RuntimeStreamChunk, ...], SessionState, int]:
        runtime = self._runtime
        emitted: list[RuntimeStreamChunk] = []
        sequence = start_sequence - 1
        current_session: SessionState = session
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
