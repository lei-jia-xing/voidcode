from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from ..graph.contracts import GraphRunRequest
from ..provider.protocol import ProviderAbortSignal
from ..tools.contracts import ToolCall, ToolResult, ToolResultStatus
from ..tools.output import sanitize_tool_result_data
from ..tools.question import QuestionTool
from . import chunk_builders
from .acp import (
    disconnect_acp_for_session_state,
    emit_acp_events,
    emit_current_acp_drain,
    finalize_run_acp,
)
from .config import RuntimeConfig, serialize_runtime_agent_config
from .context_continuity import verified_checkpoint_session_metadata
from .contracts import (
    NoPendingQuestionError,
    RuntimeRequest,
    RuntimeRequestError,
    RuntimeResponse,
    RuntimeStreamChunk,
)
from .event_envelopes import resequence_event
from .events import RUNTIME_QUESTION_ANSWERED, RUNTIME_SKILLS_BINDING_MISMATCH, EventEnvelope
from .execution_seams import select_graph_for_effective_config
from .hook_runtime import (
    HOOK_RECURSION_ENV_VAR,
    hook_execution_policy_from_metadata,
    run_lifecycle_hooks_for_session,
)
from .mcp import release_mcp_session_events
from .permission import PendingApproval, PermissionResolution
from .permission_policy import (
    permission_policy_for_session,
    request_event_and_resolution_state,
)
from .provider_execution_metadata import provider_attempt_from_metadata
from .provider_metadata import validate_reasoning_effort_capability
from .question import PendingQuestion, QuestionResponse
from .session import (
    SessionState,
    reload_persisted_session,
    validate_session_workspace,
)
from .session_metadata_helpers import (
    continuity_state_from_session_metadata,
    resume_waiting_reason,
    session_model_identity,
    session_with_context_window_payload_metadata,
    session_with_current_acp_metadata,
    session_with_plan_state,
    session_with_run_id,
    waiting_reason_from_session,
)
from .skills import skill_binding_mismatch_payload, skill_prompt_context_for_assembly
from .storage import SessionStore

if TYPE_CHECKING:
    from .acp import AcpAdapter
    from .background_tasks import RuntimeBackgroundTaskSupervisor
    from .mcp import McpManager
    from .permission import PermissionPolicy
    from .run_loop import RuntimeRunLoopCoordinator
    from .runtime_surface import RuntimeSurface


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


def validate_pending_approval_matches_recorded_request(
    *,
    stored: RuntimeResponse,
    pending: PendingApproval,
    checkpoint: dict[str, object] | None,
) -> None:
    request_event, resolved = request_event_and_resolution_state(
        stored.events,
        request_kind="approval",
        request_id=pending.request_id,
    )
    if resolved:
        raise ValueError("approval request was already resolved; stale approval replay is not allowed")
    if request_event is None:
        if checkpoint is None:
            raise ValueError("persisted pending approval has no matching approval request event")
        if checkpoint.get("pending_approval_request_id") != pending.request_id:
            raise ValueError("persisted approval resume checkpoint request id does not match pending approval")
        if checkpoint.get("pending_approval_tool_name") != pending.tool_name or checkpoint.get("pending_approval_arguments") != pending.arguments:
            raise ValueError("persisted pending approval no longer matches the recorded approval request payload")
        if checkpoint.get("pending_approval_owner_session_id") != pending.owner_session_id:
            raise ValueError("persisted pending approval owner_session_id does not match the recorded approval request")
        if checkpoint.get("pending_approval_owner_parent_session_id") != pending.owner_parent_session_id:
            raise ValueError("persisted pending approval owner_parent_session_id does not match the recorded approval request")
        if checkpoint.get("pending_approval_delegated_task_id") != pending.delegated_task_id:
            raise ValueError("persisted pending approval delegated_task_id does not match the recorded approval request")
        checkpoint_sequence = checkpoint.get("pending_approval_request_event_sequence")
        if pending.request_event_sequence is not None and checkpoint_sequence is not None and checkpoint_sequence != pending.request_event_sequence:
            raise ValueError("persisted pending approval sequence does not match the recorded approval request")
        return
    if pending.request_event_sequence is not None and request_event.sequence != pending.request_event_sequence:
        raise ValueError("persisted pending approval sequence does not match the recorded approval request")
    payload = request_event.payload
    if payload.get("tool") != pending.tool_name or payload.get("arguments") != pending.arguments:
        raise ValueError("persisted pending approval no longer matches the recorded approval request payload")
    if payload.get("owner_session_id") != pending.owner_session_id:
        raise ValueError("persisted pending approval owner_session_id does not match the recorded approval request")
    if payload.get("owner_parent_session_id") != pending.owner_parent_session_id:
        raise ValueError("persisted pending approval owner_parent_session_id does not match the recorded approval request")
    if payload.get("delegated_task_id") != pending.delegated_task_id:
        raise ValueError("persisted pending approval delegated_task_id does not match the recorded approval request")


def validate_pending_question_matches_recorded_request(
    *,
    stored: RuntimeResponse,
    pending: PendingQuestion,
    checkpoint: dict[str, object] | None,
) -> None:
    request_event, resolved = request_event_and_resolution_state(
        stored.events,
        request_kind="question",
        request_id=pending.request_id,
    )
    if resolved:
        raise ValueError("question request was already answered; stale question replay is not allowed")
    expected_questions = [
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
        for prompt in pending.prompts
    ]
    if request_event is None:
        if checkpoint is None:
            raise ValueError("persisted pending question has no matching question request event")
        if checkpoint.get("pending_question_request_id") != pending.request_id:
            raise ValueError("persisted question resume checkpoint request id does not match pending question")
        if checkpoint.get("pending_question_tool_name") != pending.tool_name:
            raise ValueError("persisted pending question tool does not match the recorded question request")
        if checkpoint.get("pending_question_prompts") != expected_questions:
            raise ValueError("persisted pending question no longer matches the recorded question request payload")
        return
    payload = request_event.payload
    if payload.get("tool") != pending.tool_name:
        raise ValueError("persisted pending question tool does not match the recorded question request")
    if payload.get("questions") != expected_questions:
        raise ValueError("persisted pending question no longer matches the recorded question request payload")


