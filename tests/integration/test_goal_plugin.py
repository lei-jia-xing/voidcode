"""End-to-end integration tests for the Goal plugin.

Smoke test verifying goal creation, multi-step autonomous task execution, and state persistence across restarts.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.commands import CommandResult

_GOAL_PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent / "src" / "voidcode" / "plugins" / "goal"


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    """Close every core this test opened, also when the body raises."""
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _core_with_loopback(tmp_path: Path, server: OpenAILoopbackServer) -> Core:
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
            }
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
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
    with _loopback_server(replies) as server:
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
        _CREATED_CORES.remove(core)
        reopened = _core_with_loopback(tmp_path, server)
        res_after = _invoke(reopened, session_id, "goal", {})
        assert res_after.data["goal"] is not None
        assert res_after.data["goal"]["status"] == "complete"
        assert res_after.data["goal"]["tokens_used"] == res_goal.data["goal"]["tokens_used"]
