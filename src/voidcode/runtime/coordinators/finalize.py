"""Finalize-bucket coordinator: single owner of every runtime write path.

Owns what ``VoidCodeRuntime`` seals after the graph loop: incremental event
persistence, interrupted-terminal synthesis, response persistence, the
terminal-seal guard, and the stored-response readers the seal depends on.

Reads via constructor-injected collaborators plus a narrow ``RuntimeSurface``
for run/resume-owned composition (policy snapshots, hook execution, ACP
finalize, provider context). Run-mutable registry state (tool registry
materialization) stays owned by the service: this module never mutates it.
All storage writes below route through the narrow injected repositories.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

from ...provider.protocol import ProviderAbortSignal
from ..acp import AcpAdapter, disconnect_acp_for_session_state, finalize_run_acp
from ..contracts import RuntimeRequest, RuntimeResponse, RuntimeStreamChunk, UnknownSessionError
from ..event_envelopes import resequence_event
from ..events import EventEnvelope, EventSource
from ..execution import chunk_builders
from ..execution.provider_execution_metadata import run_id_from_session_metadata
from ..hook_runtime import (
    HOOK_RECURSION_ENV_VAR,
    hook_execution_policy_from_metadata,
    run_lifecycle_hooks_for_session,
)
from ..permission_policy import pending_approval_from_response, pending_question_from_response
from ..session import SessionState, SessionStatus, is_session_status_terminal
from ..session_metadata_helpers import session_without_tool_intent, waiting_reason_from_session

if TYPE_CHECKING:
    from ..background.supervisor import RuntimeBackgroundTaskSupervisor
    from ..config import RuntimeConfig
    from ..mcp import McpManager
    from ..runtime_surface import RuntimeSurface
    from ..storage import SessionEventRepository, SessionRecoveryRepository, SessionRepository, SessionRunWriter
logger = logging.getLogger(__name__)

_POLICY_PROJECTED_EVENT_TYPES = frozenset({"runtime.request_received"})


class FinalizeCoordinator:
    """Single write-path owner; see module docstring."""

    def __init__(
        self,
        surface: RuntimeSurface,
        *,
        events: SessionEventRepository,
        sessions: SessionRepository,
        run_writer: SessionRunWriter,
        recovery: SessionRecoveryRepository,
        workspace: Path,
        config: RuntimeConfig,
        acp_adapter: AcpAdapter,
        mcp_manager: McpManager,
        background_task_supervisor: RuntimeBackgroundTaskSupervisor,
        is_active_session: Callable[[str], bool],
        active_run_count: Callable[[str], int],
    ) -> None:
        self._surface = surface
        self._events = events
        self._sessions = sessions
        self._run_writer = run_writer
        self._recovery = recovery
        self._workspace = workspace
        self._config = config
        self._acp_adapter = acp_adapter
        self._mcp_manager = mcp_manager
        self._background_task_supervisor = background_task_supervisor
        self._is_active_session_fn = is_active_session
        self._active_run_count_fn = active_run_count

    def persist_emitted_event(
        self,
        *,
        session_id: str,
        event_type: str,
        source: EventSource,
        payload: dict[str, object],
        dedupe_key: str | None = None,
    ) -> EventEnvelope:
        """Persist one service-emitted event; return its DB-assigned envelope."""
        return self._events.append_session_events(
            workspace=self._workspace,
            session_id=session_id,
            events=((event_type, source, payload, dedupe_key),),
        )[0]

    def persist_emitted_chunk(self, chunk: RuntimeStreamChunk) -> RuntimeStreamChunk:
        event = chunk.event
        if event is None:
            return chunk
        envelope = self.persist_emitted_event(
            session_id=event.session_id,
            event_type=event.event_type,
            source=event.source,
            payload=event.payload,
        )
        return RuntimeStreamChunk(kind="event", session=chunk.session, event=envelope)

    def persist_emitted_chunks(
        self,
        chunks: Iterable[RuntimeStreamChunk],
        *,
        fallback_sequence: int,
    ):

        sequence = fallback_sequence
        for chunk in chunks:
            event = chunk.event
            if event is None:
                yield chunk
                continue
            envelope = self.persist_emitted_event(
                session_id=event.session_id,
                event_type=event.event_type,
                source=event.source,
                payload=event.payload,
            )
            sequence = envelope.sequence
            yield RuntimeStreamChunk(kind="event", session=chunk.session, event=envelope)
        return sequence

    def load_stored_response(self, *, session_id: str) -> RuntimeResponse:
        from ..session import validate_session_workspace

        response = self._sessions.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(response.session, session_id=session_id, workspace=self._workspace)
        return response

    def load_existing_session_if_present(self, *, session_id: str) -> RuntimeResponse | None:
        if not self._sessions.has_session(workspace=self._workspace, session_id=session_id):
            return None
        return self.load_stored_response(session_id=session_id)

    def save_interrupted_checkpoint(
        self,
        *,
        session_id: str,
        prompt: str,
        session_metadata: dict[str, object],
        tool_results: tuple[dict[str, object], ...] | list[dict[str, object]],
        last_event_sequence: int,
        output: str | None,
        create_if_missing: bool = False,
        turn: int | None = None,
        parent_session_id: str | None = None,
    ) -> None:
        normalized_results = tuple(tool_results)
        self._recovery.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=session_id,
            prompt=prompt,
            session_metadata=session_metadata,
            tool_results=normalized_results,
            last_event_sequence=last_event_sequence,
            output=output,
            create_if_missing=create_if_missing,
            turn=turn if turn is not None else 1,
            parent_session_id=parent_session_id,
        )

    def sealed_session_status(self, *, session_id: str) -> SessionStatus | None:
        """Return the terminal status sealing ``session_id``, or None when mutable."""
        if self._is_active_session_fn(session_id):
            return None
        try:
            status = self._sessions.load_session_status(workspace=self._workspace, session_id=session_id)
        except UnknownSessionError:
            return None
        if is_session_status_terminal(status):
            return status
        return None

    @staticmethod
    def request_for_persisted_response(
        request: RuntimeRequest,
        response: RuntimeResponse,
    ) -> RuntimeRequest:
        """Use the startup-drained follow-up prompt for checkpoint identity."""
        from dataclasses import replace

        request_events = tuple(event for event in response.events if event.event_type == "runtime.request_received")
        if len(request_events) != 1:
            return request
        event_prompt = request_events[0].payload.get("prompt")
        if not isinstance(event_prompt, str) or event_prompt == request.prompt:
            return request
        steering_prefix = f"{request.prompt}\n\nRuntime steering messages:"
        if event_prompt.startswith(steering_prefix):
            return request
        return replace(request, prompt=event_prompt)

    def persist_response(self, *, request: RuntimeRequest, response: RuntimeResponse) -> None:
        request = self.request_for_persisted_response(request, response)
        if response.session.status in {"completed", "failed"}:
            cleaned_session = session_without_tool_intent(response.session)
            if cleaned_session is not response.session:
                response = RuntimeResponse(
                    session=cleaned_session,
                    events=response.events,
                    output=response.output,
                )
        if response.session.status == "waiting":
            pending_question = pending_question_from_response(response)
            if pending_question is not None:
                self._recovery.save_pending_question(
                    workspace=self._workspace,
                    request=request,
                    response=response,
                    pending_question=pending_question,
                )
                return
            pending_approval = pending_approval_from_response(response)
            self._recovery.save_pending_approval(
                workspace=self._workspace,
                request=request,
                response=response,
                pending_approval=pending_approval,
            )
            return
        seal_terminal_status = self._active_run_count_fn(response.session.session.id) <= 1
        self._run_writer.save_run(
            workspace=self._workspace,
            request=request,
            response=response,
            seal_terminal_status=seal_terminal_status,
        )

    @staticmethod
    def interrupt_requested_but_not_emitted(
        *,
        abort_signal: ProviderAbortSignal | None,
        final_session: SessionState | None,
        events: list[EventEnvelope],
    ) -> bool:
        """True when the user interrupted the run but no cancelled terminal event reached the stream."""
        if abort_signal is None or not abort_signal.cancelled:
            return False
        if final_session is None or final_session.status != "running":
            return False
        return not any(event.event_type == "runtime.failed" for event in events)

    def persist_interrupted_terminal_on_generator_close(
        self,
        *,
        request: RuntimeRequest,
        abort_signal: ProviderAbortSignal | None,
        final_session: SessionState | None,
        events: list[EventEnvelope],
        run_id: str,
    ) -> None:
        """Persist cancellation when a consumer closes the stream at a yield."""
        if not self.interrupt_requested_but_not_emitted(
            abort_signal=abort_signal,
            final_session=final_session,
            events=events,
        ):
            return
        assert final_session is not None

        stored = self.load_stored_response(session_id=final_session.session.id)
        if any(
            event.event_type == "runtime.failed" and event.payload.get("kind") == "interrupted" and event.payload.get("cancelled") is True
            for event in stored.events
        ):
            return

        failed_chunk = chunk_builders.failed_chunk(
            session=final_session,
            sequence=0,
            error="run interrupted",
            payload=chunk_builders.user_interrupted_payload(
                run_id=run_id_from_session_metadata(final_session.metadata) or run_id,
                reason=abort_signal.reason if abort_signal is not None else None,
            ),
            status="interrupted",
        )
        failed_event = failed_chunk.event
        assert failed_event is not None
        persisted_event = self.persist_emitted_event(
            session_id=failed_event.session_id,
            event_type=failed_event.event_type,
            source=failed_event.source,
            payload=failed_event.payload,
        )

        checkpoint = self._recovery.load_resume_checkpoint(
            workspace=self._workspace,
            session_id=final_session.session.id,
        )
        raw_tool_results = checkpoint.get("tool_results", []) if isinstance(checkpoint, dict) else []
        tool_results: tuple[dict[str, object], ...] = (
            tuple(item for item in raw_tool_results if isinstance(item, dict)) if isinstance(raw_tool_results, list) else ()
        )
        self._recovery.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=final_session.session.id,
            prompt=request.prompt,
            session_metadata=final_session.metadata,
            tool_results=tool_results,
            last_event_sequence=persisted_event.sequence,
            output=None,
            create_if_missing=False,
            turn=final_session.turn,
            parent_session_id=final_session.session.parent_id,
        )

    def synthesize_interrupted_terminal(
        self,
        *,
        session: SessionState,
        events: list[EventEnvelope],
        run_id: str,
        reason: str | None,
    ):
        """Persist a synthetic ``runtime.failed{cancelled:true}`` terminal event."""
        failed_chunk = chunk_builders.failed_chunk(
            session=session,
            sequence=0,
            error="run interrupted",
            payload=chunk_builders.user_interrupted_payload(
                run_id=run_id_from_session_metadata(session.metadata) or run_id,
                reason=reason,
            ),
            status="interrupted",
        )
        persisted = self.persist_emitted_chunk(failed_chunk)
        if persisted.event is not None:
            events.append(persisted.event)
        yield persisted
        return persisted.session

    def events_with_runtime_policy_projection(
        self,
        events: tuple[EventEnvelope, ...],
        *,
        metadata: dict[str, object],
    ) -> tuple[EventEnvelope, ...]:
        from ..events import runtime_policy_observability_payload

        raw_policy = metadata.get("runtime_policy")
        if not isinstance(raw_policy, dict):
            return events
        projected: list[EventEnvelope] = []
        for event in events:
            if event.event_type not in _POLICY_PROJECTED_EVENT_TYPES:
                projected.append(event)
                continue
            projected.append(
                EventEnvelope(
                    session_id=event.session_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    source=event.source,
                    payload={
                        **event.payload,
                        "runtime_policy": runtime_policy_observability_payload(raw_policy),
                    },
                )
            )
        return tuple(projected)

    def load_replay_response(self, *, session_id: str) -> RuntimeResponse:
        from ..session import SessionState as _SessionState
        from ..session import session_metadata_for_replay

        response = self.load_stored_response(session_id=session_id)
        if "runtime_config" in response.session.metadata:
            self._surface.effective_runtime_config_from_metadata(response.session.metadata)
        projected_metadata = session_metadata_for_replay(response.session.metadata)
        replay_events = self.events_with_runtime_policy_projection(
            response.events,
            metadata=projected_metadata,
        )
        return RuntimeResponse(
            session=_SessionState(
                session=response.session.session,
                status=response.session.status,
                turn=response.session.turn,
                metadata=projected_metadata,
            ),
            events=replay_events,
            output=response.output,
        )

    def finalize_stream_run(
        self,
        last_chunk: RuntimeStreamChunk | None,
        last_sequence: int,
        deferred_failed_chunk: RuntimeStreamChunk | None,
        graph_loop_error: Exception | None,
    ) -> Generator[RuntimeStreamChunk]:
        if last_chunk is None:
            if graph_loop_error is not None:
                raise graph_loop_error
            return

        if deferred_failed_chunk is not None:
            failed_event = deferred_failed_chunk.require_event()
            cleanup_sequence = failed_event.sequence - 1
            final_chunks, finalized_session, final_sequence = finalize_run_acp(
                self._acp_adapter,
                session=deferred_failed_chunk.session,
                sequence=cleanup_sequence,
            )
            final_sequence = yield from self.persist_emitted_chunks(
                final_chunks,
                fallback_sequence=final_sequence,
            )
            end_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=finalized_session,
                surface="session_end",
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                sequence=final_sequence,
                payload={"session_status": finalized_session.status},
                policy=hook_execution_policy_from_metadata(finalized_session.metadata),
            )
            release_sequence = yield from self.persist_emitted_chunks(
                end_hook_outcome.chunks,
                fallback_sequence=end_hook_outcome.last_sequence,
            )
            if end_hook_outcome.failed_error is not None:
                hook_failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=finalized_session,
                    sequence=end_hook_outcome.last_sequence,
                    surface="session_end",
                    error=end_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if hook_failed_chunk is not None:
                    persisted_hook_failed = self.persist_emitted_chunk(hook_failed_chunk)
                    yield persisted_hook_failed
                    release_sequence = persisted_hook_failed.event.sequence if persisted_hook_failed.event is not None else release_sequence
            yield RuntimeStreamChunk(
                kind="event",
                session=deferred_failed_chunk.session,
                event=resequence_event(failed_event, sequence=release_sequence + 1),
            )
            if graph_loop_error is not None:
                raise graph_loop_error
            return

        if graph_loop_error is not None:
            raise graph_loop_error

        if (
            last_chunk.event is not None
            and last_chunk.event.event_type == "runtime.tool_completed"
            and last_chunk.event.payload.get("permission_denied") is True
        ):
            return

        if last_chunk.session.status == "waiting":
            idle_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=last_chunk.session,
                surface="session_idle",
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                sequence=last_sequence,
                payload={"reason": waiting_reason_from_session(last_chunk.session)},
                policy=hook_execution_policy_from_metadata(last_chunk.session.metadata),
            )
            yield from self.persist_emitted_chunks(
                idle_hook_outcome.chunks,
                fallback_sequence=idle_hook_outcome.last_sequence,
            )
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=disconnect_acp_for_session_state(self._acp_adapter, last_chunk.session),
                    sequence=idle_hook_outcome.last_sequence,
                    surface="session_idle",
                    error=idle_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    yield self.persist_emitted_chunk(failed_chunk)
            return

        final_chunks, finalized_session, final_sequence = finalize_run_acp(
            self._acp_adapter,
            session=last_chunk.session,
            sequence=last_sequence,
        )
        final_sequence = yield from self.persist_emitted_chunks(
            final_chunks,
            fallback_sequence=final_sequence,
        )
        end_hook_outcome = run_lifecycle_hooks_for_session(
            hooks=self._config.hooks,
            workspace=self._workspace,
            session=finalized_session,
            surface="session_end",
            recursion_env_var=HOOK_RECURSION_ENV_VAR,
            sequence=final_sequence,
            payload={"session_status": finalized_session.status},
            policy=hook_execution_policy_from_metadata(finalized_session.metadata),
        )
        release_sequence = yield from self.persist_emitted_chunks(
            end_hook_outcome.chunks,
            fallback_sequence=end_hook_outcome.last_sequence,
        )
        if end_hook_outcome.failed_error is not None:
            failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                session=finalized_session,
                sequence=end_hook_outcome.last_sequence,
                surface="session_end",
                error=end_hook_outcome.failed_error,
                hooks=self._config.hooks,
            )
            if failed_chunk is not None:
                persisted_failed_chunk = self.persist_emitted_chunk(failed_chunk)
                yield persisted_failed_chunk
                release_sequence = persisted_failed_chunk.event.sequence if persisted_failed_chunk.event is not None else release_sequence
