"""Response-schema drift tests for the runtime HTTP transport.

These tests are what make the OpenAPI document authoritative rather than
aspirational. The transport's handlers render their own JSON
(``JsonResponse`` with sorted keys and an explicit charset) and return
``Response`` objects, so FastAPI never validates a body against
``response_model``: the models only describe. The guarantee therefore has to
come from tests, and it comes from two directions:

* **Document side** — every operation the document publishes must declare a
  success body (a JSON schema for the JSON routes, an ``itemSchema`` for the two
  server-sent-events routes), so a route cannot ship undocumented.
* **Body side** — every documented operation is driven against a fixture runtime
  and the body the transport actually writes is validated against the very model
  the document points at, then dumped back through that model and compared for
  equality. Extra keys fail (``extra="forbid"``), missing required keys fail, and
  the ``null``-vs-absent distinction survives the round trip.

Two fixture runtimes are used so both presence and absence are covered: the rich
one fills every optional field of every contract dataclass, the minimal one
leaves every optional field unset.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import TypeAdapter

from voidcode.runtime.active_session import ActiveRunInterruptResult
from voidcode.runtime.background.models import (
    BackgroundTaskConcurrencyObservability,
    BackgroundTaskObservability,
    BackgroundTaskRef,
    BackgroundTaskRequestSnapshot,
    BackgroundTaskRetryObservability,
    BackgroundTaskState,
    SchemaValidation,
    StoredBackgroundTaskSummary,
)
from voidcode.runtime.background.routing import SubagentRoutingIdentity
from voidcode.runtime.contracts import (
    AgentSummary,
    BackgroundTaskResult,
    CapabilityStatusSnapshot,
    CommandSummary,
    GitStatusSnapshot,
    ProviderInspectResult,
    ProviderModelMetadata,
    ProviderModelsResult,
    ProviderReadinessResult,
    ProviderSummary,
    ProviderValidationResult,
    ReviewChangedFile,
    ReviewFileDiff,
    ReviewTreeNode,
    RuntimeBackgroundTaskStatusSnapshot,
    RuntimeHookPresetSnapshot,
    RuntimeProviderContextDiagnostic,
    RuntimeProviderContextPolicyDecision,
    RuntimeProviderContextSegmentSnapshot,
    RuntimeProviderContextSnapshot,
    RuntimeProviderMessageSnapshot,
    RuntimeResponse,
    RuntimeSessionDebugEvent,
    RuntimeSessionDebugFailure,
    RuntimeSessionDebugPendingApproval,
    RuntimeSessionDebugPendingQuestion,
    RuntimeSessionDebugSnapshot,
    RuntimeSessionDebugToolSummary,
    RuntimeSessionResult,
    RuntimeSessionRevertMarker,
    RuntimeStatusSnapshot,
    RuntimeStreamChunk,
    SessionEventBatch,
    WorkspaceReviewSnapshot,
)
from voidcode.runtime.events import EventEnvelope
from voidcode.runtime.permission import PermissionResolution
from voidcode.runtime.question import QuestionResponse
from voidcode.runtime.session import SessionRef, SessionState, StoredSessionForestEntry, StoredSessionSummary
from voidcode.runtime.transport import http_models
from voidcode.runtime.transport.http_models import ResponseModel
from voidcode.runtime.workspace import WorkspaceRuntimeCoordinator

pytestmark = pytest.mark.usefixtures("_deterministic_engine")


@pytest.fixture
def _deterministic_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    config_module = importlib.import_module("voidcode.runtime.config")
    monkeypatch.setattr(
        config_module,
        "_default_runtime_mcp_config",
        lambda: config_module.RuntimeMcpConfig(enabled=False),
    )
    monkeypatch.setattr(config_module, "_default_runtime_mcp_servers", lambda: {})


_OPENCODE = "opencode-zen"

# ------------------------------------------------------------------ fixture model


def _session_ref(rich: bool) -> SessionRef:
    return SessionRef(id="sess-1", parent_id="parent-1" if rich else None)


def _session_state(rich: bool) -> SessionState:
    metadata: dict[str, object] = {}
    if rich:
        metadata = {
            "runtime_config": {"model": "opencode-zen/gpt-5.4"},
            "todos": {"version": 2, "revision": 1, "phases": [], "summary": {}},
            "runtime_policy": _runtime_policy_snapshot(),
        }
    return SessionState(session=_session_ref(rich), status="completed", turn=3 if rich else 0, metadata=metadata)


def _session_state_json(rich: bool) -> dict[str, object]:
    """The wire shape of :func:`_session_state`, as the transport serializes it."""
    state = _session_state(rich)
    session: dict[str, object] = {"id": state.session.id}
    if state.session.parent_id is not None:
        session["parent_id"] = state.session.parent_id
    return {"session": session, "status": state.status, "turn": state.turn, "metadata": state.metadata}


def _runtime_policy_snapshot() -> dict[str, object]:
    """A persisted ``runtime_policy`` blob as ``runtime.policy`` writes it."""
    return {
        "schema_version": 1,
        "policy_version": "2026.04",
        "mode": "plan",
        "read_only": True,
        "agent_preset": "leader",
        "agent_manifest_id": "leader",
        "intent": {
            "label": "refactor",
            "confidence": 0.5,
            "authoritative": True,
            "matched_rule_ids": ["rule-1"],
        },
        "tool_policy": {"allowed": ["read"], "denied": [{"target": "write", "reason": "read-only"}], "source": "runtime_config"},
        "delegation_policy": {"allowed_presets": ["worker"], "denied": [{"target": "advisor", "reason": "read-only"}]},
        "hook_policy": {"allowed_event_scopes": ["runtime"], "actions": ["notification"], "authoritative": True},
        "prompt_activation": {
            "enabled": True,
            "raw_prompt_stored": True,
            "activated_this_turn": True,
            "activated_refs": ["profile-1"],
        },
        "precedence_trace": [
            {"source": "intent_metadata", "applied": False, "authoritative": False, "label": "refactor", "confidence": 0.5, "reason": None},
        ],
        "diagnostics": {"segments": 1},
    }


def _delegated_event(rich: bool, sequence: int) -> EventEnvelope:
    """A delegated-lifecycle event: the one event kind that adds a wire key."""
    payload: dict[str, object] = {
        "delegation": {
            "parent_session_id": "parent-1" if rich else None,
            "requested_child_session_id": "sess-1",
            "child_session_id": "sess-1",
            "delegated_task_id": "task-1",
            "approval_request_id": "req-1",
            "question_request_id": None,
            "routing": {"mode": "background", "subagent_type": "worker", "description": "do work", "command": "task"},
            "selected_preset": "worker",
            "selected_execution_engine": "provider",
            "lifecycle_status": "completed",
            "approval_blocked": False,
            "result_available": True,
            "cancellation_cause": None,
        },
        "message": {
            "kind": "delegated_lifecycle",
            "status": "completed",
            "summary_output": "done",
            "error": None,
            "approval_blocked": False,
            "result_available": True,
        },
    }
    if rich:
        payload["session_id"] = "sess-1"
        payload["parent_session_id"] = "parent-1"
    return EventEnvelope(
        session_id="sess-1",
        sequence=sequence,
        event_type="runtime.background_task_completed",
        source="runtime",
        payload=payload,
    )


def _plain_event(sequence: int) -> EventEnvelope:
    return EventEnvelope(
        session_id="sess-1",
        sequence=sequence,
        event_type="graph.response_ready",
        source="graph",
        payload={"content": "hi", "usage": {"input_tokens": 5}},
    )


def _events(rich: bool) -> tuple[EventEnvelope, ...]:
    return (_plain_event(1), _delegated_event(rich, 2))


def _background_task_state(rich: bool) -> BackgroundTaskState:
    return BackgroundTaskState(
        task=BackgroundTaskRef(id="task-1"),
        status="completed",
        request=BackgroundTaskRequestSnapshot(
            prompt="delegate this",
            session_id="sess-1",
            parent_session_id="parent-1",
            metadata={"delegation": {"mode": "background", "subagent_type": "worker", "description": "do work", "command": "task"}} if rich else {},
            allocate_session_id=rich,
        ),
        session_id="sess-1",
        approval_request_id="req-1" if rich else None,
        question_request_id=None,
        cancellation_cause="operator_cancel" if rich else None,
        result_available=True,
        error="boom" if rich else None,
        created_at=1,
        updated_at=2,
        started_at=2 if rich else None,
        finished_at=3 if rich else None,
        created_at_unix_ms=1000 if rich else None,
        started_at_unix_ms=2000 if rich else None,
        finished_at_unix_ms=3000 if rich else None,
        cancel_requested_at=2500 if rich else None,
        observability=_task_observability(rich),
        keep_alive=rich,
        steer_prompt="keep going" if rich else None,
        output_schema={"type": "object"} if rich else None,
        schema_mode="strict" if rich else "permissive",
        structured_output={"answer": 1} if rich else None,
        schema_validation=SchemaValidation(schema_source="yield", valid=True, error=None) if rich else None,
    )


def _task_observability(rich: bool) -> BackgroundTaskObservability | None:
    if not rich:
        return None
    return BackgroundTaskObservability(
        waiting_reason="provider_slot",
        terminal_reason="completed",
        queue_position=2,
        concurrency=BackgroundTaskConcurrencyObservability(
            provider=_OPENCODE,
            model="gpt-5.4",
            limit=2,
            limit_source="runtime_config",
            running_provider=1,
            running_model=1,
            running_total=1,
            active_worker_slots=1,
            queued_provider=1,
            queued_model=1,
            queued_total=1,
        ),
        retry=BackgroundTaskRetryObservability(retry_count=1, max_retries=3, backoff_seconds=0.5, next_retry_at=4000),
    )


def _background_task_summary(rich: bool) -> StoredBackgroundTaskSummary:
    return StoredBackgroundTaskSummary(
        task=BackgroundTaskRef(id="task-1"),
        status="completed",
        prompt="delegate this",
        session_id="sess-1" if rich else None,
        error="boom" if rich else None,
        created_at=1,
        updated_at=2,
        created_at_unix_ms=1000 if rich else None,
        observability=_task_observability(rich),
        keep_alive=rich,
        steer_prompt="keep going" if rich else None,
        output_schema={"type": "object"} if rich else None,
        schema_mode="strict" if rich else "permissive",
    )


def _background_task_result(rich: bool) -> BackgroundTaskResult:
    return BackgroundTaskResult(
        task_id="task-1",
        parent_session_id="parent-1",
        child_session_id="sess-1",
        status="completed",
        requested_child_session_id="sess-1",
        delegated_prompt="delegate this" if rich else None,
        routing=SubagentRoutingIdentity(mode="background", subagent_type="worker", description="do work", command="task") if rich else None,
        approval_request_id="req-1" if rich else None,
        question_request_id=None,
        approval_blocked=rich,
        summary_output="done" if rich else None,
        error=None,
        result_available=True,
        cancellation_cause=None,
        duration_seconds=1.5 if rich else None,
        tool_call_count=2,
        observability=_task_observability(rich),
        hook_reminder={"hooks": ["post_run"]} if rich else None,
        structured_output={"answer": 1} if rich else None,
        schema_validation=SchemaValidation(schema_source="yield", valid=True) if rich else None,
    )


def _session_result(rich: bool) -> RuntimeSessionResult:
    return RuntimeSessionResult(
        session=_session_state(rich),
        prompt="delegate this",
        status="completed",
        summary="Completed",
        output="answer" if rich else None,
        error=None,
        transcript=_events(rich),
        last_event_sequence=2,
        revert_marker=RuntimeSessionRevertMarker(sequence=1) if rich else None,
    )


def _provider_context(rich: bool) -> RuntimeProviderContextSnapshot | None:
    if not rich:
        return None
    return RuntimeProviderContextSnapshot(
        provider=_OPENCODE,
        model="gpt-5.4",
        execution_engine="provider",
        segment_count=1,
        message_count=1,
        context_window={"limit": 200000, "used": 20},
        segments=(
            RuntimeProviderContextSegmentSnapshot(
                index=0,
                role="user",
                source="prompt",
                content="hello",
                content_truncated=False,
                tool_call_id=None,
                tool_name=None,
                tool_arguments={},
                metadata={"origin": "prompt"},
            ),
        ),
        provider_messages=(
            RuntimeProviderMessageSnapshot(
                index=0,
                role="user",
                source="prompt",
                content="hello",
                content_truncated=True,
                tool_call_id="call-1",
                tool_calls=({"id": "call-1", "name": "read"},),
            ),
        ),
        diagnostics=(
            RuntimeProviderContextDiagnostic(
                severity="warning",
                code="segment_truncated",
                message="segment truncated",
                source="context",
                segment_indices=(0,),
                suggested_fix="raise the budget",
                details={"limit": 10},
                policy_action="warn",
                policy_blocking=False,
            ),
        ),
        policy_decision=RuntimeProviderContextPolicyDecision(
            mode="warn",
            action="warn",
            blocked=False,
            diagnostic_count=1,
            diagnostic_codes=("segment_truncated",),
            blocking_diagnostic_codes=(),
            message="warned",
        ),
    )


def _debug_snapshot(rich: bool) -> RuntimeSessionDebugSnapshot:
    return RuntimeSessionDebugSnapshot(
        session=_session_state(rich),
        prompt="debug this",
        persisted_status="completed",
        current_status="completed",
        active=False,
        resumable=rich,
        replayable=True,
        terminal=True,
        resume_checkpoint_kind="terminal" if rich else None,
        pending_approval=(
            RuntimeSessionDebugPendingApproval(
                request_id="req-1",
                tool_name="write",
                target_summary="src/app.py",
                reason="write tool",
                policy_mode="ask",
                arguments={"path": "src/app.py"},
                owner_session_id="sess-1",
                owner_parent_session_id="parent-1",
                delegated_task_id="task-1",
            )
            if rich
            else None
        ),
        pending_question=(
            RuntimeSessionDebugPendingQuestion(request_id="req-2", tool_name="question", question_count=2, headers=("Pick one", "And one"))
            if rich
            else None
        ),
        revert_marker=RuntimeSessionRevertMarker(sequence=1) if rich else None,
        last_event_sequence=2,
        last_relevant_event=(
            RuntimeSessionDebugEvent(sequence=2, event_type="graph.response_ready", source="graph", payload={"content": "hi"}) if rich else None
        ),
        last_failure_event=None,
        failure=RuntimeSessionDebugFailure(classification="provider_error", message="boom") if rich else None,
        last_tool=(
            RuntimeSessionDebugToolSummary(
                tool_name="read",
                status="completed",
                summary="read 12 lines",
                arguments={"path": "sample.txt"},
                artifact={"path": "sample.txt"},
                sequence=1,
            )
            if rich
            else None
        ),
        provider_context=_provider_context(rich),
        hook_presets=(
            RuntimeHookPresetSnapshot(
                refs=("hooks/post_run.json",),
                kinds=("post_run",),
                source="workspace",
                count=1,
            )
            if rich
            else None
        ),
        suggested_operator_action="replay",
        operator_guidance="Replay the session.",
    )


def _provider_models(rich: bool) -> ProviderModelsResult:
    metadata = (
        {
            "gpt-5.4": ProviderModelMetadata(
                context_window=200000,
                max_input_tokens=180000,
                max_output_tokens=32000,
                supports_tools=True,
                supports_vision=False,
                supports_streaming=True,
                supports_reasoning=True,
                supports_json_mode=True,
                cost_per_input_token=0.000001,
                cost_per_output_token=0.000002,
                cost_per_cache_read_token=0.0000001,
                cost_per_cache_write_token=0.0000002,
                supports_reasoning_effort=True,
                default_reasoning_effort="medium",
                supported_effort_levels=("low", "medium"),
                supports_reasoning_summary=True,
                supports_thinking_budget=False,
                supports_interleaved_reasoning=False,
                reasoning_visibility="summary",
                modalities_input=("text",),
                modalities_output=("text",),
                model_status="stable",
                tool_feedback_mode="standard",
            ),
            "mystery": ProviderModelMetadata(),
        }
        if rich
        else {}
    )
    return ProviderModelsResult(
        provider=_OPENCODE,
        configured=True,
        models=("gpt-5.4", "mystery") if rich else (),
        model_metadata=metadata,
        source="provider_catalog",
        last_refresh_status="ok",
        last_error=None,
        discovery_mode="dynamic",
    )


def _provider_validation(rich: bool) -> ProviderValidationResult:
    return ProviderValidationResult(
        provider=_OPENCODE,
        configured=True,
        ok=rich,
        status="valid" if rich else "unconfigured",
        message="credentials accepted" if rich else "no credentials",
        source="env",
        last_error=None,
        discovery_mode="static",
    )


def _runtime_status(rich: bool) -> RuntimeStatusSnapshot:
    return RuntimeStatusSnapshot(
        git=GitStatusSnapshot(
            state="git_ready",
            root="/workspace" if rich else None,
            branch="main" if rich else None,
            error=None,
        ),
        lsp=CapabilityStatusSnapshot(state="running", error=None, details={"servers": 1} if rich else {}),
        mcp=CapabilityStatusSnapshot(state="stopped", error=None, details={}),
        acp=CapabilityStatusSnapshot(state="unconfigured", error=None, details={}),
        background_tasks=RuntimeBackgroundTaskStatusSnapshot(
            active_worker_slots=1,
            queued_count=0,
            running_count=1,
            terminal_count=2,
            default_concurrency=4,
            provider_concurrency={_OPENCODE: 2} if rich else {},
            model_concurrency={"gpt-5.4": 1} if rich else {},
            status_counts={"completed": 2} if rich else {},
        ),
    )


def _review_snapshot(rich: bool) -> WorkspaceReviewSnapshot:
    return WorkspaceReviewSnapshot(
        root="/workspace",
        git=GitStatusSnapshot(state="git_ready", root="/workspace", branch="main", error="stale index" if rich else None),
        changed_files=(ReviewChangedFile(path="src/app.py", change_type="modified", old_path="src/old_app.py" if rich else None),),
        tree=(
            ReviewTreeNode(
                path="src",
                name="src",
                kind="directory",
                changed=True,
                children=(ReviewTreeNode(path="src/app.py", name="app.py", kind="file", changed=True),),
            ),
        ),
    )


@dataclass(slots=True)
class _FixtureRuntime:
    """A runtime stub that fills (or leaves unset) every optional contract field."""

    rich: bool

    def __exit__(self, *_: object) -> None:
        return None

    def run_stream(self, request: object) -> Iterator[RuntimeStreamChunk]:
        _ = request
        metadata: dict[str, object] = {"model": "gpt-5.4"} if self.rich else {}
        yield RuntimeStreamChunk(
            kind="event",
            session=_session_state(self.rich),
            event=_plain_event(1),
        )
        yield RuntimeStreamChunk(kind="output", session=_session_state(self.rich), output="answer")
        changed = SessionState(
            session=_session_ref(self.rich),
            status="running",
            turn=4,
            metadata=metadata,
        )
        yield RuntimeStreamChunk(kind="event", session=changed, event=_delegated_event(self.rich, 2))

    def replay_session(self, *, session_id: str) -> RuntimeResponse:
        _ = session_id
        return RuntimeResponse(session=_session_state(self.rich), events=_events(self.rich), output="answer" if self.rich else None)

    def session_events_after(self, *, session_id: str, after_sequence: int) -> SessionEventBatch:
        _ = session_id, after_sequence
        return SessionEventBatch(status="completed")

    def session_result(self, *, session_id: str) -> RuntimeSessionResult:
        _ = session_id
        return _session_result(self.rich)

    def session_debug_snapshot(self, *, session_id: str) -> RuntimeSessionDebugSnapshot:
        _ = session_id
        return _debug_snapshot(self.rich)

    def undo_session(self, *, session_id: str) -> RuntimeSessionRevertMarker:
        _ = session_id
        return RuntimeSessionRevertMarker(sequence=1)

    def revert_session(self, *, session_id: str, sequence: int) -> RuntimeSessionRevertMarker:
        _ = session_id
        return RuntimeSessionRevertMarker(sequence=sequence)

    def unrevert_session(self, *, session_id: str) -> RuntimeSessionRevertMarker | None:
        _ = session_id
        return RuntimeSessionRevertMarker(sequence=1) if self.rich else None

    def cancel_session(self, session_id: str, *, run_id: str | None = None, reason: str | None = None) -> ActiveRunInterruptResult:
        return ActiveRunInterruptResult(
            session_id=session_id,
            status="interrupted" if self.rich else "not_active",
            run_id=run_id,
            reason=reason,
        )

    def queue_steering(self, session_id: str, content: str) -> tuple[dict[str, object], ...]:
        return (
            ({"session_id": session_id, "content": content}, {"session_id": session_id, "content": content})
            if self.rich
            else ({"session_id": session_id, "content": content},)
        )

    def resume(
        self,
        session_id: str,
        *,
        approval_request_id: str | None = None,
        approval_decision: PermissionResolution | None = None,
    ) -> RuntimeResponse:
        _ = session_id, approval_request_id, approval_decision
        return RuntimeResponse(session=_session_state(self.rich), events=_events(self.rich), output="answer" if self.rich else None)

    def answer_question(
        self,
        session_id: str,
        *,
        question_request_id: str,
        responses: tuple[QuestionResponse, ...],
    ) -> RuntimeResponse:
        _ = session_id, question_request_id, responses
        return RuntimeResponse(session=_session_state(self.rich), events=_events(self.rich), output=None)

    def list_sessions(self) -> tuple[StoredSessionSummary, ...]:
        # ``GET /api/sessions`` is the main-session surface: the transport drops
        # child sessions, so this fixture returns one of each to pin that filter
        # and the parent_id-absent shape it produces.
        return (
            StoredSessionSummary(session=SessionRef(id="sess-1"), status="completed", turn=3, prompt="hello", updated_at=1000),
            StoredSessionSummary(
                session=SessionRef(id="sess-2", parent_id="parent-1"), status="interrupted", turn=1, prompt="child", updated_at=1001
            ),
        )

    def session_forest(self) -> tuple[StoredSessionForestEntry, ...]:
        # The transport reads the list's ``depth`` from this projection, so both
        # wire shapes are pinned: ``rich`` supplies a depth (the shape a real
        # workspace has) and ``minimal`` omits the session entirely (``null``).
        if not self.rich:
            return ()
        return (
            StoredSessionForestEntry(session_id="sess-1", forked_from_session_id=None, forked_at_sequence=None, depth=0),
            StoredSessionForestEntry(session_id="sess-2", forked_from_session_id="sess-1", forked_at_sequence=2, depth=1),
        )

    def list_background_tasks(self) -> tuple[StoredBackgroundTaskSummary, ...]:
        return (_background_task_summary(self.rich),)

    def list_background_tasks_by_parent_session(self, *, parent_session_id: str) -> tuple[StoredBackgroundTaskSummary, ...]:
        _ = parent_session_id
        return (_background_task_summary(self.rich),)

    def start_background_task(self, request: object) -> BackgroundTaskState:
        _ = request
        return _background_task_state(self.rich)

    def load_background_task(self, task_id: str) -> BackgroundTaskState:
        _ = task_id
        return _background_task_state(self.rich)

    def load_background_task_result(self, task_id: str) -> BackgroundTaskResult:
        _ = task_id
        return _background_task_result(self.rich)

    def load_background_task_result_by_child_session(self, *, child_session_id: str) -> BackgroundTaskResult | None:
        _ = child_session_id
        return _background_task_result(self.rich)

    def cancel_background_task(self, task_id: str) -> BackgroundTaskState:
        _ = task_id
        return _background_task_state(self.rich)

    def retry_background_task(self, task_id: str) -> BackgroundTaskState:
        _ = task_id
        return _background_task_state(self.rich)

    def steer_background_task(self, task_id: str, content: str) -> BackgroundTaskState:
        _ = task_id, content
        return _background_task_state(self.rich)

    def web_settings(self) -> dict[str, object]:
        return {"provider": _OPENCODE if self.rich else None, "provider_api_key_present": self.rich, "model": "gpt-5.4" if self.rich else None}

    def update_web_settings(self, *, provider: str | None = None, provider_api_key: str | None = None, model: str | None = None) -> dict[str, object]:
        _ = provider, provider_api_key, model
        return self.web_settings()

    def list_provider_summaries(self) -> tuple[ProviderSummary, ...]:
        return (ProviderSummary(name=_OPENCODE, label="OpenCode Go", configured=self.rich, current=self.rich),)

    def provider_models_result(self, provider_name: str) -> ProviderModelsResult:
        _ = provider_name
        return _provider_models(self.rich)

    def inspect_provider(self, provider_name: str) -> ProviderInspectResult:
        _ = provider_name
        return ProviderInspectResult(
            summary=ProviderSummary(name=_OPENCODE, label="OpenCode Go", configured=self.rich, current=self.rich),
            models=_provider_models(self.rich),
            validation=_provider_validation(self.rich),
            current_model="gpt-5.4" if self.rich else None,
            current_model_metadata=ProviderModelMetadata(context_window=200000) if self.rich else None,
            readiness=(
                ProviderReadinessResult(
                    provider=_OPENCODE,
                    model="gpt-5.4",
                    configured=True,
                    ok=True,
                    status="ready",
                    guidance="run it",
                    auth_present=True,
                    streaming_configured=True,
                    streaming_supported=True,
                    context_window=200000,
                    max_output_tokens=32000,
                    fallback_chain=("gpt-5.3",),
                    reasoning_controls={"effort": "medium"},
                )
                if self.rich
                else None
            ),
        )

    def validate_provider_credentials(self, provider_name: str) -> ProviderValidationResult:
        _ = provider_name
        return _provider_validation(self.rich)

    def list_agent_summaries(self) -> tuple[AgentSummary, ...]:
        return (
            AgentSummary(
                id="leader",
                label="Leader",
                description="Primary agent" if self.rich else None,
                mode="primary" if self.rich else None,
                selectable=True,
                configured=self.rich,
                model="gpt-5.4" if self.rich else None,
                model_label="gpt-5.4" if self.rich else None,
                model_source="configured" if self.rich else None,
                provider=_OPENCODE if self.rich else None,
                fallback_chain=("gpt-5.3",) if self.rich else (),
                source_scope="workspace" if self.rich else None,
                source_path="agents/leader.json" if self.rich else None,
            ),
        )

    def list_skill_summaries(self) -> tuple[object, ...]:
        contracts = importlib.import_module("voidcode.runtime.contracts")
        return (
            contracts.SkillSummary(
                name="review",
                description="Review a diff",
                origin="catalog",
                source_path="skills/review/SKILL.md" if self.rich else None,
            ),
        )

    def list_command_summaries(self) -> tuple[CommandSummary, ...]:
        return (
            CommandSummary(
                name="fix",
                description="Fix the failing test",
                source="markdown",
                enabled=True,
                hidden=False,
                agent="worker" if self.rich else None,
                model="gpt-5.4" if self.rich else None,
                subtask=self.rich,
                path=".voidcode/commands/fix.md" if self.rich else None,
            ),
        )

    def current_status(self) -> RuntimeStatusSnapshot:
        return _runtime_status(self.rich)

    def retry_mcp_connections(self) -> RuntimeStatusSnapshot:
        return _runtime_status(self.rich)

    def review_snapshot(self) -> WorkspaceReviewSnapshot:
        return _review_snapshot(self.rich)

    def review_diff(self, path: str) -> ReviewFileDiff:
        return ReviewFileDiff(
            root="/workspace",
            path=path,
            state="changed",
            diff="@@ -1 +1 @@" if self.rich else None,
        )


# ------------------------------------------------------------------- app plumbing


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _run_app(
    app: object,
    *,
    method: str,
    path: str,
    body: bytes = b"",
    query_string: bytes = b"",
) -> _TransportResponse:
    messages: list[dict[str, object]] = [{"type": "http.request", "body": body, "more_body": False}]
    sent: list[dict[str, object]] = []

    async def _receive() -> dict[str, object]:
        if messages:
            return messages.pop(0)
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        sent.append(message)

    scope: dict[str, object] = {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "headers": [],
    }
    asyncio.run(cast(Any, app)(scope, _receive, _send))
    start_message = next(message for message in sent if message["type"] == "http.response.start")
    headers = {key.decode("utf-8").lower(): value.decode("utf-8") for key, value in cast(list[tuple[bytes, bytes]], start_message["headers"])}
    body_bytes = b"".join(cast(bytes, message.get("body", b"")) for message in sent if message["type"] == "http.response.body")
    return _TransportResponse(status=cast(int, start_message["status"]), headers=headers, body=body_bytes)


def _workspace_coordinator(workspace: Path, *, rich: bool) -> WorkspaceRuntimeCoordinator:
    def _runtime_factory(_workspace: Path) -> object:
        return _FixtureRuntime(rich=rich)

    return WorkspaceRuntimeCoordinator(
        initial_workspace=workspace,
        runtime_factory=cast(Any, _runtime_factory),
    )


def _app(workspace: Path, *, rich: bool) -> object:
    runtime_http = importlib.import_module("voidcode.runtime.transport.http")
    coordinator = _workspace_coordinator(workspace, rich=rich)
    return runtime_http.RuntimeTransportApp(
        runtime_factory=cast(Any, coordinator.runtime),
        workspace_coordinator=coordinator,
    )


def _openapi_document(app: object) -> dict[str, Any]:
    response = _run_app(app, method="GET", path="/api/openapi.json")
    assert response.status == 200, response.body
    return cast(dict[str, Any], response.json())


# ------------------------------------------------------------------ route driving

_PATH_PARAMETERS: dict[str, str] = {
    "session_id": "sess-1",
    "task_id": "task-1",
    "provider_name": _OPENCODE,
    "path": "src/sample.py",
}

# Bodies for the routes that own a request body. The workspace switch is driven
# separately because its body names the workspace to open.
_BODIES: dict[tuple[str, str], dict[str, object]] = {
    ("POST", "/api/runtime/run/stream"): {"prompt": "hello"},
    ("POST", "/api/tasks"): {"prompt": "delegate"},
    ("POST", "/api/settings"): {"provider": _OPENCODE},
    ("POST", "/api/tasks/{task_id}/steer"): {"prompt": "keep going"},
    ("POST", "/api/tasks/{task_id}/retry"): {},
    ("POST", "/api/tasks/{task_id}/cancel"): {},
    ("POST", "/api/sessions/{session_id}/approval"): {"request_id": "req-1", "decision": "allow"},
    ("POST", "/api/sessions/{session_id}/question"): {"request_id": "req-1", "responses": [{"header": "Pick one", "answers": ["a"]}]},
    ("POST", "/api/sessions/{session_id}/revert"): {"sequence": 1},
    ("POST", "/api/sessions/{session_id}/steer"): {"content": "focus"},
    ("POST", "/api/sessions/{session_id}/cancel"): {},
    ("POST", "/api/sessions/{session_id}/undo"): {},
    ("POST", "/api/sessions/{session_id}/unrevert"): {},
    ("POST", "/api/sessions/{session_id}/resume"): {},
    ("POST", "/api/status/mcp/retry"): {},
    ("POST", "/api/providers/{provider_name}/validate"): {},
}

_SSE_TEMPLATES = frozenset({"/api/runtime/run/stream", "/api/sessions/{session_id}/events"})


def _concrete_path(template: str) -> str:
    path = template
    for name, value in _PATH_PARAMETERS.items():
        path = path.replace(f"{{{name}}}", value)
    return path


_JSON_MEDIA_TYPE = "application/json; charset=utf-8"


def _ref(model_name: str) -> dict[str, str]:
    """The document's reference to one response model."""
    return {"$ref": f"#/components/schemas/{model_name}"}


