"""Contract parity tests for CLI/HTTP/Web field consistency.

These tests assert that the CLI and HTTP transports serialize the same runtime
boundary types with consistent field names and structure, preventing contract
drift as the runtime surface expands.

Parity requirements covered:
- EventEnvelope fields match between CLI and HTTP serialization
- StoredSessionSummary shape matches (nested SessionRef)
- SessionState shape matches
- Background task state/summary/result fields match
- RuntimeSessionResult fields match
- RuntimeNotification fields match
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture
def _force_deterministic_engine_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")


pytestmark = pytest.mark.usefixtures("_force_deterministic_engine_default")


def _runtime_module() -> Any:
    return importlib.import_module("voidcode.runtime")


def _cli_support_module() -> Any:
    return importlib.import_module("voidcode.cli_support")


def _http_module() -> Any:
    return importlib.import_module("voidcode.runtime.http")


def _make_event(runtime: Any, *, session_id: str = "s1", sequence: int = 1) -> Any:
    return runtime.EventEnvelope(
        session_id=session_id,
        sequence=sequence,
        event_type="runtime.request_received",
        source="runtime",
        payload={"prompt": "hello"},
    )


def _make_session(runtime: Any, *, session_id: str = "s1") -> Any:
    return runtime.SessionState(
        session=runtime.SessionRef(id=session_id),
        status="completed",
        turn=1,
        metadata={},
    )


def _make_session_summary(runtime: Any, *, session_id: str = "s1") -> Any:
    return runtime.StoredSessionSummary(
        session=runtime.SessionRef(id=session_id),
        status="completed",
        turn=1,
        prompt="hello",
        updated_at=1234567890,
    )


# ---------------------------------------------------------------------------
# Event serialization parity
# ---------------------------------------------------------------------------


def test_cli_and_http_event_serialization_share_required_fields() -> None:
    """CLI and HTTP must emit the same core EventEnvelope fields."""
    runtime = _runtime_module()
    cli_support = _cli_support_module()
    http_module = _http_module()

    event = _make_event(runtime)
    cli_serialized = cli_support.serialize_event(event)
    http_serialized = http_module.RuntimeTransportApp._serialize_event(event)

    required_fields = {"session_id", "sequence", "event_type", "source", "payload"}
    for field in required_fields:
        assert field in cli_serialized, f"CLI event serialization missing field: {field}"
        assert field in http_serialized, f"HTTP event serialization missing field: {field}"
        assert cli_serialized[field] == http_serialized[field], (
            f"CLI and HTTP disagree on event field '{field}': "
            f"CLI={cli_serialized[field]!r}, HTTP={http_serialized[field]!r}"
        )


def test_cli_and_http_event_serialization_include_delegated_lifecycle() -> None:
    """Both CLI and HTTP must include delegated_lifecycle when present."""
    runtime = _runtime_module()
    cli_support = _cli_support_module()
    http_module = _http_module()

    event = runtime.EventEnvelope(
        session_id="leader-session",
        sequence=2,
        event_type="runtime.background_task_completed",
        source="runtime",
        payload={
            "task_id": "task-1",
            "parent_session_id": "leader-session",
            "child_session_id": "child-session",
            "status": "completed",
            "summary_output": "done",
            "result_available": True,
            "delegation": {
                "delegated_task_id": "task-1",
                "parent_session_id": "leader-session",
                "child_session_id": "child-session",
                "routing": {"mode": "background"},
                "lifecycle_status": "completed",
                "result_available": True,
                "approval_blocked": False,
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
        },
    )

    cli_serialized = cli_support.serialize_event(event)
    http_serialized = http_module.RuntimeTransportApp._serialize_event(event)

    assert "delegated_lifecycle" in cli_serialized, (
        "CLI event serialization must include delegated_lifecycle"
    )
    assert "delegated_lifecycle" in http_serialized, (
        "HTTP event serialization must include delegated_lifecycle"
    )
    assert cli_serialized["delegated_lifecycle"] == http_serialized["delegated_lifecycle"]


# ---------------------------------------------------------------------------
# Session summary shape parity
# ---------------------------------------------------------------------------


def test_cli_and_http_session_summary_use_nested_session_ref() -> None:
    """CLI and HTTP must use nested SessionRef structure, not flat id/parent_id."""
    runtime = _runtime_module()
    cli_support = _cli_support_module()
    http_module = _http_module()

    summary = _make_session_summary(runtime)
    cli_serialized = cli_support.serialize_stored_session_summary(summary)
    http_serialized = http_module.RuntimeTransportApp._serialize_stored_session_summary(summary)

    assert "session" in cli_serialized, "CLI summary must use nested 'session' key"
    assert "session" in http_serialized, "HTTP summary must use nested 'session' key"
    assert "id" in cli_serialized["session"], "CLI session ref must have 'id'"
    assert "id" in http_serialized["session"], "HTTP session ref must have 'id'"

    shared_fields = {"session", "status", "turn", "prompt", "updated_at"}
    for field in shared_fields:
        assert field in cli_serialized, f"CLI summary missing field: {field}"
        assert field in http_serialized, f"HTTP summary missing field: {field}"


def test_cli_and_http_session_summary_parent_id_consistency() -> None:
    """CLI and HTTP must handle parent_id consistently in SessionRef."""
    runtime = _runtime_module()
    cli_support = _cli_support_module()
    http_module = _http_module()

    summary_with_parent = runtime.StoredSessionSummary(
        session=runtime.SessionRef(id="child-1", parent_id="parent-1"),
        status="completed",
        turn=1,
        prompt="hello",
        updated_at=1234567890,
    )

    cli_serialized = cli_support.serialize_stored_session_summary(summary_with_parent)
    http_serialized = http_module.RuntimeTransportApp._serialize_stored_session_summary(
        summary_with_parent
    )

    assert "parent_id" in cli_serialized["session"]
    assert "parent_id" in http_serialized["session"]
    assert cli_serialized["session"]["parent_id"] == "parent-1"
    assert http_serialized["session"]["parent_id"] == "parent-1"

    summary_without_parent = runtime.StoredSessionSummary(
        session=runtime.SessionRef(id="solo-1"),
        status="completed",
        turn=1,
        prompt="hello",
        updated_at=1234567890,
    )

    cli_serialized_no_parent = cli_support.serialize_stored_session_summary(summary_without_parent)
    http_serialized_no_parent = http_module.RuntimeTransportApp._serialize_stored_session_summary(
        summary_without_parent
    )

    assert "parent_id" not in cli_serialized_no_parent["session"]
    assert "parent_id" not in http_serialized_no_parent["session"]


# ---------------------------------------------------------------------------
# Session state shape parity
# ---------------------------------------------------------------------------


def test_cli_and_http_session_state_shape_match() -> None:
    """CLI and HTTP session state must share the same nested structure."""
    runtime = _runtime_module()
    cli_support = _cli_support_module()
    http_module = _http_module()

    session = _make_session(runtime)
    cli_serialized = cli_support.serialize_session_state(session)
    http_serialized = http_module.RuntimeTransportApp._serialize_session_state(session)

    required_fields = {"session", "status", "turn", "metadata"}
    for field in required_fields:
        assert field in cli_serialized, f"CLI session state missing field: {field}"
        assert field in http_serialized, f"HTTP session state missing field: {field}"

    assert cli_serialized["session"]["id"] == http_serialized["session"]["id"]
    assert cli_serialized["status"] == http_serialized["status"]
    assert cli_serialized["turn"] == http_serialized["turn"]


# ---------------------------------------------------------------------------
# Contract field presence tests (fail if fields are removed)
# ---------------------------------------------------------------------------


def test_event_envelope_requires_session_id_and_sequence() -> None:
    """EventEnvelope must always include session_id and sequence for correlation."""
    runtime = _runtime_module()
    event = runtime.EventEnvelope(
        session_id="s1",
        sequence=42,
        event_type="runtime.request_received",
        source="runtime",
    )
    assert event.session_id == "s1"
    assert event.sequence == 42


def test_stored_session_summary_requires_session_ref() -> None:
    """StoredSessionSummary must use SessionRef, not raw id string."""
    runtime = _runtime_module()
    summary = runtime.StoredSessionSummary(
        session=runtime.SessionRef(id="s1"),
        status="completed",
        turn=1,
        prompt="hello",
        updated_at=0,
    )
    assert hasattr(summary.session, "id")
    assert summary.session.id == "s1"


def test_runtime_notification_requires_event_sequence() -> None:
    """RuntimeNotification must always carry event_sequence for correlation."""
    contracts = importlib.import_module("voidcode.runtime.contracts")
    notification = contracts.RuntimeNotification(
        id="n1",
        session=contracts.SessionRef(id="s1"),
        kind="completion",
        status="unread",
        summary="done",
        event_sequence=5,
        created_at=0,
    )
    assert notification.event_sequence == 5


def test_runtime_session_result_includes_revert_marker() -> None:
    """RuntimeSessionResult must expose revert_marker for undo/revert flows."""
    runtime = _runtime_module()
    result = runtime.RuntimeSessionResult(
        session=_make_session(runtime),
        prompt="hello",
        status="completed",
        summary="done",
        revert_marker=runtime.RuntimeSessionRevertMarker(sequence=3, active=True),
    )
    assert result.revert_marker is not None
    assert result.revert_marker.sequence == 3
    assert result.revert_marker.active is True


def test_runtime_session_debug_snapshot_includes_revert_marker_and_provider_context() -> None:
    """RuntimeSessionDebugSnapshot must expose revert_marker and provider_context."""
    contracts = importlib.import_module("voidcode.runtime.contracts")
    runtime = _runtime_module()

    snapshot = contracts.RuntimeSessionDebugSnapshot(
        session=_make_session(runtime),
        prompt="hello",
        persisted_status="completed",
        current_status="completed",
        active=False,
        resumable=True,
        replayable=True,
        terminal=True,
        revert_marker=contracts.RuntimeSessionRevertMarker(sequence=2),
        provider_context=contracts.RuntimeProviderContextSnapshot(
            provider="openai",
            model="gpt-4",
            execution_engine="provider",
            segment_count=1,
            message_count=1,
        ),
    )
    assert snapshot.revert_marker is not None
    assert snapshot.provider_context is not None
    assert snapshot.provider_context.provider == "openai"


def test_background_task_state_includes_unix_ms_timestamps() -> None:
    """BackgroundTaskState must include unix_ms timestamps for frontend time display."""
    task_module = importlib.import_module("voidcode.runtime.task")
    task = task_module.BackgroundTaskState(
        task=task_module.BackgroundTaskRef(id="t1"),
        status="completed",
        request=task_module.BackgroundTaskRequestSnapshot(prompt="work"),
        created_at_unix_ms=1000,
        started_at_unix_ms=1001,
        finished_at_unix_ms=1002,
    )
    assert task.created_at_unix_ms == 1000
    assert task.started_at_unix_ms == 1001
    assert task.finished_at_unix_ms == 1002


def test_background_task_result_includes_duration_and_tool_count() -> None:
    """BackgroundTaskResult must include duration_seconds and tool_call_count."""
    runtime = _runtime_module()
    result = runtime.BackgroundTaskResult(
        task_id="t1",
        parent_session_id=None,
        child_session_id=None,
        status="completed",
        duration_seconds=1.5,
        tool_call_count=3,
    )
    assert result.duration_seconds == 1.5
    assert result.tool_call_count == 3


def test_runtime_status_snapshot_includes_background_tasks() -> None:
    """RuntimeStatusSnapshot must include background_tasks status overview."""
    contracts = importlib.import_module("voidcode.runtime.contracts")
    snapshot = contracts.RuntimeStatusSnapshot(
        git=contracts.GitStatusSnapshot(state="git_ready"),
        lsp=contracts.CapabilityStatusSnapshot(state="stopped"),
        mcp=contracts.CapabilityStatusSnapshot(state="stopped"),
        acp=contracts.CapabilityStatusSnapshot(state="unconfigured"),
        background_tasks=contracts.RuntimeBackgroundTaskStatusSnapshot(
            active_worker_slots=0,
            queued_count=0,
            running_count=0,
            terminal_count=0,
            default_concurrency=4,
        ),
    )
    assert snapshot.background_tasks is not None
    assert snapshot.background_tasks.default_concurrency == 4
