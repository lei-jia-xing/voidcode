from __future__ import annotations

from typing import cast

from voidcode.acp import (
    AcpConfigState,
    AcpDelegatedExecution,
    AcpEventEnvelope,
    AcpEventPublisher,
    AcpRequestEnvelope,
    AcpRequestHandler,
    AcpResponseEnvelope,
)


def test_acp_config_state_defaults_to_disabled() -> None:
    assert AcpConfigState() == AcpConfigState(configured_enabled=False)


def test_acp_config_state_derives_enabled_flag_without_runtime_dependency() -> None:
    assert AcpConfigState.from_enabled(None).configured_enabled is False
    assert AcpConfigState.from_enabled(False).configured_enabled is False
    assert AcpConfigState.from_enabled(True).configured_enabled is True


def test_acp_request_envelope_defaults_payload_to_empty_object() -> None:
    envelope = AcpRequestEnvelope(request_type="ping")

    assert envelope.request_type == "ping"
    assert envelope.request_id is None
    assert envelope.session_id is None
    assert envelope.parent_session_id is None
    assert envelope.delegation is None
    assert envelope.payload == {}


def test_acp_delegated_execution_serializes_runtime_frozen_correlation_and_routing() -> None:
    delegation = AcpDelegatedExecution(
        parent_session_id="parent-1",
        requested_child_session_id="child-request-1",
        child_session_id="child-1",
        delegated_task_id="task-1",
        approval_request_id="approval-1",
        question_request_id="question-1",
        routing_mode="background",
        routing_category="deep",
        routing_description="Investigate",
        selected_preset="worker",
        selected_execution_engine="provider",
        lifecycle_status="waiting_approval",
        approval_blocked=True,
        result_available=False,
    )

    assert delegation.as_payload() == {
        "parent_session_id": "parent-1",
        "requested_child_session_id": "child-request-1",
        "child_session_id": "child-1",
        "delegated_task_id": "task-1",
        "approval_request_id": "approval-1",
        "question_request_id": "question-1",
        "routing_mode": "background",
        "routing_category": "deep",
        "routing_subagent_type": None,
        "routing_description": "Investigate",
        "routing_command": None,
        "selected_preset": "worker",
        "selected_execution_engine": "provider",
        "lifecycle_status": "waiting_approval",
        "approval_blocked": True,
        "result_available": False,
        "cancellation_cause": None,
    }


def test_acp_response_envelope_supports_ok_and_error_shapes() -> None:
    delegation = AcpDelegatedExecution(delegated_task_id="task-1", lifecycle_status="running")
    ok = AcpResponseEnvelope(
        status="ok",
        request_type="ping",
        request_id="req-1",
        session_id="child-1",
        parent_session_id="parent-1",
        delegation=delegation,
        payload={"accepted": True},
    )
    error = AcpResponseEnvelope(
        status="error",
        request_type="ping",
        request_id="req-1",
        delegation=delegation,
        error="boom",
        payload={"request_type": "ping"},
    )

    assert ok.payload == {"accepted": True}
    assert ok.error is None
    assert ok.request_id == "req-1"
    assert ok.session_id == "child-1"
    assert ok.parent_session_id == "parent-1"
    assert ok.delegation == delegation
    assert error.error == "boom"
    assert error.payload == {"request_type": "ping"}


def test_acp_event_protocol_supports_delegated_lifecycle_envelopes() -> None:
    class _StubPublisher:
        def publish(self, envelope: AcpEventEnvelope) -> AcpResponseEnvelope:
            return AcpResponseEnvelope(
                status="ok",
                session_id=envelope.session_id,
                parent_session_id=envelope.parent_session_id,
                delegation=envelope.delegation,
                payload={"event_type": envelope.event_type, **envelope.payload},
            )

    publisher = cast(AcpEventPublisher, _StubPublisher())
    delegation = AcpDelegatedExecution(delegated_task_id="task-1", lifecycle_status="completed")
    response = publisher.publish(
        AcpEventEnvelope(
            event_type="runtime.acp_delegated_lifecycle",
            session_id="child-1",
            parent_session_id="parent-1",
            delegation=delegation,
            payload={"result_available": True},
        )
    )

    assert response.payload == {
        "event_type": "runtime.acp_delegated_lifecycle",
        "result_available": True,
    }
    assert response.delegation == delegation


def test_acp_request_handler_protocol_matches_adapter_facing_request_contract() -> None:
    class _StubHandler:
        def request(self, envelope: AcpRequestEnvelope) -> AcpResponseEnvelope:
            return AcpResponseEnvelope(
                status="ok",
                payload={"request_type": envelope.request_type, **envelope.payload},
            )

    handler = cast(AcpRequestHandler, _StubHandler())
    response = handler.request(AcpRequestEnvelope(request_type="ping", payload={"x": 1}))

    assert response == AcpResponseEnvelope(
        status="ok",
        payload={"request_type": "ping", "x": 1},
    )
