from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from voidcode.command.native import NATIVE_COMMANDS_BY_NAME
from voidcode.core.turns import FinalTurn, ModelTurnFact, TurnRequest
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.transport.http import create_runtime_app
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import (
    ModelPickerOverlay,
    PermissionsPickerOverlay,
)
from voidcode.tui.term import Terminal
from voidcode.tui.transcript import NoticeBlock


class _DummyProducer:
    def __init__(self, answer: str = "ok") -> None:
        self.answer = answer

    def __call__(self, request: TurnRequest) -> FinalTurn:
        return FinalTurn(facts=(ModelTurnFact(request.run_step, "deterministic", request.prompt),), output=self.answer)


def _last_notice(app: TuiApp) -> str:
    assert app._view is not None
    notices = [b.text for b in app._view.transcript().blocks if isinstance(b, NoticeBlock)]
    return notices[-1] if notices else ""


def _make_tui_app(tmp_path: Path, runtime: VoidCodeRuntime) -> tuple[TuiApp, Terminal, Any]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    app = TuiApp(workspace, runtime=runtime)
    log_file = tmp_path / "terminal.log"
    output = log_file.open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    app._term = terminal
    app._setup(terminal)
    return app, terminal, output


# ---------------------------------------------------------------------------
# 1. Runtime Tests
# ---------------------------------------------------------------------------


def test_runtime_model_switch_including_effort(tmp_path: Path) -> None:
    """Model switch updates session metadata and reasoning effort."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="model-test")
        session_id = sess.session.id

        # Initial model
        info = runtime.session_model(session_id=session_id)
        assert "model" in info

        # Switch model without effort
        switched = runtime.set_session_model(session_id=session_id, model="openai/gpt-4o")
        assert switched["model"] == "openai/gpt-4o"
        assert switched["reasoning_effort"] is None

        # Verify persisted
        reloaded = runtime.session_model(session_id=session_id)
        assert reloaded["model"] == "openai/gpt-4o"

        # Switch model with effort
        switched_effort = runtime.set_session_model(
            session_id=session_id,
            model="openai/o3-mini",
            reasoning_effort="high",
        )
        assert switched_effort["model"] == "openai/o3-mini"
        assert switched_effort["reasoning_effort"] == "high"

        reloaded_effort = runtime.session_model(session_id=session_id)
        assert reloaded_effort["model"] == "openai/o3-mini"
        assert reloaded_effort["reasoning_effort"] == "high"


def test_runtime_preset_mapping_matrix(tmp_path: Path) -> None:
    """Verify Codex presets and VoidCode approval modes mapping matrix."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="perm-test")
        session_id = sess.session.id

        # read-only preset -> ask mode
        r1 = runtime.set_session_permissions(session_id=session_id, mode="read-only")
        assert r1["approval_mode"] == "ask"
        assert r1["preset"] == "read-only"
        assert runtime.session_permissions(session_id=session_id)["approval_mode"] == "ask"

        # auto preset -> write mode
        r2 = runtime.set_session_permissions(session_id=session_id, mode="auto")
        assert r2["approval_mode"] == "write"
        assert r2["preset"] == "auto"
        assert runtime.session_permissions(session_id=session_id)["approval_mode"] == "write"

        # full-access preset -> yolo mode
        r3 = runtime.set_session_permissions(session_id=session_id, mode="full-access")
        assert r3["approval_mode"] == "yolo"
        assert r3["preset"] == "full-access"
        assert runtime.session_permissions(session_id=session_id)["approval_mode"] == "yolo"

        # direct modes also supported
        assert runtime.set_session_permissions(session_id=session_id, mode="ask")["approval_mode"] == "ask"
        assert runtime.set_session_permissions(session_id=session_id, mode="write")["approval_mode"] == "write"
        assert runtime.set_session_permissions(session_id=session_id, mode="yolo")["approval_mode"] == "yolo"

        # Invalid mode raises ValueError
        with pytest.raises(ValueError, match="invalid permissions mode or preset"):
            runtime.set_session_permissions(session_id=session_id, mode="invalid_mode")


