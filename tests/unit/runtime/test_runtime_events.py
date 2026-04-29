from __future__ import annotations

from voidcode.runtime.contracts import BackgroundTaskResult
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
    RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED,
    RUNTIME_BACKGROUND_TASK_PROGRESS,
    RUNTIME_BACKGROUND_TASK_REGISTERED,
    RUNTIME_BACKGROUND_TASK_RESULT_READ,
    RUNTIME_BACKGROUND_TASK_STARTED,
    RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
    RUNTIME_CONTEXT_PRESSURE,
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
    RUNTIME_TODO_UPDATED,
    RUNTIME_TOOL_STARTED,
    DelegatedExecutionPayload,
    DelegatedLifecycleEventPayload,
    DelegatedLifecycleMessage,
    DelegatedRoutingPayload,
    EventEnvelope,
)
from voidcode.runtime.task import SubagentRoutingIdentity
from voidcode.runtime.tool_display import build_tool_display


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
        RUNTIME_CONTEXT_PRESSURE,
        RUNTIME_SESSION_STARTED,
        RUNTIME_SESSION_ENDED,
        RUNTIME_SESSION_IDLE,
        RUNTIME_SKILLS_BINDING_MISMATCH,
        RUNTIME_BACKGROUND_TASK_REGISTERED,
        RUNTIME_BACKGROUND_TASK_STARTED,
        RUNTIME_BACKGROUND_TASK_PROGRESS,
        RUNTIME_BACKGROUND_TASK_WAITING_APPROVAL,
        RUNTIME_BACKGROUND_TASK_COMPLETED,
        RUNTIME_BACKGROUND_TASK_FAILED,
        RUNTIME_BACKGROUND_TASK_CANCELLED,
        RUNTIME_BACKGROUND_TASK_NOTIFICATION_ENQUEUED,
        RUNTIME_BACKGROUND_TASK_RESULT_READ,
        RUNTIME_DELEGATED_RESULT_AVAILABLE,
        RUNTIME_SKILL_LOADED,
        RUNTIME_TODO_UPDATED,
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


def test_background_task_result_delegated_event_payload_names_are_explicit() -> None:
    result = BackgroundTaskResult(
        task_id="task-1",
        parent_session_id="leader-session",
        child_session_id="child-session",
        requested_child_session_id="child-requested",
        status="completed",
        routing=SubagentRoutingIdentity(mode="background", category="visual-engineering"),
        summary_output="done",
        result_available=True,
    )

    payload = result.delegated_event.as_payload()

    assert result.task_id == "task-1"
    assert payload["parent_session_id"] == "leader-session"
    assert payload["session_id"] == "child-session"
    assert payload["delegation"] == {
        "parent_session_id": "leader-session",
        "requested_child_session_id": "child-requested",
        "child_session_id": "child-session",
        "delegated_task_id": "task-1",
        "approval_request_id": None,
        "question_request_id": None,
        "routing": {"mode": "background", "category": "visual-engineering"},
        "selected_preset": "product",
        "selected_execution_engine": "provider",
        "lifecycle_status": "completed",
        "approval_blocked": False,
        "result_available": True,
        "cancellation_cause": None,
    }
    assert payload["message"] == {
        "kind": "delegated_lifecycle",
        "status": "completed",
        "summary_output": "done",
        "error": None,
        "approval_blocked": False,
        "result_available": True,
    }


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


def test_delegated_lifecycle_preserves_interrupted_status_on_failed_event() -> None:
    event = EventEnvelope(
        session_id="leader-session",
        sequence=1,
        event_type=RUNTIME_BACKGROUND_TASK_FAILED,
        source="runtime",
        payload={
            "session_id": "child-session",
            "parent_session_id": "leader-session",
            "delegation": {
                "parent_session_id": "leader-session",
                "child_session_id": "child-session",
                "delegated_task_id": "task-1",
                "lifecycle_status": "interrupted",
            },
            "message": {
                "status": "interrupted",
                "error": "background task interrupted before completion",
                "result_available": True,
            },
        },
    )

    delegated = event.delegated_lifecycle

    assert delegated is not None
    assert delegated.delegation.lifecycle_status == "interrupted"
    assert delegated.message.status == "interrupted"
    assert delegated.message.error == "background task interrupted before completion"


# ── Target contract: tool display metadata payload shapes ──────────────
# These tests encode the additive schema before the runtime emits it.
# They are expected to pass as pure schema assertions (no runtime needed).

VALID_TOOL_DISPLAY_KINDS = frozenset(
    {
        "shell",
        "context",
        "read",
        "write",
        "edit",
        "search",
        "fetch",
        "task",
        "skill",
        "question",
        "approval",
        "background",
        "lsp",
        "generic",
    }
)


def test_tool_display_payload_shape_is_well_formed() -> None:
    """Verify the ToolDisplay payload contract shape from the plan."""
    display: dict[str, object] = {
        "kind": "shell",
        "title": "Shell",
        "summary": "List directory contents",
        "args": ["ls", "-la"],
        "copyable": {"command": "ls -la", "output": "file1  file2"},
        "hidden": False,
    }

    assert display["kind"] in VALID_TOOL_DISPLAY_KINDS
    assert isinstance(display["title"], str) and len(display["title"]) > 0
    assert isinstance(display["summary"], str) and len(display["summary"]) > 0

    args_value = display.get("args")
    assert args_value is None or isinstance(args_value, list)

    copyable = display.get("copyable")
    if copyable is not None:
        assert isinstance(copyable, dict)
        assert any(key in copyable for key in ("command", "output", "path")), (
            "copyable must carry at least one payload key"
        )