def _documented_routes(document: dict[str, Any]) -> list[tuple[str, str]]:
    return sorted((method.upper(), path) for path, operations in document["paths"].items() for method in operations)


def _response_models() -> dict[str, type[ResponseModel]]:
    models: dict[str, type[ResponseModel]] = {}
    for name in dir(http_models):
        candidate = getattr(http_models, name)
        if isinstance(candidate, type) and issubclass(candidate, ResponseModel) and candidate is not ResponseModel:
            models[name] = candidate
    return models


def _model_named(name: str) -> type[ResponseModel]:
    models = _response_models()
    assert name in models, f"no response model named {name!r} in {http_models.__name__}"
    return models[name]


def _model_from_schema(schema: dict[str, Any]) -> Any:
    """Resolve a documented schema (``$ref``, or array of ``$ref``) to its model."""
    reference = schema.get("$ref")
    if reference is not None:
        return _model_named(cast(str, reference).rsplit("/", 1)[-1])
    if schema.get("type") == "array":
        return list[_model_from_schema(cast(dict[str, Any], schema["items"]))]
    raise AssertionError(f"response schema does not name a model: {schema}")


def _success_status_code(operation: dict[str, Any]) -> str:
    codes = [code for code in operation["responses"] if code.startswith("2")]
    assert len(codes) == 1, f"expected exactly one documented success status, got {codes}"
    return codes[0]


