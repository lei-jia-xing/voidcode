# pyright: reportPrivateUsage=false
from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from ..graph.contracts import GraphRunRequest
from ..provider.protocol import ProviderAbortSignal
from ..tools.contracts import ToolResult, ToolResultStatus
from ..tools.output import sanitize_tool_result_data
from ..tools.question import QuestionTool
from .config import serialize_runtime_agent_config
from .contracts import (
    NoPendingQuestionError,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeResponse,
    RuntimeStreamChunk,
)
from .events import RUNTIME_QUESTION_ANSWERED, RUNTIME_SKILLS_BINDING_MISMATCH, EventEnvelope
from .permission import PendingApproval, PermissionResolution
from .question import PendingQuestion, QuestionResponse
from .session import SessionState

if TYPE_CHECKING:
    from .service import VoidCodeRuntime


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ApprovalResumeCheckpointState:
    prompt: str
    session_metadata: dict[str, object]
    tool_results: tuple[ToolResult, ...]


@dataclass(frozen=True, slots=True)
class PersistedResumeCheckpointEnvelope:
    kind: str
    version: int
    payload: dict[str, object]


def _metadata_with_resume_run_id(
    metadata: dict[str, object], *, run_id: str | None
) -> dict[str, object]:
    if run_id is None:
        return metadata
    raw_runtime_state = metadata.get("runtime_state")
    runtime_state = (
        dict(cast(dict[str, object], raw_runtime_state))
        if isinstance(raw_runtime_state, dict)
        else {}
    )
    runtime_state["run_id"] = run_id
    return {**metadata, "runtime_state": runtime_state}


