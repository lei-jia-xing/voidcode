"""End-to-end integration tests for the Goal plugin.

Smoke test verifying goal creation, multi-step autonomous task execution, and state persistence across restarts.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.core_factory import discard_core, loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import StagedStateUpdate
from voidcode.plugins.goal.goal_plugin import SCHEMA_VERSION
from voidcode.sdk.commands import CommandResult
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.resources import ResourceReadRequest, ResourceUnavailable

_GOAL_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "voidcode" / "plugins" / "goal"


def _core_with_loopback(tmp_path: Path, server: OpenAILoopbackServer, *, files_enabled: bool = True) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            "goal": {
                "path": str(_GOAL_PLUGIN_ROOT),
            },
            "files": {"enabled": files_enabled},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = register_core(open_core(workspace, state_dir=state_dir))
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: Mapping[str, object]) -> CommandResult:
    """Invoke a command bound to the revision the client just displayed."""
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def test_goal_autonomous_execution_and_completion(tmp_path: Path) -> None:
    """Test full loopback execution where model creates a goal, reads two integers from input.txt,
    computes the sum, writes sum.txt, reads back to verify, and marks the goal complete."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "input.txt").write_text("17 25", encoding="utf-8")

    # Turn 1: user says "start". Model creates goal via create_goal tool.
    call_create = {
        "id": "call-create-1",
        "type": "function",
        "function": {
            "name": "create_goal",
            "arguments": json.dumps({"objective": "Read input.txt, write sum to sum.txt, verify, and complete", "token_budget": 5000}),
        },
    }
    turn_1 = openai_response(
        message={"role": "assistant", "content": None, "tool_calls": [call_create]},
        finish="tool_calls",
        usage={"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
    )

    # Turn 2: model reads input.txt
    call_read_input = {
        "id": "call-read-1",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": json.dumps({"path": "input.txt"}),
        },
    }
    turn_2 = openai_response(
        message={"role": "assistant", "content": "Reading input.txt to extract numbers.", "tool_calls": [call_read_input]},
        finish="tool_calls",
        usage={"prompt_tokens": 70, "completion_tokens": 25, "total_tokens": 95},
    )

    # Turn 3: model writes sum.txt with "42"
    call_write_sum = {
        "id": "call-write-1",
        "type": "function",
        "function": {
            "name": "write",
            "arguments": json.dumps({"path": "sum.txt", "content": "42"}),
        },
    }
    turn_3 = openai_response(
        message={"role": "assistant", "content": "Writing computed sum 42 to sum.txt.", "tool_calls": [call_write_sum]},
        finish="tool_calls",
        usage={"prompt_tokens": 80, "completion_tokens": 30, "total_tokens": 110},
    )

    # Turn 4: model reads back sum.txt to verify
    call_verify = {
        "id": "call-read-verify",
        "type": "function",
        "function": {
            "name": "read",
            "arguments": json.dumps({"path": "sum.txt"}),
        },
    }
    turn_4 = openai_response(
        message={"role": "assistant", "content": "Verifying sum.txt content.", "tool_calls": [call_verify]},
        finish="tool_calls",
        usage={"prompt_tokens": 85, "completion_tokens": 20, "total_tokens": 105},
    )

    # Turn 5: model calls update_goal(status="complete")
    call_complete = {
        "id": "call-complete-1",
        "type": "function",
        "function": {
            "name": "update_goal",
            "arguments": json.dumps({"status": "complete"}),
        },
    }
    turn_5 = openai_response(
        message={"role": "assistant", "content": "Verified sum.txt contains 42. Marking goal complete.", "tool_calls": [call_complete]},
        finish="tool_calls",
        usage={"prompt_tokens": 90, "completion_tokens": 15, "total_tokens": 105},
    )

    # Turn 6: final model response concluding turn
    turn_6 = openai_response(
        message={"role": "assistant", "content": "Task completed successfully: sum 42 verified in sum.txt."},
        finish="stop",
        usage={"prompt_tokens": 40, "completion_tokens": 15, "total_tokens": 55},
    )

    replies = [turn_1, turn_2, turn_3, turn_4, turn_5, turn_6]
    with loopback_server(replies) as server:
        core = _core_with_loopback(tmp_path, server)
        session_id = core.create_session().session_id

        # Run user turn
        run_res = core.run("Start the automated pipeline", session_id=session_id)
        assert run_res.status == "completed"

        # Verify sum.txt exists and contains "42"
        sum_file = workspace / "sum.txt"
        assert sum_file.is_file()
        assert sum_file.read_text(encoding="utf-8") == "42"

        # Check goal state in session
        res_goal = _invoke(core, session_id, "goal", {})
        assert res_goal.data["goal"] is not None
        assert res_goal.data["goal"]["status"] == "complete"
        assert res_goal.data["goal"]["tokens_used"] > 0

        # Restart the core on the same state directory: goal state survives
        core.close()
        discard_core(core)
        reopened = _core_with_loopback(tmp_path, server)
        res_after = _invoke(reopened, session_id, "goal", {})
        assert res_after.data["goal"] is not None
        assert res_after.data["goal"]["status"] == "complete"
        assert res_after.data["goal"]["tokens_used"] == res_goal.data["goal"]["tokens_used"]