def _success_content(operation: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], operation["responses"][_success_status_code(operation)].get("content", {}))


def _success_schema(operation: dict[str, Any]) -> dict[str, Any]:
    content = _success_content(operation)
    media_types = [media_type for media_type, media in content.items() if "schema" in media]
    assert media_types, f"success response has no schema: {json.dumps(content, sort_keys=True)}"
    return cast(dict[str, Any], content[media_types[0]]["schema"])


def _statuses_carrying_the_success_schema(operation: dict[str, Any]) -> list[int]:
    """Every documented status whose body is the success payload.

    A provider that is not configured answers 409 with the same body it answers
    200 with, so the status a route returns for a given fixture is not always its
    success status. The document is the authority here too: a status counts only
    when it documents the same schema.
    """
    success = _success_schema(operation)
    statuses: list[int] = []
    for code, response in operation["responses"].items():
        if not code.isdigit():
            continue
        for media in response.get("content", {}).values():
            if media.get("schema") == success:
                statuses.append(int(code))
    return sorted(statuses)


def _assert_body_matches_model(body: object, model: Any, *, label: str) -> None:
    """The emitted body validates against the model and round-trips unchanged.

    ``exclude_unset`` is what reproduces the wire's ``null``-vs-absent choice: the
    body only sets the keys the transport wrote, so the fields the body omitted
    stay omitted. A body that carried an explicit ``null`` for a key some
    serializer omits would fail here, which is the point.
    """
    adapter = TypeAdapter(model)
    validated = adapter.validate_python(body)
    assert adapter.dump_python(validated, mode="json", exclude_unset=True) == body, f"{label}: the model does not reproduce the body"