class RuntimeResumeCoordinator:
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
        background_task_supervisor: RuntimeBackgroundTaskSupervisor,
        run_loop_coordinator: RuntimeRunLoopCoordinator,
    ) -> None:
        self._surface = surface
        self._session_store = session_store
        self._workspace = workspace
        self._config = config
        self._permission_policy = permission_policy
        self._acp_adapter = acp_adapter
        self._mcp_manager = mcp_manager
        self._background_task_supervisor = background_task_supervisor
        self._run_loop_coordinator = run_loop_coordinator

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
            self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)

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
        stored_response, pending, checkpoint, normalized_responses = self._load_pending_question_context(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
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
            self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)

    def answer_pending_question_response(
        self,
        *,
        session_id: str,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> tuple[tuple[EventEnvelope, ...], RuntimeResponse]:
        stored_response, pending, checkpoint, normalized_responses = self._load_pending_question_context(
            session_id=session_id,
            question_request_id=question_request_id,
            responses=responses,
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
        runtime = self._surface
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
        prompt = checkpoint_state.prompt
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=checkpoint_state.session_metadata,
        )
        tool_results: list[ToolResult] = list(checkpoint_state.tool_results)
        session = session_with_run_id(session, run_id=run_id)
        validate_session_workspace(session, session_id=stored.session.session.id, workspace=self._workspace)
        tool_results.append(question_answer_result)

        sequence = max_stored_sequence + 1
        answered_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type=RUNTIME_QUESTION_ANSWERED,
            source="runtime",
            payload={
                "request_id": pending.request_id,
                "responses": [{"header": response.header, "answers": list(response.answers)} for response in responses],
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=answered_event)
        sequence += 1
        loop_events = [answered_event]
        model, provider = session_model_identity(session.metadata)
        identity_payload: dict[str, str] = {}
        if model is not None:
            identity_payload["model"] = model
        if provider is not None:
            identity_payload["provider"] = provider
        tool_completed_event = EventEnvelope(
            session_id=session.session.id,
            sequence=sequence,
            event_type="runtime.tool_completed",
            source="tool",
            payload={
                **identity_payload,
                "tool": question_answer_result.tool_name,
                "status": question_answer_result.status,
                "content": question_answer_result.content,
                "error": question_answer_result.error,
                **question_answer_result.data,
            },
        )
        yield RuntimeStreamChunk(kind="event", session=session, event=tool_completed_event)
        loop_events.append(tool_completed_event)

        effective_config = runtime.effective_runtime_config_from_metadata(session.metadata)
        try:
            validate_reasoning_effort_capability(effective_config)
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        tool_registry = runtime.tool_registry_for_effective_config(effective_config)
        skill_registry = runtime.skill_registry_for_effective_config(effective_config)
        resumed_skill_snapshot = runtime.build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="resume",
        )
        assembled_context = runtime.assemble_provider_context(
            prompt=prompt,
            tool_results=tuple(tool_results),
            session_metadata=session.metadata,
            skill_prompt_context=skill_prompt_context_for_assembly(
                skill_registry=skill_registry,
                applied_context=resumed_skill_snapshot.skill_prompt_context,
                selected_skill_names=resumed_skill_snapshot.selected_skill_names,
            ),
        )
        session = session_with_context_window_payload_metadata(
            session,
            dict(assembled_context.metadata),
        )
        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=runtime.provider_tool_definitions(tool_registry, effective_config),
            context_window=runtime.prepare_provider_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            assembled_context=assembled_context,
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                "resume": True,
                "resume_kind": "approval",
                "approval_request_id": pending.request_id,
                "provider_attempt": (
                    session.metadata.get("provider_attempt", 0) if isinstance(session.metadata.get("provider_attempt", 0), int) else 0
                ),
                **(
                    {"reasoning_effort": effective_config.reasoning_effort}
                    if effective_config.reasoning_effort is not None and "reasoning_effort" not in session.metadata
                    else {}
                ),
            },
            abort_signal=abort_signal,
        )
        graph = runtime.graph_for_session_metadata(session.metadata)
        output: str | None = None
        final_session = session
        last_sequence = sequence
        try:
            for chunk in self._run_loop_coordinator.execute_graph_loop(
                graph=graph,
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                graph_request=graph_request,
                tool_results=tool_results,
                permission_policy=permission_policy_for_session(base_policy=self._permission_policy, metadata=session.metadata),
                preserved_continuity_state=continuity_state_from_session_metadata(session.metadata),
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
                _ = self._persist_resumed_response(
                    stored_response=stored,
                    prompt=prompt,
                    response=response,
                )
                return
            raise

        if final_session.status == "waiting":
            final_session = disconnect_acp_for_session_state(self._acp_adapter, final_session)
            waiting_response = RuntimeResponse(
                session=final_session,
                events=stored.events + tuple(loop_events),
                output=output,
            )
            idle_reason = resume_waiting_reason(waiting_response)
            idle_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=final_session,
                sequence=last_sequence,
                surface="session_idle",
                payload={"reason": idle_reason, "resume": True},
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(final_session.metadata),
            )
            for hook_chunk in idle_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                loop_events.append(hook_event)
                yield hook_chunk
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=final_session,
                    sequence=idle_hook_outcome.last_sequence,
                    surface="session_idle",
                    error=idle_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    failed_event = cast(EventEnvelope, failed_chunk.event)
                    loop_events.append(failed_event)
                    final_session = failed_chunk.session
                    yield failed_chunk
        else:
            final_chunks, final_session, final_sequence = finalize_run_acp(
                self._acp_adapter,
                session=final_session,
                sequence=last_sequence,
            )
            final_session = session_with_plan_state(final_session, status="completed")
            for chunk in final_chunks:
                if chunk.event is not None:
                    last_sequence += 1
                    resequenced_event = resequence_event(chunk.event, sequence=last_sequence)
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
            end_hook_sequence = max(last_sequence, final_sequence)
            end_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=final_session,
                sequence=end_hook_sequence,
                surface="session_end",
                payload={"session_status": final_session.status, "resume": True},
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(final_session.metadata),
            )
            for hook_chunk in end_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                loop_events.append(hook_event)
                yield hook_chunk
            if end_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=final_session,
                    sequence=end_hook_outcome.last_sequence,
                    surface="session_end",
                    error=end_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    failed_event = cast(EventEnvelope, failed_chunk.event)
                    loop_events.append(failed_event)
                    final_session = failed_chunk.session
                    yield failed_chunk
            for release_event in release_mcp_session_events(
                self._mcp_manager,
                session_id=final_session.session.id,
                start_sequence=end_hook_outcome.last_sequence + 1,
            ):
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
        _ = self._persist_resumed_response(
            stored_response=stored,
            prompt=prompt,
            response=response,
        )

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
            final_session = reload_persisted_session(self._session_store, self._workspace, session_id=final_session.session.id)
        resolved_session = cast(SessionState, final_session)
        response = RuntimeResponse(
            session=resolved_session,
            events=stored_response.events + tuple(streamed_events),
            output=output,
        )
        return response

    def _persist_resumed_response(
        self,
        *,
        stored_response: Any,
        prompt: str,
        response: RuntimeResponse,
    ) -> RuntimeResponse:
        request = self._resumed_runtime_request(
            stored_response=stored_response,
            prompt=prompt,
        )
        self._surface.persist_response(request=request, response=response)
        return response

    @staticmethod
    def _resumed_runtime_request(*, stored_response: Any, prompt: str) -> RuntimeRequest:
        return RuntimeRequest(
            prompt=prompt,
            session_id=stored_response.session.session.id,
            parent_session_id=stored_response.session.session.parent_id,
            metadata=stored_response.session.metadata,
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
        runtime = self._surface
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
        checkpoint_payload = cast(dict[str, object], checkpoint)
        checkpoint_binding = checkpoint_payload.get("skill_binding_snapshot")
        checkpoint_binding_payload = cast(dict[str, object], checkpoint_binding) if isinstance(checkpoint_binding, dict) else None
        if checkpoint_binding_payload is not None:
            stored_snapshot_payload = cast(
                dict[str, object] | None,
                stored.session.metadata.get("skill_snapshot"),
            )
            stored_binding_payload = (
                cast(dict[str, object], stored_snapshot_payload.get("binding_snapshot"))
                if isinstance(stored_snapshot_payload, dict) and isinstance(stored_snapshot_payload.get("binding_snapshot"), dict)
                else None
            )
            mismatch_payload = skill_binding_mismatch_payload(
                checkpoint_binding_payload,
                stored_binding_payload,
            )
            if cast(bool, mismatch_payload["mismatch"]):
                binding_mismatch_payload = mismatch_payload
        prompt = checkpoint_state.prompt
        session = SessionState(
            session=stored.session.session,
            status="running",
            turn=stored.session.turn,
            metadata=checkpoint_state.session_metadata,
        )
        tool_results: list[ToolResult] = list(checkpoint_state.tool_results)

        session = session_with_run_id(session, run_id=run_id)
        validate_session_workspace(session, session_id=stored.session.session.id, workspace=self._workspace)
        session = session_with_current_acp_metadata(session, self._acp_adapter.current_state())
        effective_config = runtime.effective_runtime_config_from_metadata(session.metadata)
        mcp_state = self._mcp_manager.current_state()
        if mcp_state.configuration.configured_enabled is True and not runtime.should_skip_mcp_startup_for_request(
            request_metadata=session.metadata,
            effective_config=effective_config,
        ):
            mcp_startup_chunks, session, _, mcp_failed_chunk = runtime.refresh_mcp_tools_for_session(
                session=session,
                sequence=max_stored_sequence,
                failure_kind="mcp_startup_failed",
            )
            effective_config = runtime.effective_runtime_config_from_metadata(session.metadata)
        else:
            mcp_startup_chunks = ()
            mcp_failed_chunk = None
            runtime.reset_tool_registry_to_base()
        try:
            validate_reasoning_effort_capability(effective_config)
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        tool_registry = runtime.tool_registry_for_effective_config(effective_config)
        skill_registry = runtime.skill_registry_for_effective_config(effective_config)

        resumed_skill_snapshot = runtime.build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="resume",
        )

        assembled_context = runtime.assemble_provider_context(
            prompt=prompt,
            tool_results=tuple(tool_results),
            session_metadata=session.metadata,
            skill_prompt_context=skill_prompt_context_for_assembly(
                skill_registry=skill_registry,
                applied_context=resumed_skill_snapshot.skill_prompt_context,
                selected_skill_names=resumed_skill_snapshot.selected_skill_names,
            ),
        )
        session = session_with_context_window_payload_metadata(
            session,
            dict(assembled_context.metadata),
        )
        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=runtime.provider_tool_definitions(tool_registry, effective_config),
            context_window=runtime.prepare_provider_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            assembled_context=assembled_context,
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                "provider_attempt": (
                    session.metadata.get("provider_attempt", 0) if isinstance(session.metadata.get("provider_attempt", 0), int) else 0
                ),
                **(
                    {"reasoning_effort": effective_config.reasoning_effort}
                    if effective_config.reasoning_effort is not None and "reasoning_effort" not in session.metadata
                    else {}
                ),
            },
            abort_signal=abort_signal,
        )
        provider_attempt = provider_attempt_from_metadata(graph_request.metadata)
        graph = runtime.graph_for_session_metadata(session.metadata)
        if provider_attempt > 0:
            graph = select_graph_for_effective_config(
                config=runtime.effective_runtime_config_from_metadata(session.metadata),
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
            resequenced_event = resequence_event(cast(EventEnvelope, chunk.event), sequence=emitted_sequence)
            loop_events.append(resequenced_event)
            yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
        if mcp_failed_chunk is not None:
            emitted_sequence += 1
            resequenced_failed = resequence_event(cast(EventEnvelope, mcp_failed_chunk.event), sequence=emitted_sequence)
            response = RuntimeResponse(
                session=mcp_failed_chunk.session,
                events=stored.events + tuple(loop_events) + (resequenced_failed,),
                output=output,
            )
            _ = self._persist_resumed_response(
                stored_response=stored,
                prompt=prompt,
                response=response,
            )
            yield RuntimeStreamChunk(
                kind="event",
                session=mcp_failed_chunk.session,
                event=resequenced_failed,
            )
            return

        deferred_startup_acp_events: tuple[object, ...] = ()
        if self._acp_adapter.current_state().configuration.configured_enabled is True:
            try:
                deferred_startup_acp_events = self._acp_adapter.connect()
            except Exception as exc:
                startup_chunks, session, last_sequence = emit_current_acp_drain(
                    self._acp_adapter,
                    session=session,
                    start_sequence=max_stored_sequence + 1,
                )
                startup_failed_chunk = chunk_builders.failed_chunk(
                    session=session_with_current_acp_metadata(session, self._acp_adapter.current_state()),
                    sequence=last_sequence + 1,
                    error=str(exc),
                    payload={"kind": "acp_startup_failed"},
                )
            else:
                session = session_with_current_acp_metadata(session, self._acp_adapter.current_state())
                startup_chunks = ()
                startup_failed_chunk = None
        else:
            startup_chunks = ()
            startup_failed_chunk = None
        for chunk in startup_chunks:
            emitted_sequence += 1
            resequenced_event = resequence_event(cast(EventEnvelope, chunk.event), sequence=emitted_sequence)
            loop_events.append(resequenced_event)
            yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
        if startup_failed_chunk is not None:
            emitted_sequence += 1
            resequenced_failed = resequence_event(
                cast(EventEnvelope, startup_failed_chunk.event),
                sequence=emitted_sequence,
            )
            response = RuntimeResponse(
                session=startup_failed_chunk.session,
                events=stored.events + tuple(loop_events) + (resequenced_failed,),
                output=output,
            )
            _ = self._persist_resumed_response(
                stored_response=stored,
                prompt=prompt,
                response=response,
            )
            yield RuntimeStreamChunk(
                kind="event",
                session=startup_failed_chunk.session,
                event=resequenced_failed,
            )
            return

        approved_tool_call = ToolCall(
            tool_name=pending.tool_name,
            arguments=dict(pending.arguments),
            tool_call_id=self._recorded_pending_tool_call_id(
                stored_events=stored.events,
                pending=pending,
            ),
        )
        sequence = emitted_sequence
        try:
            for chunk in self._run_loop_coordinator.execute_approved_tool_call(
                tool_registry=tool_registry,
                session=session,
                sequence=sequence,
                tool_call=approved_tool_call,
                pending=pending,
                decision=approval_decision,
                tool_results=tool_results,
                abort_signal=graph_request.abort_signal,
            ):
                session = chunk.session
                if deferred_startup_acp_events and (
                    (chunk.event is not None and chunk.event.event_type in {"runtime.approval_resolved", "runtime.failed"}) or chunk.kind == "output"
                ):
                    startup_chunks, updated_session, _ = emit_acp_events(
                        self._acp_adapter,
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
                    session = chunk.session
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = resequence_event(chunk.event, sequence=emitted_sequence)
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
                if chunk.kind == "output":
                    output = chunk.output
                    yield chunk

            graph_loop_chunks: Iterator[RuntimeStreamChunk]
            resumed_engine = runtime.effective_runtime_config_from_metadata(session.metadata).execution_engine
            if session.status == "failed" or (approval_decision == "deny" and resumed_engine != "provider"):
                graph_loop_chunks = iter(())
            else:
                graph_loop_chunks = self._run_loop_coordinator.execute_graph_loop(
                    graph=graph,
                    tool_registry=tool_registry,
                    session=session,
                    sequence=emitted_sequence,
                    graph_request=graph_request,
                    tool_results=tool_results,
                    permission_policy=permission_policy_for_session(base_policy=self._permission_policy, metadata=session.metadata),
                    preserved_continuity_state=None,
                )

            for chunk in graph_loop_chunks:
                session = chunk.session
                if deferred_startup_acp_events and (
                    (chunk.event is not None and chunk.event.event_type in {"runtime.approval_resolved", "runtime.failed"}) or chunk.kind == "output"
                ):
                    startup_chunks, updated_session, _ = emit_acp_events(
                        self._acp_adapter,
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
                    session = chunk.session
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = resequence_event(chunk.event, sequence=emitted_sequence)
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
                if chunk.kind == "output":
                    output = chunk.output
                    yield chunk
        except Exception:
            if session.status == "failed":
                response = RuntimeResponse(
                    session=session,
                    events=stored.events + tuple(loop_events),
                    output=output,
                )
                _ = self._persist_resumed_response(
                    stored_response=stored,
                    prompt=prompt,
                    response=response,
                )
                return
            raise

        if deferred_startup_acp_events:
            startup_chunks, session, _ = emit_acp_events(
                self._acp_adapter,
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
            session = disconnect_acp_for_session_state(self._acp_adapter, session)
            idle_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=session,
                sequence=last_sequence,
                surface="session_idle",
                payload={
                    "reason": waiting_reason_from_session(session),
                    "resume": True,
                },
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(session.metadata),
            )
            for hook_chunk in idle_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                loop_events.append(hook_event)
                yield hook_chunk
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=session,
                    sequence=idle_hook_outcome.last_sequence,
                    surface="session_idle",
                    error=idle_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    failed_event = cast(EventEnvelope, failed_chunk.event)
                    loop_events.append(failed_event)
                    session = failed_chunk.session
                    yield failed_chunk
        else:
            final_chunks, session, _ = finalize_run_acp(
                self._acp_adapter,
                session=session,
                sequence=last_sequence,
            )
            for chunk in final_chunks:
                if chunk.event is not None:
                    emitted_sequence += 1
                    resequenced_event = resequence_event(chunk.event, sequence=emitted_sequence)
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
            end_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=session,
                sequence=emitted_sequence,
                surface="session_end",
                payload={"session_status": session.status, "resume": True},
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(session.metadata),
            )
            for hook_chunk in end_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                loop_events.append(hook_event)
                yield hook_chunk
            if end_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=session,
                    sequence=end_hook_outcome.last_sequence,
                    surface="session_end",
                    error=end_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    failed_event = cast(EventEnvelope, failed_chunk.event)
                    loop_events.append(failed_event)
                    session = failed_chunk.session
                    yield failed_chunk
            for release_event in release_mcp_session_events(
                self._mcp_manager,
                session_id=session.session.id,
                start_sequence=end_hook_outcome.last_sequence + 1,
            ):
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
        _ = self._persist_resumed_response(
            stored_response=stored,
            prompt=prompt,
            response=response,
        )

    def approval_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingApproval,
        stored_metadata: dict[str, object],
    ) -> ApprovalResumeCheckpointState:
        checkpoint_envelope = self.validated_resume_checkpoint_envelope(
            checkpoint=checkpoint,
            expected_kind="approval_wait",
        )
        checkpoint_payload = checkpoint_envelope.payload
        if checkpoint_payload.get("pending_approval_request_id") != pending.request_id:
            raise ValueError("persisted approval resume checkpoint request id does not match pending approval")
        checkpoint_snapshot_hash = checkpoint_payload.get("skill_snapshot_hash")
        stored_snapshot_payload = cast(
            dict[str, object] | None,
            stored_metadata.get("skill_snapshot"),
        )
        stored_snapshot_hash = stored_snapshot_payload.get("snapshot_hash") if isinstance(stored_snapshot_payload, dict) else None
        if checkpoint_snapshot_hash is not None and stored_snapshot_hash is not None and checkpoint_snapshot_hash != stored_snapshot_hash:
            raise ValueError("persisted approval resume checkpoint skill snapshot hash does not match session")
        prompt = checkpoint_payload.get("prompt")
        session_metadata = checkpoint_payload.get("session_metadata")
        raw_tool_results = checkpoint_payload.get("tool_results")
        if not isinstance(prompt, str):
            raise ValueError("persisted approval resume checkpoint prompt must be a string")
        if not isinstance(session_metadata, dict):
            raise ValueError("persisted approval resume checkpoint session_metadata must be an object")
        recovered_metadata = verified_checkpoint_session_metadata(
            checkpoint_metadata=cast(dict[str, object], session_metadata),
            stored_metadata=stored_metadata,
        )
        if recovered_metadata is None:
            raise ValueError("persisted approval resume checkpoint session_metadata does not match session")
        if not isinstance(raw_tool_results, list):
            raise ValueError("persisted approval resume checkpoint tool_results must be a list")
        return ApprovalResumeCheckpointState(
            prompt=prompt,
            session_metadata=recovered_metadata,
            tool_results=self.tool_results_from_checkpoint(cast(list[object], raw_tool_results)),
        )

    def question_resume_state_from_checkpoint(
        self,
        *,
        checkpoint: dict[str, object] | None,
        pending: PendingQuestion,
        stored_metadata: dict[str, object],
    ) -> ApprovalResumeCheckpointState:
        checkpoint_envelope = self.validated_resume_checkpoint_envelope(
            checkpoint=checkpoint,
            expected_kind="question_wait",
        )
        checkpoint_payload = checkpoint_envelope.payload
        if checkpoint_payload.get("pending_question_request_id") != pending.request_id:
            raise ValueError("persisted question resume checkpoint request id does not match pending question")
        prompt = checkpoint_payload.get("prompt")
        session_metadata = checkpoint_payload.get("session_metadata")
        raw_tool_results = checkpoint_payload.get("tool_results")
        if not isinstance(prompt, str):
            raise ValueError("persisted question resume checkpoint prompt must be a string")
        if not isinstance(session_metadata, dict):
            raise ValueError("persisted question resume checkpoint session_metadata must be an object")
        recovered_metadata = verified_checkpoint_session_metadata(
            checkpoint_metadata=cast(dict[str, object], session_metadata),
            stored_metadata=stored_metadata,
        )
        if recovered_metadata is None:
            raise ValueError("persisted question resume checkpoint session_metadata does not match session")
        if not isinstance(raw_tool_results, list):
            raise ValueError("persisted question resume checkpoint tool_results must be a list")
        return ApprovalResumeCheckpointState(
            prompt=prompt,
            session_metadata=recovered_metadata,
            tool_results=self.tool_results_from_checkpoint(cast(list[object], raw_tool_results)),
        )

    @staticmethod
    def validated_resume_checkpoint_envelope(*, checkpoint: dict[str, object] | None, expected_kind: str) -> PersistedResumeCheckpointEnvelope:
        if checkpoint is None:
            raise ValueError("persisted resume checkpoint is required")
        kind = checkpoint.get("kind")
        if not isinstance(kind, str):
            raise ValueError("persisted resume checkpoint kind must be a string")
        if kind != expected_kind:
            raise ValueError(f"persisted resume checkpoint kind mismatch: expected {expected_kind!r}, got {kind!r}")
        version = checkpoint.get("version")
        if version != 1:
            raise ValueError(f"persisted resume checkpoint version mismatch: expected 1, got {version!r}")
        return PersistedResumeCheckpointEnvelope(kind=kind, version=1, payload=checkpoint)

    def load_resume_checkpoint(self, *, session_id: str) -> dict[str, object] | None:
        return self._session_store.load_resume_checkpoint(
            workspace=self._workspace,
            session_id=session_id,
        )

    def resume_provider_failure_response(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        finalize_background_task: bool = False,
    ) -> RuntimeResponse:
        output: str | None = None
        final_session: SessionState | None = None
        for chunk in self.resume_provider_failure_stream(
            session_id=session_id,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.kind == "output":
                output = chunk.output
        stored = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        response = RuntimeResponse(
            session=final_session or stored.session,
            events=stored.events,
            output=output if output is not None else stored.output,
        )
        if finalize_background_task:
            self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)
        return response

    def resume_interrupted_response(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        finalize_background_task: bool = False,
    ) -> RuntimeResponse:
        output: str | None = None
        final_session: SessionState | None = None
        for chunk in self.resume_interrupted_stream(
            session_id=session_id,
            checkpoint=checkpoint,
        ):
            final_session = chunk.session
            if chunk.kind == "output":
                output = chunk.output
        stored = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        response = RuntimeResponse(
            session=final_session or stored.session,
            events=stored.events,
            output=output if output is not None else stored.output,
        )
        if finalize_background_task:
            self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)
        return response

    def resume_provider_failure_stream(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_checkpoint_stream(
            session_id=session_id,
            checkpoint=checkpoint,
            expected_kind="provider_failure_retryable",
            resume_kind=None,
            provider_failure_resume=True,
            run_id=run_id,
            abort_signal=abort_signal,
            finalize_background_task=finalize_background_task,
        )

    def resume_interrupted_stream(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        yield from self._resume_checkpoint_stream(
            session_id=session_id,
            checkpoint=checkpoint,
            expected_kind="interrupted",
            resume_kind="interrupted",
            provider_failure_resume=False,
            truncate_tail=True,
            run_id=run_id,
            abort_signal=abort_signal,
            finalize_background_task=finalize_background_task,
        )

    def _resume_checkpoint_stream(
        self,
        *,
        session_id: str,
        checkpoint: dict[str, object],
        expected_kind: str,
        resume_kind: str | None,
        provider_failure_resume: bool,
        truncate_tail: bool = False,
        run_id: str | None = None,
        abort_signal: ProviderAbortSignal | None = None,
        finalize_background_task: bool = False,
    ) -> Iterator[RuntimeStreamChunk]:
        runtime = self._surface
        checkpoint_envelope = self.validated_resume_checkpoint_envelope(
            checkpoint=checkpoint,
            expected_kind=expected_kind,
        )
        payload = checkpoint_envelope.payload
        prompt = payload.get("prompt")
        session_metadata = payload.get("session_metadata")
        raw_tool_results = payload.get("tool_results")
        if not isinstance(prompt, str):
            raise ValueError("persisted resume checkpoint prompt must be a string")
        if not isinstance(session_metadata, dict):
            raise ValueError("persisted resume checkpoint session_metadata must be an object")
        if not isinstance(raw_tool_results, list):
            raise ValueError("persisted resume checkpoint tool_results must be a list")
        checkpoint_last_sequence: int | None = None
        if truncate_tail:
            raw_last_sequence = payload.get("last_event_sequence")
            if not isinstance(raw_last_sequence, int):
                raise ValueError("persisted interrupted resume checkpoint last_event_sequence must be an integer")
            checkpoint_last_sequence = raw_last_sequence
            self._session_store.truncate_session_events_after(
                workspace=self._workspace,
                session_id=session_id,
                sequence=checkpoint_last_sequence,
            )
        stored = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(stored.session, session_id=session_id, workspace=self._workspace)
        tool_results = list(self.tool_results_from_checkpoint(cast(list[object], raw_tool_results)))
        replayed_conversation_segments = runtime.replayed_conversation_segments_for_existing_session(
            stored=stored,
            parent_session_id=stored.session.session.parent_id,
            current_prompt=prompt,
        )
        session = session_with_run_id(
            SessionState(
                session=stored.session.session,
                status="running",
                turn=stored.session.turn,
                metadata=cast(dict[str, object], session_metadata),
            ),
            run_id=run_id,
        )
        validate_session_workspace(session, session_id=session_id, workspace=self._workspace)
        session = session_with_current_acp_metadata(session, self._acp_adapter.current_state())
        effective_config = runtime.effective_runtime_config_from_metadata(session.metadata)
        try:
            validate_reasoning_effort_capability(effective_config)
        except ValueError as exc:
            raise RuntimeRequestError(str(exc)) from exc
        tool_registry = runtime.tool_registry_for_effective_config(effective_config)
        skill_registry = runtime.skill_registry_for_effective_config(effective_config)
        resumed_skill_snapshot = runtime.build_skill_snapshot(
            skill_registry,
            metadata=session.metadata,
            agent=effective_config.agent,
            source="resume",
        )
        assembled_context = runtime.assemble_provider_context(
            prompt=prompt,
            tool_results=tuple(tool_results),
            session_metadata=session.metadata,
            skill_prompt_context=skill_prompt_context_for_assembly(
                skill_registry=skill_registry,
                applied_context=resumed_skill_snapshot.skill_prompt_context,
                selected_skill_names=resumed_skill_snapshot.selected_skill_names,
            ),
            replayed_conversation_segments=replayed_conversation_segments,
        )
        session = session_with_context_window_payload_metadata(
            session,
            dict(assembled_context.metadata),
        )
        graph_request = GraphRunRequest(
            session=session,
            prompt=prompt,
            available_tools=runtime.provider_tool_definitions(tool_registry, effective_config),
            context_window=runtime.prepare_provider_context_window(
                prompt=prompt,
                tool_results=tuple(tool_results),
                session_metadata=session.metadata,
            ),
            assembled_context=assembled_context,
            metadata={
                **session.metadata,
                "agent_preset": serialize_runtime_agent_config(effective_config.agent),
                **({"resume_kind": resume_kind} if resume_kind is not None else {}),
                "provider_attempt": (
                    session.metadata.get("provider_attempt", 0) if isinstance(session.metadata.get("provider_attempt", 0), int) else 0
                ),
                "provider_stream": True,
                **({"provider_failure_resume": True} if provider_failure_resume else {}),
                **(
                    {"reasoning_effort": effective_config.reasoning_effort}
                    if effective_config.reasoning_effort is not None and "reasoning_effort" not in session.metadata
                    else {}
                ),
            },
            abort_signal=abort_signal,
        )
        graph = runtime.graph_for_session_metadata(session.metadata)
        if provider_failure_resume:
            provider_attempt = provider_attempt_from_metadata(graph_request.metadata)
            if provider_attempt > 0:
                graph = select_graph_for_effective_config(
                    config=effective_config,
                    provider_attempt=provider_attempt,
                ).graph
        max_stored_sequence = (
            checkpoint_last_sequence if checkpoint_last_sequence is not None else (stored.events[-1].sequence if stored.events else 0)
        )
        loop_events: list[EventEnvelope] = []
        output: str | None = None
        final_session = session
        last_sequence = max_stored_sequence
        # The stored row was sealed terminal (completed/failed) by the terminal
        # seal-writer ``save_run``; the run loop appends events incrementally
        # via ``append_session_events``, which rejects non-lifecycle events on a
        # sealed row. Transition it back to ``interrupted`` (the same un-seal the
        # fresh-run path performs) so the resumed loop can append.
        self._session_store.save_interrupted_checkpoint(
            workspace=self._workspace,
            session_id=session_id,
            prompt=prompt,
            session_metadata=cast(dict[str, object], session_metadata),
            tool_results=tuple(cast(dict[str, object], result) for result in raw_tool_results),
            last_event_sequence=max_stored_sequence,
            output=None,
            create_if_missing=False,
            parent_session_id=session.session.parent_id,
        )
        try:
            for chunk in self._run_loop_coordinator.execute_graph_loop(
                graph=graph,
                tool_registry=tool_registry,
                session=session,
                sequence=max_stored_sequence,
                graph_request=graph_request,
                tool_results=tool_results,
                permission_policy=permission_policy_for_session(base_policy=self._permission_policy, metadata=session.metadata),
                preserved_continuity_state=continuity_state_from_session_metadata(session.metadata),
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
                response = self._persist_resumed_response(
                    stored_response=stored,
                    prompt=prompt,
                    response=response,
                )
                if finalize_background_task:
                    self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)
                return
            raise

        if final_session.status == "waiting":
            final_session = disconnect_acp_for_session_state(self._acp_adapter, final_session)
            idle_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=final_session,
                sequence=last_sequence,
                surface="session_idle",
                payload={
                    "reason": waiting_reason_from_session(final_session),
                    "resume": True,
                },
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(final_session.metadata),
            )
            for hook_chunk in idle_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                loop_events.append(hook_event)
                yield hook_chunk
            if idle_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=final_session,
                    sequence=idle_hook_outcome.last_sequence,
                    surface="session_idle",
                    error=idle_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    failed_event = cast(EventEnvelope, failed_chunk.event)
                    loop_events.append(failed_event)
                    final_session = failed_chunk.session
                    yield failed_chunk
        else:
            final_chunks, final_session, final_sequence = finalize_run_acp(
                self._acp_adapter,
                session=final_session,
                sequence=last_sequence,
            )
            for chunk in final_chunks:
                if chunk.event is not None:
                    last_sequence += 1
                    resequenced_event = resequence_event(
                        chunk.event,
                        sequence=last_sequence,
                    )
                    loop_events.append(resequenced_event)
                    yield RuntimeStreamChunk(kind="event", session=chunk.session, event=resequenced_event)
            end_hook_outcome = run_lifecycle_hooks_for_session(
                hooks=self._config.hooks,
                workspace=self._workspace,
                session=final_session,
                sequence=max(last_sequence, final_sequence),
                surface="session_end",
                payload={"session_status": final_session.status, "resume": True},
                recursion_env_var=HOOK_RECURSION_ENV_VAR,
                policy=hook_execution_policy_from_metadata(final_session.metadata),
            )
            for hook_chunk in end_hook_outcome.chunks:
                hook_event = cast(EventEnvelope, hook_chunk.event)
                loop_events.append(hook_event)
                yield hook_chunk
            if end_hook_outcome.failed_error is not None:
                failed_chunk = chunk_builders.lifecycle_hook_failure_chunk(
                    session=final_session,
                    sequence=end_hook_outcome.last_sequence,
                    surface="session_end",
                    error=end_hook_outcome.failed_error,
                    hooks=self._config.hooks,
                )
                if failed_chunk is not None:
                    failed_event = cast(EventEnvelope, failed_chunk.event)
                    loop_events.append(failed_event)
                    final_session = failed_chunk.session
                    yield failed_chunk
            release_events = release_mcp_session_events(
                self._mcp_manager,
                session_id=final_session.session.id,
                start_sequence=end_hook_outcome.last_sequence + 1,
            )
            loop_events.extend(release_events)
            for event in release_events:
                yield RuntimeStreamChunk(kind="event", session=final_session, event=event)

        response = RuntimeResponse(
            session=final_session,
            events=stored.events + tuple(loop_events),
            output=output,
        )
        response = self._persist_resumed_response(
            stored_response=stored,
            prompt=prompt,
            response=response,
        )
        if finalize_background_task:
            self._background_task_supervisor.finalize_background_task_from_session_response(session_response=response)

    @staticmethod
    def tool_results_from_checkpoint(raw_tool_results: list[object]) -> tuple[ToolResult, ...]:
        parsed: list[ToolResult] = []
        for raw_tool_result in raw_tool_results:
            if not isinstance(raw_tool_result, dict):
                raise ValueError("persisted resume checkpoint tool_results must contain objects")
            payload = cast(dict[str, object], raw_tool_result)
            tool_name = payload.get("tool_name")
            raw_status = payload.get("status")
            if raw_status == "ok":
                status: ToolResultStatus = "ok"
            elif raw_status == "error":
                status = "error"
            else:
                status = None
            data = payload.get("data")
            content = payload.get("content")
            error = payload.get("error")
            error_kind = payload.get("error_kind")
            error_summary = payload.get("error_summary")
            error_details = payload.get("error_details")
            retry_guidance = payload.get("retry_guidance")
            if not isinstance(tool_name, str) or status is None or not isinstance(data, dict):
                raise ValueError("persisted resume checkpoint tool_results are malformed")
            if content is not None and not isinstance(content, str):
                raise ValueError("persisted resume checkpoint tool result content must be a string or null")
            if error is not None and not isinstance(error, str):
                raise ValueError("persisted resume checkpoint tool result error must be a string or null")
            if error_kind is not None and not isinstance(error_kind, str):
                raise ValueError("persisted resume checkpoint tool result error_kind must be a string or null")
            parsed.append(
                ToolResult(
                    tool_name=tool_name,
                    content=content,
                    status=status,
                    data=sanitize_tool_result_data(cast(dict[str, object], data)),
                    error=error,
                    error_kind=error_kind,
                    error_summary=cast(str | None, error_summary),
                    error_details=(cast(dict[str, object], error_details) if isinstance(error_details, dict) else None),
                    retry_guidance=cast(str | None, retry_guidance),
                    source="checkpoint",
                )
            )
        return tuple(parsed)

    def _load_pending_approval_context(
        self,
        *,
        session_id: str,
        approval_request_id: str,
    ) -> tuple[Any, PendingApproval, dict[str, object] | None]:
        stored_response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(stored_response.session, session_id=session_id, workspace=self._workspace)
        pending = self._session_store.load_pending_approval(
            workspace=self._workspace,
            session_id=session_id,
        )
        checkpoint = self.load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise ValueError(f"no pending approval for session: {session_id}")
        if pending.request_id != approval_request_id:
            raise ValueError("approval request id does not match pending session approval")
        validate_pending_approval_matches_recorded_request(
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
        stored_response = self._session_store.load_session(
            workspace=self._workspace,
            session_id=session_id,
        )
        validate_session_workspace(stored_response.session, session_id=session_id, workspace=self._workspace)
        pending = self._session_store.load_pending_question(
            workspace=self._workspace,
            session_id=session_id,
        )
        checkpoint = self.load_resume_checkpoint(session_id=session_id)
        if pending is None:
            raise NoPendingQuestionError(f"no pending question for session: {session_id}")
        if pending.request_id != question_request_id:
            raise ValueError("question request id does not match pending session question")
        validate_pending_question_matches_recorded_request(
            stored=stored_response,
            pending=pending,
            checkpoint=checkpoint,
        )
        normalized_responses = QuestionTool.validate_responses(pending.prompts, responses)
        return stored_response, pending, checkpoint, normalized_responses

    @staticmethod
    def _recorded_pending_tool_call_id(
        *,
        stored_events: tuple[EventEnvelope, ...],
        pending: PendingApproval,
    ) -> str | None:
        approval_index: int | None = None
        for index, event in enumerate(stored_events):
            if event.event_type == "runtime.approval_requested" and event.payload.get("request_id") == pending.request_id:
                approval_index = index
                break
        if approval_index is None:
            return None

        for event in reversed(stored_events[:approval_index]):
            if event.event_type != "graph.tool_request_created":
                continue
            if event.payload.get("tool") != pending.tool_name:
                continue
            if event.payload.get("arguments") != pending.arguments:
                continue
            tool_call_id = event.payload.get("tool_call_id")
            return tool_call_id if isinstance(tool_call_id, str) else None
        return None