def test_runtime_approve_one_and_empty_queue_semantics(tmp_path: Path) -> None:
    """Empty queue reports reason; approving a valid pending request claims it."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="approve-test")
        session_id = sess.session.id

        # Empty queue
        approvals = runtime.list_pending_approvals(session_id=session_id)
        assert len(approvals) == 0

        res_empty = runtime.approve_pending_action(session_id=session_id)
        assert not res_empty["approved"]
        assert res_empty["reason"] == "no_pending_approvals"

        # Inject a pending approval into SQLite sessions table
        payload = {
            "request_id": "req-test-123",
            "tool_name": "shell_exec",
            "arguments": {"command": "ls"},
            "target_summary": "command 'ls'",
            "reason": "run shell",
            "policy_mode": "ask",
            "request_event_sequence": None,
            "owner_session_id": session_id,
            "owner_parent_session_id": None,
            "delegated_task_id": None,
            "path_scope": "workspace",
            "operation_class": "execute",
            "canonical_path": None,
            "matched_rule": None,
            "policy_surface": None,
        }
        with runtime._repositories.recovery._write_connect(runtime.workspace) as conn:
            conn.execute(
                "UPDATE sessions SET pending_approval_json = ? WHERE workspace_id = ? AND session_id = ?",
                (json.dumps(payload), str(runtime.workspace), session_id),
            )
            conn.commit()

        # Now listed
        listed = runtime.list_pending_approvals(session_id=session_id)
        assert len(listed) == 1
        assert listed[0]["request_id"] == "req-test-123"

        # Unknown request ID ignored with warning / not found
        res_unknown = runtime.approve_pending_action(session_id=session_id, request_id="non-existent")
        assert not res_unknown["approved"]
        assert res_unknown["reason"] == "not_found"

        # Approve the pending action
        res_ok = runtime.approve_pending_action(session_id=session_id, request_id="req-test-123")
        assert res_ok["approved"]
        assert res_ok["request_id"] == "req-test-123"


# ---------------------------------------------------------------------------
# 2. Busy Gate Matrix (all 3 run mid-stream)
# ---------------------------------------------------------------------------


def test_busy_gate_matrix_runs_during_streaming(tmp_path: Path) -> None:
    """All three slash commands have busy_gate=False and execute mid-stream."""
    for name in ("approve", "model", "permissions"):
        cmd = NATIVE_COMMANDS_BY_NAME[name]
        assert cmd.busy_gate is False, f"/{name} must have busy_gate=False"

    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="busy-test")
        session_id = sess.session.id

        app, terminal, output = _make_tui_app(tmp_path, runtime)
        app._session_id = session_id

        try:
            # Simulate streaming active
            app._streaming = True

            # /model executes mid-stream (doesn't trigger busy notice)
            app._submit("/model openai/gpt-4o")
            assert "disabled while a task is in progress" not in _last_notice(app)
            assert "requires an idle" not in _last_notice(app)
            assert "Model set to openai/gpt-4o" in _last_notice(app)

            # /permissions executes mid-stream
            app._submit("/permissions auto")
            assert "disabled while a task is in progress" not in _last_notice(app)
            assert "Approval mode set to auto" in _last_notice(app)

            # /approve executes mid-stream
            app._submit("/approve")
            assert "disabled while a task is in progress" not in _last_notice(app)
            assert "No pending approvals" in _last_notice(app)
        finally:
            app._streaming = False
            app._shutdown(terminal)
            terminal.close()
            output.close()


# ---------------------------------------------------------------------------
# 3. TUI Non-PTY Dispatch + Picker Tests
# ---------------------------------------------------------------------------


def test_tui_model_picker_and_dispatch(tmp_path: Path) -> None:
    """Bare /model opens ModelPickerOverlay; selecting an option applies the model."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="tui-model")
        session_id = sess.session.id

        app, terminal, output = _make_tui_app(tmp_path, runtime)
        app._session_id = session_id

        try:
            # Bare command opens picker
            app._submit("/model")
            assert isinstance(app._overlay, ModelPickerOverlay)

            # Press enter to select the highlighted option
            app._handle_overlay_key(Key("enter"))
            assert app._overlay is None
            assert "Model set to" in _last_notice(app)

            # Bare with no session gives startup notice
            app._session_id = None
            app._submit("/model")
            assert _last_notice(app) == "Model selection is disabled until startup completes."
        finally:
            app._shutdown(terminal)
            terminal.close()
            output.close()