def _assert_wire_rendering(response: _TransportResponse, *, label: str) -> None:
    """The transport's byte-stable renderer is still the one writing bodies."""
    assert response.headers["content-type"] == "application/json; charset=utf-8", label
    parsed = response.json()
    assert response.body == json.dumps(parsed, sort_keys=True).encode("utf-8"), f"{label}: body is not the sorted-key JSON rendering"


@pytest.mark.parametrize("rich", [True, False], ids=["rich", "minimal"])
def test_every_documented_json_route_emits_a_body_its_documented_model_describes(tmp_path: Path, rich: bool) -> None:
    """Drive every documented JSON route and pin its body to the documented model.

    The model is resolved from the document itself, so this asserts the
    document's claim about the wire rather than a second hand-kept mapping: a
    route whose body drifts, whose model drifts, or whose schema reference is
    wrong fails here.
    """
    app = _app(tmp_path, rich=rich)
    document = _openapi_document(app)
    other_workspace = tmp_path / "other"
    other_workspace.mkdir()

    sse_routes: list[tuple[str, str]] = []
    for method, template in _documented_routes(document):
        operation = document["paths"][template][method.lower()]
        if template in _SSE_TEMPLATES:
            sse_routes.append((method, template))
            continue

        body = _BODIES.get((method, template))
        if template == "/api/workspaces/open":
            body = {"path": str(other_workspace)}
        response = _run_app(
            app,
            method=method,
            path=_concrete_path(template),
            body=json.dumps(body).encode("utf-8") if body is not None else b"",
        )
        label = f"{method} {template}"

        assert response.status in _statuses_carrying_the_success_schema(operation), f"{label}: {response.body!r}"
        _assert_wire_rendering(response, label=label)
        _assert_body_matches_model(response.json(), _model_from_schema(_success_schema(operation)), label=label)

    assert sorted(sse_routes) == [("GET", "/api/sessions/{session_id}/events"), ("POST", "/api/runtime/run/stream")]