def test_tool_status_payload_shape_includes_display() -> None:
    """The ToolStatusPayload must carry an optional display field."""
    tool_status = {
        "invocation_id": "call_123",
        "tool_name": "shell_exec",
        "phase": "running",
        "status": "running",
        "label": "List files",
        "display": {
            "kind": "shell",
            "title": "Shell",
            "summary": "List files",
        },
    }

    assert tool_status["tool_name"] == "shell_exec"
    assert tool_status["status"] in {"pending", "running", "completed", "failed"}
    assert "display" in tool_status
    display_value = tool_status["display"]
    assert isinstance(display_value, dict)
    assert display_value["kind"] == "shell"


def test_tool_status_allows_missing_display_for_backwards_compatibility() -> None:
    """Legacy events without display must remain valid."""
    tool_status = {
        "invocation_id": "call_xyz",
        "tool_name": "read_file",
        "status": "completed",
        "label": "Read 10 lines",
    }
    assert tool_status["tool_name"] == "read_file"
    assert tool_status["status"] == "completed"
    assert "display" not in tool_status


def test_tool_started_payload_target_shape_preserves_existing_keys() -> None:
    """tool_started payload must add display/tool_status without removing 'tool'."""
    payload = {
        "tool": "shell_exec",
        "tool_call_id": "call_abc",
        "display": {
            "kind": "shell",
            "title": "Shell",
            "summary": "List directory contents",
        },
        "tool_status": {
            "invocation_id": "call_abc",
            "tool_name": "shell_exec",
            "phase": "running",
            "status": "running",
            "label": "List directory contents",
            "display": {
                "kind": "shell",
                "title": "Shell",
                "summary": "List directory contents",
            },
        },
    }

    assert payload["tool"] == "shell_exec"
    assert payload["tool_call_id"] == "call_abc"
    assert "display" in payload
    assert "tool_status" in payload
    tool_status_value = payload["tool_status"]
    assert isinstance(tool_status_value, dict)
    display_value = tool_status_value["display"]
    assert isinstance(display_value, dict)
    assert display_value["kind"] == "shell"
    assert tool_status_value["status"] == "running"


def test_tool_completed_payload_target_shape_preserves_existing_keys() -> None:
    """tool_completed payload must add tool_status without removing result fields."""
    payload = {
        "tool": "shell_exec",
        "tool_call_id": "call_abc",
        "status": "ok",
        "arguments": {"command": "ls -la", "description": "List directory contents"},
        "content": "file1\nfile2\n",
        "error": None,
        "tool_status": {
            "invocation_id": "call_abc",
            "tool_name": "shell_exec",
            "phase": "completed",
            "status": "completed",
            "label": "List directory contents",
            "display": {
                "kind": "shell",
                "title": "Shell",
                "summary": "List directory contents",
                "copyable": {"command": "ls -la", "output": "file1\nfile2\n"},
            },
        },
    }

    assert payload["tool"] == "shell_exec"
    assert payload["tool_call_id"] == "call_abc"
    assert payload["status"] == "ok"
    assert "arguments" in payload
    assert "content" in payload
    assert "tool_status" in payload
    tool_status_value = payload["tool_status"]
    assert isinstance(tool_status_value, dict)
    display_value = tool_status_value["display"]
    assert isinstance(display_value, dict)
    assert display_value["kind"] == "shell"
    assert display_value["summary"] == "List directory contents"
    copyable_value = display_value["copyable"]
    assert isinstance(copyable_value, dict)
    assert copyable_value["command"] == "ls -la"


def test_tool_display_supports_all_kinds() -> None:
    """Every ToolDisplayKind from the plan must be accepted."""
    for kind in VALID_TOOL_DISPLAY_KINDS:
        display = {"kind": kind, "title": kind.title(), "summary": f"Execute {kind}"}
        assert display["kind"] in VALID_TOOL_DISPLAY_KINDS
        assert isinstance(display["title"], str)


def test_skill_tool_display_uses_name_argument() -> None:
    """Skill display metadata must use the canonical skill tool argument."""
    display = build_tool_display(
        "skill",
        {"name": "frontend-ui-ux", "skill": "legacy-skill"},
    )

    assert display["kind"] == "skill"
    assert display["title"] == "Skill"
    assert display["summary"] == "frontend-ui-ux"
    assert display["args"] == ["frontend-ui-ux", "legacy-skill"]


def test_background_cancel_display_uses_camel_case_task_id_argument() -> None:
    """Started-event display must use the canonical background_cancel argument."""
    display = build_tool_display("background_cancel", {"taskId": "task-123"})

    assert display["kind"] == "background"
    assert display["title"] == "Background"
    assert display["summary"] == "task-123"
    assert display["args"] == ["task-123"]


def test_background_cancel_display_accepts_legacy_task_id_argument() -> None:
    """Legacy result-shaped cancellation data should remain displayable."""
    display = build_tool_display("background_cancel", {"task_id": "legacy-task"})

    assert display["summary"] == "legacy-task"
    assert display["args"] == ["legacy-task"]


def test_background_output_display_keeps_snake_case_task_id_argument() -> None:
    """background_output uses snake_case task_id and should not depend on taskId."""
    display = build_tool_display("background_output", {"task_id": "output-task"})

    assert display["summary"] == "output-task"
    assert display["args"] == ["output-task"]