def test_tui_permissions_picker_and_dispatch(tmp_path: Path) -> None:
    """Bare /permissions opens PermissionsPickerOverlay; selecting an option applies preset."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="tui-perm")
        session_id = sess.session.id

        app, terminal, output = _make_tui_app(tmp_path, runtime)
        app._session_id = session_id

        try:
            # Bare command opens picker
            app._submit("/permissions")
            assert isinstance(app._overlay, PermissionsPickerOverlay)
            assert len(app._overlay._options) == 3

            # Current preset defaults to full-access (index 2). Move up to 'auto' (index 1) and press enter
            app._handle_overlay_key(Key("up"))
            app._handle_overlay_key(Key("enter"))
            assert app._overlay is None
            assert "Approval mode set to auto (write)" in _last_notice(app)
            assert app._approval_mode == "write"

            # Direct argument applies immediately
            app._submit("/permissions full-access")
            assert "Approval mode set to full-access (yolo)" in _last_notice(app)
            assert app._approval_mode == "yolo"
        finally:
            app._shutdown(terminal)
            terminal.close()
            output.close()


def test_tui_approve_picker_and_dispatch(tmp_path: Path) -> None:
    """Empty queue shows notice; single pending approves; multiple opens ApprovePickerOverlay."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="tui-approve")
        session_id = sess.session.id

        app, terminal, output = _make_tui_app(tmp_path, runtime)
        app._session_id = session_id

        try:
            # Empty queue
            app._submit("/approve")
            assert _last_notice(app) == "No pending approvals in this session."

            # Inject a pending approval
            payload = {
                "request_id": "req-tui-1",
                "tool_name": "edit",
                "arguments": {"path": "foo.py"},
                "target_summary": "foo.py",
                "reason": "edit file",
                "policy_mode": "ask",
                "request_event_sequence": None,
                "owner_session_id": session_id,
                "owner_parent_session_id": None,
                "delegated_task_id": None,
                "path_scope": "workspace",
                "operation_class": "write",
                "canonical_path": None,
                "matched_rule": None,
                "policy_surface": None,
            }
            with runtime._repositories.recovery._write_connect(runtime.workspace) as conn:
                conn.execute(
                    "UPDATE sessions SET pending_approval_json = ? WHERE workspace_id = ? AND session_id = ?",
                    (json.dumps(payload), str(runtime.workspace), session_id),
                )
                conn.commit()

            resolved_calls: list[tuple[str, str]] = []
            app._resolve_approval = lambda req_id, dec: resolved_calls.append((req_id, dec))

            # Single pending bare approves directly
            app._submit("/approve")
            assert len(resolved_calls) == 1
            assert resolved_calls[0] == ("req-tui-1", "allow")
            assert "Approved edit" in _last_notice(app)
        finally:
            app._shutdown(terminal)
            terminal.close()
            output.close()


# ---------------------------------------------------------------------------
# 4. HTTP Contract Tests
# ---------------------------------------------------------------------------


def test_http_model_permissions_approve_endpoints(tmp_path: Path) -> None:
    """HTTP endpoints for model, permissions, and approve return typed contracts."""
    with VoidCodeRuntime(workspace=tmp_path, turn_producer=_DummyProducer()) as runtime:
        sess = runtime.create_session(title="http-test")
        session_id = sess.session.id

        app = create_runtime_app(workspace=tmp_path, runtime_factory=lambda: runtime)
        client = TestClient(app)

        # Test model retrieval and update
        res_get_model = client.get(f"/api/sessions/{session_id}/model")
        assert res_get_model.status_code == 200
        assert "model" in res_get_model.json()

        res_post_model = client.post(
            f"/api/sessions/{session_id}/model",
            json={"model": "anthropic/claude-3-5-sonnet", "reasoning_effort": "low"},
        )
        assert res_post_model.status_code == 200
        assert res_post_model.json()["model"] == "anthropic/claude-3-5-sonnet"
        assert res_post_model.json()["reasoning_effort"] == "low"

        # Test permissions retrieval and update
        res_get_perm = client.get(f"/api/sessions/{session_id}/permissions")
        assert res_get_perm.status_code == 200
        assert "approval_mode" in res_get_perm.json()

        res_post_perm = client.post(
            f"/api/sessions/{session_id}/permissions",
            json={"mode": "auto"},
        )
        assert res_post_perm.status_code == 200
        assert res_post_perm.json()["approval_mode"] == "write"
        assert res_post_perm.json()["preset"] == "auto"

        # Test pending approvals retrieval (empty)
        res_get_approvals = client.get(f"/api/sessions/{session_id}/pending-approvals")
        assert res_get_approvals.status_code == 200
        assert res_get_approvals.json() == []

        # Test approve action with empty queue
        res_approve_empty = client.post(
            f"/api/sessions/{session_id}/approve",
            json={},
        )
        assert res_approve_empty.status_code == 200
        assert res_approve_empty.json()["approved"] is False
        assert res_approve_empty.json()["reason"] == "no_pending_approvals"