def test_every_documented_operation_documents_the_error_envelope(tmp_path: Path) -> None:
    """Failing responses are documented too: named statuses plus a catch-all."""
    document = _openapi_document(_app(tmp_path, rich=True))

    for method, template in _documented_routes(document):
        operation = document["paths"][template][method.lower()]
        responses = operation["responses"]
        label = f"{method} {template}"
        assert "default" in responses, f"{label} does not document the error envelope"
        assert responses["default"]["content"][_JSON_MEDIA_TYPE]["schema"] == _ref("ErrorEnvelope"), label
        for status in ("400", "404", "405"):
            if status in responses:
                assert responses[status]["content"][_JSON_MEDIA_TYPE]["schema"] == _ref("ErrorEnvelope"), f"{label} {status}"
        # A documented failing status answers either the envelope or the route's
        # own payload (a provider that is not configured answers 409 with it).
        success = _success_schema(operation)
        for status, response in responses.items():
            if status == "default" or not status.isdigit() or status.startswith("2"):
                continue
            schemas = [media["schema"] for media in response.get("content", {}).values() if "schema" in media]
            assert schemas, f"{label} {status} has no body"
            for schema in schemas:
                assert schema in (_ref("ErrorEnvelope"), success), f"{label} {status}: {schema}"
        # The transport answers validation failures with 400, never FastAPI's 422.
        assert "422" not in responses, f"{label} advertises a 422"