def test_goal_command_opens_create_then_details_and_prepared_edit_without_mutating_on_read(tmp_path: Path) -> None:
    from voidcode.sdk.views import FormViewData, MarkdownViewData, validate_form_submission

    # Creating a goal schedules autonomous work. Supply a bounded completion
    # whose measured usage exhausts this test goal rather than assuming no run.
    reply = openai_response(
        message={"role": "assistant", "content": "Goal work finished for this test."},
        finish="stop",
        usage={"prompt_tokens": 6000, "completion_tokens": 1, "total_tokens": 6001},
    )
    with loopback_server([reply]) as server:
        core = _core_with_loopback(tmp_path, server)
        session = core.create_session().session_id
        shown = _invoke(core, session, "goal", {})
        assert shown.status == "ok" and shown.presentation.view_type == "form"
        form = FormViewData.from_payload(shown.presentation.data)
        assert form.action == "goal.set" and form.initial_values == {}
        assert server.requests == []  # The read-only presentation did not run a model.
        args = validate_form_submission(form, {"objective": "Improve command UX", "token_budget": "5000"})
        assert _invoke(core, session, form.action, args).status == "ok"
        shown = _invoke(core, session, "goal", {})
        detail = MarkdownViewData.from_payload(shown.presentation.data)
        edit_action = next(action for action in detail.actions if action.action == "goal.edit")
        assert edit_action.prepare_input is True
        prepared = core.command_input(session, edit_action.action, expected_revision=core.command_catalog().revision_id)
        edit = FormViewData.from_payload(prepared.presentation.data)
        assert edit.action == "goal.edit"
        assert edit.initial_values["objective"] == "Improve command UX"
        assert edit.initial_values["token_budget"] == 5000
        committed = [
            event for event in core.events_after(session) if event.event_type == "core.command_completed" and event.payload.get("name") == "goal"
        ]
        assert len(committed) == 2
        result = CommandResult.from_payload(committed[-1].payload["result"])
        assert result.presentation == shown.presentation