class RuntimeResumeCoordinator:
    def __init__(self, runtime: VoidCodeRuntime) -> None:
        self._runtime = runtime

    def resume_pending_approval_stream(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        stored_response, pending, checkpoint = self._load_pending_approval_context(
            session_id=session_id,
            approval_request_id=approval_request_id,
        )
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: Any | None = None
        for chunk in self.resume_pending_approval_impl(
            stored=stored_response,
            pending=pending,
            approval_decision=approval_decision,
            checkpoint=checkpoint,
            run_id=run_id,
            abort_signal=abort_signal,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
            yield chunk
        if finalize_background_task:
            response = self.response_from_resumed_chunks(
                stored_response=stored_response,
                streamed_events=streamed_events,
                output=output,
                final_session=final_session,
            )
            self._runtime._finalize_background_task_from_session_response(session_response=response)

    def resume_pending_approval_response(
        self,
        *,
        session_id: str,
        approval_request_id: str,
        approval_decision: PermissionResolution,
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        stored_response, pending, checkpoint = self._load_pending_approval_context(
            session_id=session_id,
            approval_request_id=approval_request_id,
        )
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: Any | None = None
        for chunk in self.resume_pending_approval_impl(
            stored=stored_response,
            pending=pending,
            approval_decision=approval_decision,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
        response = self.response_from_resumed_chunks(
            stored_response=stored_response,
            streamed_events=streamed_events,
            output=output,
            final_session=final_session,
        )
        return stored_response.events, response

    def answer_pending_question_stream(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        stored_response, pending, checkpoint, normalized_responses = (
            self._load_pending_question_context(
                session_id=session_id,
                question_request_id=question_request_id,
                responses=responses,
            )
        )
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: Any | None = None
        for chunk in self.answer_pending_question_impl(
            stored=stored_response,
            pending=pending,
            responses=normalized_responses,
            checkpoint=checkpoint,
            run_id=run_id,
            abort_signal=abort_signal,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
            yield chunk
        if finalize_background_task:
            response = self.response_from_resumed_chunks(
                stored_response=stored_response,
                streamed_events=streamed_events,
                output=output,
                final_session=final_session,
            )
            self._runtime._finalize_background_task_from_session_response(session_response=response)

    def answer_pending_question_response(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        stored_response, pending, checkpoint, normalized_responses = (
            self._load_pending_question_context(
                session_id=session_id,
                question_request_id=question_request_id,
                responses=responses,
            )
        )
        streamed_events: list[EventEnvelope] = []
        output: str | None = None
        final_session: Any | None = None
        for chunk in self.answer_pending_question_impl(
            stored=stored_response,
            pending=pending,
            responses=normalized_responses,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.event is not None:
                streamed_events.append(chunk.event)
            if chunk.kind == "output":
                output = chunk.output
        response = self.response_from_resumed_chunks(
            stored_response=stored_response,
            streamed_events=streamed_events,
            output=output,
            final_session=final_session,
        )
        return stored_response.events, response

    def answer_pending_question_impl(
        self,
        *,
        stored: Any,
        pending: PendingQuestion,
        responses: tuple[QuestionResponse, ...],
        checkpoint: dict[str, object] | None,
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        runtime = self._runtime
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=stored.session.metadata,
        )
        max_stored_sequence = stored.events[-1].sequence if stored.events else 0
        question_answer_result = QuestionTool.answer_tool_result(responses)

        checkpoint_state = self.question_resume_state_from_checkpoint(
            checkpoint=checkpoint,
            pending=pending,
            stored_metadata=stored.session.metadata,
        )
        if checkpoint_state is not None:
            prompt = checkpoint_state.prompt
            session = SessionState(
                session=stored.session.session,
                status="running",
                turn=stored.session.turn,
                metadata=checkpoint_state.session_metadata,
            )
            tool_results: list[ToolResult] = list(checkpoint_state.tool_results)
        else:
            prompt, tool_results = self._resume_prompt_and_tool_results_from_stored_events(
                stored.events
            )
        session = SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata=_metadata_with_resume_run_id(session.metadata, run_id=run_id),
        )
        runtime._validate_session_workspace(session, session_id=stored.session.session.id)
        tool_results.append(question_answer_result)

        sequence = max_stored_sequence + 1
        answered_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type=RUNTIME_QUESTION_ANSWERED,
            source="runtime",
            payload={
                "request_id": pending.request_id,
                "responses": [
                    {"header": response.header, "answers": list(response.answers)}
                    for response in responses
                ],
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=answered_event)
        sequence += 1
        loop_events = [answered_event]
        tool_completed_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.tool_completed",
            source="tool",
            payload={
                "tool": question_answer_result.tool_name,
                "status": question_answer_result.status,
                "content": question_answer_result.content,
                "error": question_answer_result.error,
                **question_answer_result.data,
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=tool_completed_event)
        loop_events.append(tool_completed_event)

        effective_config = runtime._effective_runtime_config_from_metadata(session.metadata)
        try:
            runtime._validate_reasoning_effort_capability(effective_config)
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        tool_registry = runtime._tool_registry_for_effective_config(effective_config)
        skill_registry = runtime._skill_registry_for_effective_config(effective_config)
        resumed_skill_snapshot = runtime._build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="resume",
        )
        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=tool_registry.definitions(),
            context_window=runtime._prepare_provider_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            assembled_context=runtime._assemble_provider_context(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
                skill_prompt_context=resumed_skill_snapshot.skill_prompt_context,
            ),
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                "provider_attempt": (
                    session.metadata.get("provider_attempt", 0)
                    if isinstance(session.metadata.get("provider_attempt", 0), int)
                    else 0
                ),
                **(
                    {"reasoning_effort": effective_config.reasoning_effort}
                    if effective_config.reasoning_effort is not None
                    and "reasoning_effort" not in session.metadata
                    else {}
                ),
            },
            abort_signal=abort_signal,
        )
        graph = runtime._graph_for_session_metadata(session.metadata)
        output: str | None = None
        final_session = session
        last_sequence = sequence
        try:
            for chunk in runtime._execute_graph_loop(
                graph=graph,
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                graph_request=graph_request,
                tool_results=tool_results,
                permission_policy=runtime._permission_policy_for_session(session.metadata),
                preserved_continuity_state=runtime._continuity_state_from_session_metadata(
                    session.metadata
                ),
            ):
                final_session = chunk.session
                if chunk.event is not None:
                    last_sequence = chunk.event.sequence
                    loop_events.append(chunk.event)
                if chunk.kind == "output":
                    output = chunk.output
                yield chunk
        except Exception:
            if final_session.status == "failed":
                response = RuntimeResponse(
                    session=final_session,
                    events=stored.events + tuple(loop_events),
                    output=output,
                )
                request = RuntimeRequest(
                    prompt=prompt,
                    session_id=stored.session.session.id,
                    parent_session_id=stored.session.session.parent_id,
                )
                runtime._persist_response(request=request, response=response)
                return
            raise

        if final_session.status == "waiting":
            final_session = runtime._disconnect_acp_for_session_state(final_session)
            waiting_response = RuntimeResponse(
                session=final_session,
                events=stored.events + tuple(loop_events),
                output=output,
            )
            idle_reason = runtime._resume_waiting_reason(waiting_response)
            idle_hook_outcome = runtime._run_lifecycle_hooks(
                session=final_session,
                sequence=last_sequence,
                surface="session_idle",
                payload={"reason": idle_reason, "resume": True},
            )
            for hook_chunk in idle_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                last_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = runtime._failed_chunk(
                    session=final_session,
                    sequence=idle_hook_outcome.last_sequence + 1,
                    error=idle_hook_outcome.failed_error,
                )
                failed_event = cast(EventEnvelope, failed_chunk.event)
                loop_events.append(failed_event)
                final_session = failed_chunk.session
                yield failed_chunk
        else:
            final_chunks, final_session, final_sequence = runtime._finalize_run_acp(
                session=final_session,
                sequence=last_sequence,
            )
            for chunk in final_chunks:
                if chunk.event is not None:
                    last_sequence += 1
                    resequenced_event = runtime._resequence_event(
                        chunk.event, sequence=last_sequence
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )
            end_hook_outcome = runtime._run_lifecycle_hooks(
                session=final_session,
                sequence=max(last_sequence, final_sequence),
                surface="session_end",
                payload={"session_status": final_session.status, "resume": True},
            )
            for hook_chunk in end_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                last_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if end_hook_outcome.failed_error is not None:
                logger.warning(
                    "session_end hook failed for %s during question resume: %s",
                    final_session.session.id,
                    end_hook_outcome.failed_error,
                )
            release_sequence = end_hook_outcome.last_sequence
            for release_event in runtime._release_mcp_session_events(
                session_id=final_session.session.id,
                start_sequence=release_sequence + 1,
            ):
                release_sequence = release_event.sequence
                last_sequence = release_event.sequence
                loop_events.append(release_event)
                yield RuntimeStreamChunk(
                    kind="event",
                    session=final_session,
                    event=release_event,
                )

        response = RuntimeResponse(
            session=final_session,
            events=stored.events + tuple(loop_events),
            output=output,
        )
        request = RuntimeRequest(
            prompt=prompt,
            session_id=stored.session.session.id,
            parent_session_id=stored.session.session.parent_id,
        )
        runtime._persist_response(request=request, response=response)

    def response_from_resumed_chunks(
        self,
        *,
        stored_response: Any,
        streamed_events: list[EventEnvelope],
        output: str | None,
        final_session: Any | None,
    ) -> RuntimeResponse:
        if final_session is None:
            raise ValueError("runtime stream emitted no chunks")
        if final_session.status == "waiting":
            final_session = self._runtime._reload_persisted_session(
                session_id=final_session.session.id
            )
        resolved_session = cast(SessionState, final_session)
        return RuntimeResponse(
            session=resolved_session,
            events=stored_response.events + tuple(streamed_events),
            output=output,
        )

    def resume_pending_approval_impl(
        self,
        *,
        stored: Any,
        pending: PendingApproval,
        approval_decision: PermissionResolution,
        checkpoint: dict[str, object] | None,
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
    ) -> Iterator[RuntimeStreamChunk]:
        runtime = self._runtime
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=stored.session.metadata,
        )
        max_stored_sequence = stored.events[-1].sequence if stored.events else 0
        loop_events: list[EventEnvelope] = []
        output: str | None = None

        checkpoint_state = self.approval_resume_state_from_checkpoint(
            checkpoint=checkpoint,
            pending=pending,
            stored_metadata=stored.session.metadata,
        )
        binding_mismatch_payload: dict[str, object] | None = None
        if checkpoint is not None:
            checkpoint_binding = checkpoint.get("skill_binding_snapshot")
            checkpoint_binding_payload = (
                cast(dict[str, object], checkpoint_binding)
                if isinstance(checkpoint_binding, dict)
                else None
            )
            if checkpoint_binding_payload is not None:
                stored_snapshot_payload = cast(
                    dict[str, object] | None,
                    stored.session.metadata.get("skill_snapshot"),
                )
                stored_binding_payload = (
                    cast(dict[str, object], stored_snapshot_payload.get("binding_snapshot"))
                    if isinstance(stored_snapshot_payload, dict)
                    and isinstance(stored_snapshot_payload.get("binding_snapshot"), dict)
                    else None
                )
                mismatch_payload = runtime._skill_binding_mismatch_payload(
                    checkpoint_binding_payload,
                    stored_binding_payload,
                )
                if cast(bool, mismatch_payload["mismatch"]):
                    binding_mismatch_payload = mismatch_payload
        if checkpoint_state is not None:
            prompt = checkpoint_state.prompt
            session = SessionState(
                session=stored.session.session,
                status="running",
                turn=stored.session.turn,
                metadata=checkpoint_state.session_metadata,
            )
            tool_results: list[ToolResult] = list(checkpoint_state.tool_results)
        else:
            prompt, tool_results = self._resume_prompt_and_tool_results_from_stored_events(
                stored.events
            )

        session = SessionState(
            session=session.session,
            status=session.status,
            turn=session.turn,
            metadata=_metadata_with_resume_run_id(session.metadata, run_id=run_id),
        )
        runtime._validate_session_workspace(session, session_id=stored.session.session.id)
        session = runtime._session_with_current_acp_metadata(session)
        preserved_continuity_state = runtime._continuity_state_from_session_metadata(
            session.metadata
        )
        mcp_startup_chunks, session, _, mcp_failed_chunk = runtime._refresh_mcp_tools_for_session(
            session=session,
            sequence=max_stored_sequence,
            failure_kind="mcp_startup_failed",
        )
        effective_config = runtime._effective_runtime_config_from_metadata(session.metadata)
        try:
            runtime._validate_reasoning_effort_capability(effective_config)
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        tool_registry = runtime._tool_registry_for_effective_config(effective_config)
        skill_registry = runtime._skill_registry_for_effective_config(effective_config)

        resumed_skill_snapshot = runtime._build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="resume",
        )

        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=tool_registry.definitions(),
            context_window=runtime._prepare_provider_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            assembled_context=runtime._assemble_provider_context(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
                skill_prompt_context=resumed_skill_snapshot.skill_prompt_context,
            ),
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                "provider_attempt": (
                    session.metadata.get("provider_attempt", 0)
                    if isinstance(session.metadata.get("provider_attempt", 0), int)
                    else 0
                ),
                **(
                    {"reasoning_effort": effective_config.reasoning_effort}
                    if effective_config.reasoning_effort is not None
                    and "reasoning_effort" not in session.metadata
                    else {}
                ),
            },
            abort_signal=abort_signal,
        )
        provider_attempt = runtime._provider_attempt_from_metadata(graph_request.metadata)
        graph = runtime._graph_for_session_metadata(session.metadata)
        if provider_attempt > 0:
            graph = runtime._graph_selection_for_effective_config(
                runtime._effective_runtime_config_from_metadata(session.metadata),
                provider_attempt=provider_attempt,
            ).graph

        emitted_sequence = max_stored_sequence
        if binding_mismatch_payload is not None:
            emitted_sequence += 1
            mismatch_event = EventEnvelope(
                session_id=session.session.id,
                sequence=emitted_sequence,
                event_type=RUNTIME_SKILLS_BINDING_MISMATCH,
                source="runtime",
                payload={
                    **binding_mismatch_payload,
                    "resume": True,
                    "approval_request_id": pending.request_id,
                },
            )
            loop_events.append(mismatch_event)
            yield RuntimeStreamChunk(kind="event", session=session, event=mismatch_event)
        for chunk in mcp_startup_chunks:
            emitted_sequence += 1
            resequenced_event = runtime._resequence_event(
                cast(EventEnvelope, chunk.event), sequence=emitted_sequence
            )
            loop_events.append(resequenced_event)
            yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
        if mcp_failed_chunk is not None:
            emitted_sequence += 1
            resequenced_failed = runtime._resequence_event(
                cast(EventEnvelope, mcp_failed_chunk.event), sequence=emitted_sequence
            )
            response = RuntimeResponse(
                session=mcp_failed_chunk.session,
                events=stored.events + tuple(loop_events) + (resequenced_failed,),
                output=output,
            )
            request = RuntimeRequest(
                prompt=prompt,
                session_id=stored.session.session.id,
                parent_session_id=stored.session.session.parent_id,
            )
            runtime._persist_response(request=request, response=response)
            yield RuntimeStreamChunk(
                kind="event",
                session=mcp_failed_chunk.session,
                event=resequenced_failed,
            )
            return

        deferred_startup_acp_events: tuple[object, ...] = ()
        if runtime.current_acp_state().configuration.configured_enabled is True:
            try:
                deferred_startup_acp_events = runtime._acp_adapter.connect()
            except Exception as exc:
                startup_chunks, session, last_sequence = runtime._emit_current_acp_drain(
                    session=session,
                    start_sequence=max_stored_sequence + 1,
                )
                startup_failed_chunk = runtime._failed_chunk(
                    session=runtime._session_with_current_acp_metadata(session),
                    sequence=last_sequence + 1,
                    error=str(exc),
                    payload={"kind": "acp_startup_failed"},
                )
            else:
                session = runtime._session_with_current_acp_metadata(session)
                startup_chunks = ()
                startup_failed_chunk = None
        else:
            startup_chunks = ()
            startup_failed_chunk = None
        for chunk in startup_chunks:
            emitted_sequence += 1
            resequenced_event = runtime._resequence_event(
                cast(EventEnvelope, chunk.event), sequence=emitted_sequence
            )
            loop_events.append(resequenced_event)
            yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
        if startup_failed_chunk is not None:
            emitted_sequence += 1
            resequenced_failed = runtime._resequence_event(
                cast(EventEnvelope, startup_failed_chunk.event),
                sequence=emitted_sequence,
            )
            response = RuntimeResponse(
                session=startup_failed_chunk.session,
                events=stored.events + tuple(loop_events) + (resequenced_failed,),
                output=output,
            )
            request = RuntimeRequest(
                prompt=prompt,
                session_id=stored.session.session.id,
                parent_session_id=stored.session.session.parent_id,
            )
            runtime._persist_response(request=request, response=response)
            yield RuntimeStreamChunk(
                kind="event",
                session=startup_failed_chunk.session,
                event=resequenced_failed,
            )
            return

        sequence = max_stored_sequence
        try:
            for chunk in runtime._execute_graph_loop(
                graph=graph,
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                graph_request=graph_request,
                tool_results=tool_results,
                approval_resolution=(pending, approval_decision),
                permission_policy=runtime._permission_policy_for_session(session.metadata),
                preserved_continuity_state=preserved_continuity_state,
            ):
                if deferred_startup_acp_events and (
                    (
                        chunk.event is not None
                        and chunk.event.event_type
                        in {"runtime.approval_resolved", "runtime.failed"}
                    )
                    or chunk.kind == "output"
                ):
                    startup_chunks, updated_session, _ = runtime._emit_acp_events(
                        session=chunk.session,
                        start_sequence=emitted_sequence + 1,
                        acp_events=deferred_startup_acp_events,
                    )
                    deferred_startup_acp_events = ()
                    for startup_chunk in startup_chunks:
                        startup_event = cast(EventEnvelope, startup_chunk.event)
                        emitted_sequence = startup_event.sequence
                        loop_events.append(startup_event)
                        yield startup_chunk
                    if chunk.event is not None:
                        chunk = RuntimeStreamChunk(
                            kind="event",
                            session=updated_session,
                            event=chunk.event,
                        )
                    elif chunk.kind == "output":
                        chunk = RuntimeStreamChunk(
                            kind="output",
                            session=updated_session,
                            output=chunk.output,
                        )
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = runtime._resequence_event(
                        chunk.event, sequence=emitted_sequence
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )
                if chunk.kind == "output":
                    output = chunk.output
                    yield chunk
                session = chunk.session
        except Exception:
            if session.status == "failed":
                response = RuntimeResponse(
                    session=session,
                    events=stored.events + tuple(loop_events),
                    output=output,
                )
                request = RuntimeRequest(
                    prompt=prompt,
                    session_id=stored.session.session.id,
                    parent_session_id=stored.session.session.parent_id,
                )
                runtime._persist_response(request=request, response=response)
                return
            raise

        if deferred_startup_acp_events:
            startup_chunks, session, _ = runtime._emit_acp_events(
                session=session,
                start_sequence=emitted_sequence + 1,
                acp_events=deferred_startup_acp_events,
            )
            for startup_chunk in startup_chunks:
                startup_event = cast(EventEnvelope, startup_chunk.event)
                emitted_sequence = startup_event.sequence
                loop_events.append(startup_event)
                yield startup_chunk

        last_sequence = emitted_sequence
        if session.status == "waiting":
            session = runtime._disconnect_acp_for_session_state(session)
            idle_hook_outcome = runtime._run_lifecycle_hooks(
                session=session,
                sequence=last_sequence,
                surface="session_idle",
                payload={"reason": "waiting_for_approval", "resume": True},
            )
            for hook_chunk in idle_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                emitted_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = runtime._failed_chunk(
                    session=session,
                    sequence=idle_hook_outcome.last_sequence + 1,
                    error=idle_hook_outcome.failed_error,
                )
                failed_event = cast(EventEnvelope, failed_chunk.event)
                loop_events.append(failed_event)
                session = failed_chunk.session
                yield failed_chunk
        else:
            final_chunks, session, _ = runtime._finalize_run_acp(
                session=session,
                sequence=last_sequence,
            )
            for chunk in final_chunks:
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = runtime._resequence_event(
                        chunk.event, sequence=emitted_sequence
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(
                        kind="event", session=chunk.session, event=resequenced_event
                    )
            end_hook_outcome = runtime._run_lifecycle_hooks(
                session=session,
                sequence=emitted_sequence,
                surface="session_end",
                payload={"session_status": session.status, "resume": True},
            )
            for hook_chunk in end_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                emitted_sequence = hook_event.sequence
                loop_events.append(hook_event)
                yield hook_chunk
            if end_hook_outcome.failed_error is not None:
                logger.warning(
                    "session_end hook failed for %s during approval resume: %s",
                    session.session.id,
                    end_hook_outcome.failed_error,
                )
            for release_event in runtime._release_mcp_session_events(
                session_id=session.session.id,
                start_sequence=end_hook_outcome.last_sequence + 1,
            ):
                emitted_sequence = release_event.sequence
                loop_events.append(release_event)
                yield RuntimeStreamChunk(
                    kind="event",
                    session=session,
                    event=release_event,
                )

        response = RuntimeResponse(
            session=session,
            events=stored.events + tuple(loop_events),
            output=output,
        )
        request = RuntimeRequest(
            prompt=prompt,
            session_id=stored.session.session.id,
            parent_session_id=stored.session.session.parent_id,
        )
        runtime._persist_response(request=request, response=response)

    def approval_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingApproval,
        stored_metadata: dict[str, object],
    ) -> ApprovalResumeCheckpointState | None:
        checkpoint_envelope = self.validated_resume_checkpoint_envelope(
            checkpoint=checkpoint,
            expected_kind="approval_wait",
        )
        if checkpoint_envelope is None:
            return None
        checkpoint_payload = checkpoint_envelope.payload
        if checkpoint_payload.get("pending_approval_request_id") != pending.request_id:
            raise ValueError(
                "persisted approval resume checkpoint request id does not match pending approval"
            )
        checkpoint_snapshot_hash = checkpoint_payload.get("skill_snapshot_hash")
        stored_snapshot_payload = cast(
            dict[str, object] | None,
            stored_metadata.get("skill_snapshot"),
        )
        stored_snapshot_hash = (
            stored_snapshot_payload.get("snapshot_hash")
            if isinstance(stored_snapshot_payload, dict)
            else None
        )
        if (
            checkpoint_snapshot_hash is not None
            and stored_snapshot_hash is not None
            and checkpoint_snapshot_hash != stored_snapshot_hash
        ):
            return None
        prompt = checkpoint_payload.get("prompt")
        session_metadata = checkpoint_payload.get("session_metadata")
        raw_tool_results = checkpoint_payload.get("tool_results")
        if not isinstance(prompt, str):
            raise ValueError("persisted approval resume checkpoint prompt must be a string")
        if not isinstance(session_metadata, dict):
            raise ValueError(
                "persisted approval resume checkpoint session_metadata must be an object"
            )
        if cast(dict[str, object], session_metadata) != stored_metadata:
            return None
        if not isinstance(raw_tool_results, list):
            raise ValueError("persisted approval resume checkpoint tool_results must be a list")
        return ApprovalResumeCheckpointState(
            prompt=prompt,
            session_metadata=cast(dict[str, object], session_metadata),
            tool_results=self.tool_results_from_checkpoint(cast(list[object], raw_tool_results)),
        )

    def question_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingQuestion,
        stored_metadata: dict[str, object],
    ) -> ApprovalResumeCheckpointState | None:
        checkpoint_envelope = self.validated_resume_checkpoint_envelope(
            checkpoint=checkpoint,
            expected_kind="question_wait",
        )
        if checkpoint_envelope is None:
            return None
        checkpoint_payload = checkpoint_envelope.payload
        if checkpoint_payload.get("pending_question_request_id") != pending.request_id:
            raise ValueError(
                "persisted question resume checkpoint request id does not match pending question"
            )
        prompt = checkpoint_payload.get("prompt")
        session_metadata = checkpoint_payload.get("session_metadata")
        raw_tool_results = checkpoint_payload.get("tool_results")
        if not isinstance(prompt, str):
            raise ValueError("persisted question resume checkpoint prompt must be a string")
        if not isinstance(session_metadata, dict):
            raise ValueError(
                "persisted question resume checkpoint session_metadata must be an object"
            )
        if cast(dict[str, object], session_metadata) != stored_metadata:
            return None
        if not isinstance(raw_tool_results, list):
            raise ValueError("persisted question resume checkpoint tool_results must be a list")
        return ApprovalResumeCheckpointState(
            prompt=prompt,
            session_metadata=cast(dict[str, object], session_metadata),
            tool_results=self.tool_results_from_checkpoint(cast(list[object], raw_tool_results)),
        )

    @staticmethod
    def validated_resume_checkpoint_envelope(
        *, checkpoint: dict[str, object] | None, expected_kind: str
    ) -> PersistedResumeCheckpointEnvelope | None:
        if checkpoint is None:
            return None
        kind = checkpoint.get("kind")
        if not isinstance(kind, str):
            raise ValueError("persisted resume checkpoint kind must be a string")
        if kind != expected_kind:
            raise ValueError(
                f"persisted resume checkpoint kind mismatch: "
                f"expected {expected_kind!r}, got {kind!r}"
            )
        version = checkpoint.get("version")
        if version != 1:
            raise ValueError(
                f"persisted resume checkpoint version mismatch: expected 1, got {version!r}"
            )
        return PersistedResumeCheckpointEnvelope(kind=kind, version=1, payload=checkpoint)

    def load_resume_checkpoint(self, *, session_id: str) -> dict[str, object] | None:
        load_checkpoint = getattr(self._runtime._session_store, "load_resume_checkpoint", None)
        if load_checkpoint is None:
            return None
        return cast(
            dict[str, object] | None,
            load_checkpoint(workspace=self._runtime._workspace, session_id=session_id),
        )

    @staticmethod
    def tool_results_from_checkpoint(raw_tool_results: list[object]) -> tuple[ToolResult, ...]:
        parsed: list[ToolResult] = []
        for raw_tool_result in raw_tool_results:
            if not isinstance(raw_tool_result, dict):
                raise ValueError("persisted resume checkpoint tool_results must contain objects")
            payload = cast(dict[str, object], raw_tool_result)
            tool_name = payload.get("tool_name")
            status = payload.get("status")
            data = payload.get("data")
            content = payload.get("content")
            error = payload.get("error")
            if (
                not isinstance(tool_name, str)
                or status not in ("ok", "error")
                or not isinstance(data, dict)
            ):
                raise ValueError("persisted resume checkpoint tool_results are malformed")
            if content is not None and not isinstance(content, str):
                raise ValueError(
                    "persisted resume checkpoint tool result content must be a string or null"
                )
            if error is not None and not isinstance(error, str):
                raise ValueError(
                    "persisted resume checkpoint tool result error must be a string or null"
                )
            tool_status: ToolResultStatus = status
            parsed.append(
                ToolResult(
                    tool_name=tool_name,
                    content=content,
                    status=tool_status,
                    data=sanitize_tool_result_data(cast(dict[str, object], data)),
                    error=error,
                )
            )
        return tuple(parsed)

    def _load_pending_approval_context(
        self,
        *,
        session_id: str,
        approval_request_id: str,
    ) -> tuple[Any, PendingApproval, dict[str, object] | None]:
        runtime = self._runtime
        stored_response = runtime._session_store.load_session(
            workspace=runtime._workspace,
            session_id=session_id,
        )
        runtime._validate_session_workspace(stored_response.session, session_id=session_id)
        pending = runtime._session_store.load_pending_approval(
            workspace=runtime._workspace,
            session_id=session_id,
        )
        checkpoint = self.load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")
        runtime._validate_pending_approval_matches_recorded_request(
            stored=stored_response,
            pending=pending,
            checkpoint=checkpoint,
        )
        return stored_response, pending, checkpoint

    def _load_pending_question_context(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> tuple[Any, PendingQuestion, dict[str, object] | None, tuple[QuestionResponse, ...]]:
        runtime = self._runtime
        stored_response = runtime._session_store.load_session(
            workspace=runtime._workspace,
            session_id=session_id,
        )
        runtime._validate_session_workspace(stored_response.session, session_id=session_id)
        pending = runtime._session_store.load_pending_question(
            workspace=runtime._workspace,
            session_id=session_id,
        )
        checkpoint = self.load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise NoPendingQuestionError(f"no pending question for session: {session_id}")
        if pending.request_id != question_request_id:
            raise ValueError("question request id does not match pending session question")
        runtime._validate_pending_question_matches_recorded_request(
            stored=stored_response,
            pending=pending,
            checkpoint=checkpoint,
        )
        normalized_responses = QuestionTool.validate_responses(pending.prompts, responses)
        return stored_response, pending, checkpoint, normalized_responses

    def _resume_prompt_and_tool_results_from_stored_events(
        self,
        stored_events: tuple[EventEnvelope, ...],
    ) -> tuple[str, list[ToolResult]]:
        prompt = self._runtime._prompt_from_events(stored_events)
        tool_results: list[ToolResult] = []
        for event in stored_events:
            if event.event_type != "runtime.tool_completed":
                continue
            error_value = event.payload.get("error")
            raw_content = event.payload.get("content")
            is_err = error_value is not None
            tool_results.append(
                ToolResult(
                    tool_name=str(event.payload.get("tool", "unknown")),
                    content=str(raw_content) if raw_content is not None and not is_err else None,
                    status="error" if is_err else "ok",
                    data=sanitize_tool_result_data(event.payload),
                    error=str(error_value) if is_err else None,
                )
            )
        return prompt, tool_results