def test_response_model_schemas_describe_their_fields_in_both_modes() -> None:
    """Every model's OpenAPI schema carries its real fields, in both schema modes.

    FastAPI builds response fields in ``serialization`` mode, so a model whose
    serialization schema collapsed to ``{"type": "object"}`` would publish an
    empty response body in the document the frontend types are generated from.
    """
    for name, model in sorted(_response_models().items()):
        for mode in ("validation", "serialization"):
            schema = model.model_json_schema(mode=cast(Any, mode))
            if "$ref" in schema:
                # A self-referencing model is emitted as a reference to its own
                # definition.
                target = schema["$ref"].rsplit("/", 1)[-1]
                schema = schema["$defs"][target]
            assert schema.get("additionalProperties") is False, f"{name} ({mode}) is not a closed object"
            assert set(schema["properties"]) == set(model.model_fields), f"{name} ({mode}) does not describe every field"


def test_every_documented_body_is_typed(tmp_path: Path) -> None:
    """No route is left with an empty response schema."""
    document = _openapi_document(_app(tmp_path, rich=True))

    untyped: list[tuple[str, str]] = []
    for method, template in _documented_routes(document):
        operation = document["paths"][template][method.lower()]
        content = _success_content(operation)
        if any("schema" in media or "itemSchema" in media for media in content.values()):
            continue
        untyped.append((method, template))

    assert untyped == []


