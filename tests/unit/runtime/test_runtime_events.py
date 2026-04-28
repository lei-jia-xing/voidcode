from __future__ import annotations

from voidcode.runtime.events import (
    EMITTED_EVENT_TYPES,
    GRAPH_LOOP_STEP,
    GRAPH_MODEL_TURN,
    PROTOTYPE_ADDITIVE_EVENT_TYPES,
    RUNTIME_ACP_CONNECTED,
    RUNTIME_ACP_DISCONNECTED,
    RUNTIME_ACP_FAILED,
    RUNTIME_BACKGROUND_TASK_CANCELLED,
    RUNTIME_BACKGROUND_TASK_COMPLETED,
    RUNTIME_BACKGROUND_TASK_FAILED,
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    RUNTIME_DELEGATED_RESULT_AVAILABLE,
    RUNTIME_LSP_SERVER_FAILED,
    RUNTIME_LSP_SERVER_STARTED,
    RUNTIME_LSP_SERVER_STOPPED,
    RUNTIME_MCP_SERVER_FAILED,
    RUNTIME_MEMORY_REFRESHED,
    RUNTIME_SESSION_ENDED,
    RUNTIME_SESSION_IDLE,
    RUNTIME_SESSION_STARTED,
    RUNTIME_SKILL_LOADED,
    RUNTIME_SKILLS_APPLIED,
    RUNTIME_SKILLS_BINDING_MISMATCH,
    RUNTIME_TOOL_STARTED,
    DelegatedExecutionPayload,
    DelegatedLifecycleEventPayload,
    DelegatedLifecycleMessage,
    DelegatedRoutingPayload,
    EventEnvelope,
)


def test_runtime_event_types_include_stable_emitted_events() -> None:
    assert GRAPH_LOOP_STEP in EMITTED_EVENT_TYPES
    assert GRAPH_MODEL_TURN in EMITTED_EVENT_TYPES
    assert RUNTIME_SKILLS_APPLIED in EMITTED_EVENT_TYPES
    assert RUNTIME_ACP_CONNECTED in EMITTED_EVENT_TYPES
    assert RUNTIME_ACP_DISCONNECTED in EMITTED_EVENT_TYPES
    assert RUNTIME_ACP_FAILED in EMITTED_EVENT_TYPES
    assert RUNTIME_LSP_SERVER_STARTED in EMITTED_EVENT_TYPES
    assert RUNTIME_LSP_SERVER_STOPPED in EMITTED_EVENT_TYPES
    assert RUNTIME_LSP_SERVER_FAILED in EMITTED_EVENT_TYPES
    assert RUNTIME_MCP_SERVER_FAILED in EMITTED_EVENT_TYPES
    assert "runtime.plan_created" not in EMITTED_EVENT_TYPES
    assert RUNTIME_TOOL_STARTED in EMITTED_EVENT_TYPES


def test_future_additive_event_types_cover_async_lifecycle_surfaces() -> None:
    assert PROTOTYPE_ADDITIVE_EVENT_TYPES == (
        RUNTIME_MEMORY_REFRESHED,
        RUNTIME_SESSION_STARTED,
        RUNTIME_SESSION_ENDED,
        RUNTIME_SESSION_IDLE,
        RUNTIME_SKILLS_BINDING_MISMATCH,
        RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
        RUNTIME_BACKGROUND_TASK_COMPLETED,
        RUNTIME_BACKGROUND_TASK_FAILED,
        RUNTIME_BACKGROUND_TASK_CANCELLED,
        RUNTIME_DELEGATED_RESULT_AVAILABLE,
        RUNTIME_SKILL_LOADED,
    )


def test_delegated_lifecycle_payload_preserves_typed_transport_shape() -> None:
    delegated = DelegatedLifecycleEventPayload(
        session_id="child-session",
        parent_session_id="leader-session",
        delegation=DelegatedExecutionPayload(
            parent_session_id="leader-session",
            requested_child_session_id="child-requested",
            child_session_id="child-session",
            delegated_task_id="task-1",
            routing=DelegatedRoutingPayload(mode="background", category="deep"),
            selected_preset="worker",
            selected_execution_engine="provider",
            lifecycle_status="waiting_approval",
            approval_blocked=True,
            result_available=True,
        ),
        message=DelegatedLifecycleMessage(
            status="waiting_approval",
            approval_blocked=True,
            result_available=True,
        ),
    )

    payload = delegated.as_payload()

    assert payload["delegation"] == {
        "parent_session_id": "leader-session",
        "requested_child_session_id": "child-requested",
        "child_session_id": "child-session",
        "delegated_task_id": "task-1",
        "approval_request_id": None,
        "question_request_id": None,
        "routing": {"mode": "background", "category": "deep"},
        "selected_preset": "worker",
        "selected_execution_engine": "provider",
        "lifecycle_status": "waiting_approval",
        "approval_blocked": True,
        "result_available": True,
        "cancellation_cause": None,
    }
    assert payload["message"] == {
        "kind": "delegated_lifecycle",
        "status": "waiting_approval",
        "summary_output": None,
        "error": None,
        "approval_blocked": True,
        "result_available": True,
    }
    assert "task_id" not in payload
    assert "routing_category" not in payload


def test_event_envelope_requires_nested_delegated_payload_shape() -> None:
    event = EventEnvelope(
        session_id="leader-session",
        sequence=1,
        event_type=RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
        source="runtime",
        payload={
            "delegation": {
                "parent_session_id": "leader-session",
                "requested_child_session_id": "child-session",
                "child_session_id": "child-session",
                "delegated_task_id": "task-1",
                "approval_request_id": "approval-1",
                "routing": {"mode": "background", "subagent_type": "explore"},
            },
            "message": {"approval_blocked": True, "result_available": True},
        },
    )

    delegated = event.delegated_lifecycle

    assert delegated is not None
    assert delegated.parent_session_id == "leader-session"
    assert delegated.delegation.delegated_task_id == "task-1"
    assert delegated.delegation.lifecycle_status == "waiting_approval"
    assert delegated.delegation.routing is not None
    assert delegated.delegation.routing.subagent_type == "explore"
    assert delegated.message.approval_blocked is True


def test_acp_delegated_lifecycle_uses_top_level_message_state_when_message_missing() -> None:
    event = EventEnvelope(
        session_id="leader-session",
        sequence=1,
        event_type="runtime.acp_delegated_lifecycle",
        source="runtime",
        payload={
            "session_id": "child-session",
            "parent_session_id": "leader-session",
            "status": "running",
            "approval_blocked": True,
            "result_available": True,
            "delegation": {
                "parent_session_id": "leader-session",
                "child_session_id": "child-session",
                "delegated_task_id": "task-1",
                "lifecycle_status": "waiting_approval",
                "approval_blocked": True,
                "result_available": True,
            },
        },
    )

    delegated = event.delegated_lifecycle

    assert delegated is not None
    assert delegated.session_id == "child-session"
    assert delegated.parent_session_id == "leader-session"
    assert delegated.delegation.lifecycle_status == "waiting_approval"
    assert delegated.message.status == "waiting_approval"
    assert delegated.message.approval_blocked is True
    assert delegated.message.result_available is True