def test_long_model_goal_round_trips_after_restart_without_workspace_writes(tmp_path: Path) -> None:
    objective = " \n" + "🎯 完全自动化端到端测试与真实验证。\n" * 5000 + "Final acceptance.\n\t"
    expected_uri = "artifact://" + hashlib.sha256(objective.encode("utf-8")).hexdigest()
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "create-long-goal",
                        "type": "function",
                        "function": {"name": "create_goal", "arguments": json.dumps({"objective": objective})},
                    }
                ],
            },
            finish="tool_calls",
        ),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "complete-long-goal",
                        "type": "function",
                        "function": {"name": "update_goal", "arguments": json.dumps({"status": "complete"})},
                    }
                ],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "long-goal-round-trip"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        workspace = tmp_path / "workspace"
        before = {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
        session = core.create_session().session_id
        result = core.run("Create the explicitly requested goal and mark this storage check complete.", session_id=session)
        assert result.status == "completed", result
        shown = _invoke(core, session, "goal", {})
        assert shown.data["goal"]["objective_file_ref"] == expected_uri
        loaded = _invoke(core, session, "goal.load_objective", {})
        assert loaded.status == "ok", loaded.error
        assert loaded.data["objective"] == objective
        assert {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before
        assert not (workspace / ".voidcode").exists()

        core.close()
        discard_core(core)
        reopened = _core_with_loopback(tmp_path, server)
        loaded = _invoke(reopened, session, "goal.load_objective", {})
        assert loaded.status == "ok", loaded.error
        assert loaded.data["objective"] == objective
        assert {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before


def test_goal_creation_and_completed_run_leave_one_pending_pursuit(tmp_path: Path) -> None:
    """One create call plus the run-finished auto-enqueue must leave one pending pursuit."""
    create_call = {
        "id": "call-create-once",
        "type": "function",
        "function": {"name": "create_goal", "arguments": json.dumps({"objective": "Keep pursuing this goal"})},
    }
    replies = [
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [create_call]},
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "Goal created."}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        session_id = core.create_session().session_id
        result = core.run("Create the explicitly requested goal.", session_id=session_id)
        assert result.status == "completed", result

        pending = core.store.pending_continuations(session_id, limit=10)
        assert len(pending) == 1
        assert pending[0].owner == "goal"
        assert pending[0].payload["action"] == "pursue_goal"
        shown = _invoke(core, session_id, "goal", {})
        assert shown.data["goal"]["status"] == "active"


def test_goal_state_schema_survives_hot_reload_with_live_and_cleared_state(tmp_path: Path) -> None:
    """Hot activation validates live goal state and the explicit cleared {} row."""
    with loopback_server([]) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        session_id = core.create_session().session_id

        created = _invoke(core, session_id, "goal.set", {"objective": "Survive hot reload", "token_budget": 10_000})
        assert created.status == "ok", created.error
        goal_id = created.data["goal"]["goal_id"]

        reloaded = _invoke(core, session_id, "core.reload", {})
        assert reloaded.status == "ok", reloaded.error
        after = _invoke(core, session_id, "goal", {})
        assert after.data["goal"]["goal_id"] == goal_id
        assert after.data["goal"]["status"] == "active"
        assert after.data["goal"]["objective"] == "Survive hot reload"

        cleared = _invoke(core, session_id, "goal.clear", {})
        assert cleared.status == "ok", cleared.error
        reloaded_cleared = _invoke(core, session_id, "core.reload", {})
        assert reloaded_cleared.status == "ok", reloaded_cleared.error
        assert _invoke(core, session_id, "goal", {}).data["goal"] is None


def test_completed_automatic_pursuit_run_queues_exactly_one_next_pursuit(tmp_path: Path) -> None:
    """A run claimed from the goal queue must still be able to queue its successor once."""
    create_call = {
        "id": "call-create-chain",
        "type": "function",
        "function": {"name": "create_goal", "arguments": json.dumps({"objective": "Keep going"})},
    }
    replies = [
        openai_response(message={"role": "assistant", "content": None, "tool_calls": [create_call]}, finish="tool_calls"),
        openai_response(message={"role": "assistant", "content": "Created."}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "Furthered the goal."}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        session_id = core.create_session().session_id
        first = core.run("Create the explicitly requested goal.", session_id=session_id)
        assert first.status == "completed", first
        assert len(core.store.pending_continuations(session_id, limit=10)) == 1

        drained = core.drain_continuations(session_id, limit=1)
        assert len(drained) == 1
        assert drained[0].owner == "goal"
        assert drained[0].run_status == "completed"
        pending = core.store.pending_continuations(session_id, limit=10)
        assert len(pending) == 1
        assert pending[0].payload["action"] == "pursue_goal"


def test_replacement_confirmation_survives_usage_state_churn(tmp_path: Path) -> None:
    reply = openai_response(
        message={"role": "assistant", "content": "usage-only turn"},
        finish="stop",
        usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
    )
    with loopback_server([reply]) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        session = core.create_session().session_id
        assert _invoke(core, session, "goal.set", {"objective": "first"}).status == "ok"
        pending = _invoke(core, session, "goal.set", {"objective": "second"})
        assert pending.status == "pending_confirmation" and pending.confirmation is not None
        assert pending.confirmation.expected_state_version is None
        before = _invoke(core, session, "goal", {}).data["goal"]["tokens_used"]

        # A real provider turn writes a usage fact and bumps the volatile state version.
        assert core.run("usage-only turn", session_id=session).status == "completed"
        after = _invoke(core, session, "goal", {}).data["goal"]
        assert after["tokens_used"] is not None and after["tokens_used"] > before

        allowed = core.confirm_action(
            session,
            pending.confirmation.request_id,
            "allow",
            expected_revision=core.command_catalog().revision_id,
        )
        assert allowed.status == "ok", allowed.error
        current = _invoke(core, session, "goal", {}).data["goal"]
        assert current["objective"] == "second"


@pytest.mark.parametrize("mutation", ["extra", "missing"], ids=["extra-field", "missing-field"])
def test_cold_start_refuses_schema_invalid_goal_state(tmp_path: Path, mutation: str) -> None:
    with loopback_server([]) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        session = core.create_session().session_id
        assert _invoke(core, session, "goal.set", {"objective": "cold-state"}).status == "ok"
        payload = json_wire_object(_invoke(core, session, "goal", {}).data["goal"])
        malformed = dict(payload)
        if mutation == "extra":
            malformed["unexpected_field"] = True
        else:
            del malformed["goal_id"]
        core.close()
        discard_core(core)

        with sqlite3.connect(tmp_path / "state" / "voidcode.db") as connection:
            connection.execute(
                "UPDATE plugin_state SET payload_json = ? WHERE scope = 'session' AND scope_key = ? AND owner = 'goal'",
                (json.dumps(malformed), session),
            )

        with pytest.raises(ValueError, match="declared state schema"):
            open_core(tmp_path / "workspace", state_dir=tmp_path / "state")


@pytest.mark.parametrize("mutation", ["extra", "missing"], ids=["extra-field", "missing-field"])
def test_hot_reload_refuses_schema_invalid_goal_state(tmp_path: Path, mutation: str) -> None:
    with loopback_server([]) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        session = core.create_session().session_id
        assert _invoke(core, session, "goal.set", {"objective": "hot-state"}).status == "ok"
        payload = json_wire_object(_invoke(core, session, "goal", {}).data["goal"])
        _stored, version = core.store.plugin_state("goal", "session", session)
        malformed = dict(payload)
        if mutation == "extra":
            malformed["unexpected_field"] = True
        else:
            del malformed["goal_id"]
        record = core.store.session(session)
        core.store.commit_staged(
            session,
            expected_sequence=record.last_event_sequence,
            expected_run_id=record.run_id,
            run_id=record.run_id or "hot-state-run",
            revision_id=core.revision_id,
            events=(),
            status=record.status,
            staged_state=(StagedStateUpdate("goal", "session", session, malformed, version, SCHEMA_VERSION, core.revision_id),),
        )
        reloaded = _invoke(core, session, "core.reload", {})
        assert reloaded.status == "error"
        assert "declared state schema" in (reloaded.error or "")


def test_long_objective_model_read_requires_files_plugin(tmp_path: Path) -> None:
    objective = "🎯 long objective\n" * 500
    assert len(objective) > 4000
    with loopback_server([]) as server:
        core = _core_with_loopback(tmp_path, server, files_enabled=False)
        core.auto_drain = False
        session = core.create_session().session_id
        created = _invoke(core, session, "goal.set", {"objective": objective})
        assert created.status == "ok", created.error
        state = _invoke(core, session, "goal", {}).data["goal"]
        ref = state["objective_file_ref"]
        assert isinstance(ref, str) and ref.startswith("artifact://")
        assert "read" not in core.plugin_runtime.tool_definitions
        context = ToolContext(
            workspace=core.workspace,
            session_id=session,
            run_id="resource-check",
            invocation_id="resource-check-invocation",
            revision_id=core.revision_id,
            actor="model",
        )
        with pytest.raises(ResourceUnavailable, match="artifact"):
            core.plugin_runtime.read_resource(ResourceReadRequest(uri=ref), context=context)

        # The user-facing exact loader remains available without files.
        loaded = _invoke(core, session, "goal.load_objective", {})
        assert loaded.status == "ok", loaded.error
        assert loaded.data["objective"] == objective


def test_pending_pursuit_after_restart_requires_user_run_or_explicit_drain(tmp_path: Path) -> None:
    pursuit_reply = openai_response(
        message={"role": "assistant", "content": "pursuit completed"},
        finish="stop",
        usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
    )
    with loopback_server([pursuit_reply]) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        session = core.create_session().session_id
        assert _invoke(core, session, "goal.set", {"objective": "durable pursuit"}).status == "ok"
        assert len(core.store.pending_continuations(session, limit=10)) == 1
        core.close()
        discard_core(core)

        reopened = _core_with_loopback(tmp_path, server)
        reopened.auto_drain = False
        assert len(reopened.store.pending_continuations(session, limit=10)) == 1
        assert server.requests == []
        assert _invoke(reopened, session, "goal", {}).status == "ok"
        assert len(reopened.store.pending_continuations(session, limit=10)) == 1
        assert server.requests == []

        drained = reopened.drain_continuations(session, limit=1)
        assert len(drained) == 1 and drained[0].run_status == "completed"
        pending = reopened.store.pending_continuations(session, limit=10)
        assert len(pending) == 1
        assert pending[0].payload["action"] == "pursue_goal"


def test_child_spend_does_not_change_parent_goal_counters(tmp_path: Path) -> None:
    child_reply = openai_response(
        message={"role": "assistant", "content": "child completed"},
        finish="stop",
        usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
    )
    with loopback_server([child_reply]) as server:
        core = _core_with_loopback(tmp_path, server)
        core.auto_drain = False
        parent = core.create_session().session_id
        assert _invoke(core, parent, "goal.set", {"objective": "parent goal", "token_budget": 1000}).status == "ok"
        before = _invoke(core, parent, "goal", {}).data["goal"]["tokens_used"]
        assert before == 0

        child = core.spawn_child(parent, "child work", notify_on_completion=False)
        outcome = core.join_child(parent, child, timeout_seconds=30)
        assert outcome.status == "completed", outcome.error
        child_usage = core.usage(child)
        assert child_usage.input_tokens == 40 and child_usage.output_tokens == 10

        after = _invoke(core, parent, "goal", {}).data["goal"]["tokens_used"]
        assert after == before == 0
        parent_usage = core.usage(parent)
        assert parent_usage.input_tokens == 0 and parent_usage.output_tokens == 0