# -------------------------------------------------------------------- SSE frames


def _sse_frames(response: _TransportResponse) -> list[bytes]:
    """Split a server-sent-events body into its raw frames."""
    frames = [frame for frame in response.body.split(b"\n\n") if frame]
    for frame in frames:
        assert frame.startswith(b"data: "), frame
        assert b"\n" not in frame, f"frames carry one data line only: {frame!r}"
    return frames


def _sse_payloads(response: _TransportResponse) -> list[object]:
    return [json.loads(frame[len(b"data: ") :].decode("utf-8")) for frame in _sse_frames(response)]


def _sse_frame_models(document: dict[str, Any], template: str, method: str) -> dict[str, Any]:
    content = _success_content(document["paths"][template][method.lower()])
    assert set(content) == {"text/event-stream"}, content
    item_schema = content["text/event-stream"].get("itemSchema")
    assert item_schema is not None, f"{method} {template} does not publish its frame model"
    return cast(dict[str, Any], item_schema)


def test_run_stream_frames_validate_against_the_documented_frame_model(tmp_path: Path) -> None:
    app = _app(tmp_path, rich=True)
    document = _openapi_document(app)
    frame_model = _model_from_schema(_sse_frame_models(document, "/api/runtime/run/stream", "POST"))

    response = _run_app(
        app,
        method="POST",
        path="/api/runtime/run/stream",
        body=json.dumps({"prompt": "hello"}).encode("utf-8"),
    )

    assert response.status == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    payloads = _sse_payloads(response)
    kinds = [cast(dict[str, object], payload)["kind"] for payload in payloads]
    assert kinds == ["event", "output", "event"]
    # The session state is re-sent only when it changes: the middle frame repeats
    # the same state as the first, the last frame carries a changed state.
    sessions = [cast(dict[str, object], payload)["session"] for payload in payloads]
    assert sessions[1] is None
    assert sessions[0] != sessions[2]
    for payload in payloads:
        _assert_body_matches_model(payload, frame_model, label="run/stream frame")


def test_session_event_frames_validate_against_the_documented_frame_model(tmp_path: Path) -> None:
    app = _app(tmp_path, rich=True)
    document = _openapi_document(app)
    frame_model = _model_from_schema(_sse_frame_models(document, "/api/sessions/{session_id}/events", "GET"))

    response = _run_app(app, method="GET", path="/api/sessions/sess-1/events")

    assert response.status == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    payloads = _sse_payloads(response)
    kinds = [cast(dict[str, object], payload)["kind"] for payload in payloads]
    assert kinds == ["session", "event", "event"]
    assert all(cast(dict[str, object], payload)["output"] is None for payload in payloads)
    for payload in payloads:
        _assert_body_matches_model(payload, frame_model, label="session events frame")


def test_frame_models_reject_frames_that_contradict_their_kind() -> None:
    """The kind/payload invariant is part of the contract, not just a convention."""
    run_frame = _model_named("RunStreamFrameBody")
    with pytest.raises(Exception, match="event frames must carry an event"):
        run_frame.model_validate({"kind": "event", "session": None, "event": None, "output": None})
    with pytest.raises(Exception, match="output frames must carry output content"):
        run_frame.model_validate({"kind": "output", "session": None, "event": None, "output": None})

    event_frame = _model_named("SessionEventFrameBody")
    with pytest.raises(Exception, match="session frames must carry the session state"):
        event_frame.model_validate({"kind": "session", "session": None, "event": None, "output": None})
    state = _session_state_json(True)
    with pytest.raises(Exception, match="event frames must carry an event"):
        event_frame.model_validate({"kind": "event", "session": state, "event": None, "output": None})


def test_documented_models_reject_undocumented_keys_and_accept_absent_optionals() -> None:
    """``extra="forbid"`` is what makes the drift assertions meaningful."""
    summary = _model_named("SessionSummaryBody")
    state = _session_state_json(True)
    good = {"session": state["session"], "status": "completed", "turn": 1, "prompt": "hi", "updated_at": 1}
    adapter = TypeAdapter(summary)
    assert adapter.dump_python(adapter.validate_python(good), mode="json", exclude_unset=True) == good
    with pytest.raises(Exception, match="extra_forbidden"):
        summary.model_validate({**good, "unexpected": 1})
    # ``parent_id`` is omitted by the transport's serializer when unset; the
    # model reproduces that absence under ``exclude_unset`` instead of emitting
    # an explicit null.
    minimal = {**good, "session": {"id": "sess-1"}}
    assert adapter.dump_python(adapter.validate_python(minimal), mode="json", exclude_unset=True) == minimal
