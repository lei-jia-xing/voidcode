"""End-to-end integration tests for the Todo plugin on the real core stack.

Smoke test verifying model tool invocation, plan context injection, and state persistence across restarts.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.fixtures.core_factory import loopback_server
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.commands import CommandResult

_TODO_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "todo"


_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _core(tmp_path: Path, server: OpenAILoopbackServer, *, todo_enabled: bool, state_dir: Path | None = None) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    resolved_state = state_dir if state_dir is not None else tmp_path / "state"
    resolved_state.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": "ask"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4
            # lane; booting it would make this suite depend on their in-flight tree.
            "files": {"enabled": False},
            "todo": {"path": str(_TODO_PLUGIN_ROOT), "enabled": todo_enabled},
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=resolved_state)
    _CREATED_CORES.append(core)
    return core


def _invoke(core: Core, session_id: str, name: str, arguments: dict[str, object]) -> CommandResult:
    return core.invoke_command(session_id, name, arguments, expected_revision=core.command_catalog().revision_id)


def _todo_call(call_id: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "todo", "arguments": json.dumps(arguments)},
    }


def _tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    return [str(entry["function"]["name"]) for entry in tools if isinstance(entry, dict) and isinstance(entry.get("function"), dict)]  # type: ignore[union-attr]


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request.get("messages")
    return [entry for entry in messages if isinstance(entry, dict)] if isinstance(messages, list) else []


def _request_text(request: dict[str, object]) -> str:
    return json.dumps(_messages(request), ensure_ascii=False)


def test_todo_tool_call_writes_plan_context_and_survives_restart(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_todo_call("call-init-1", {"op": "init", "list": [{"phase": "Build", "items": ["compile", "test"]}]})],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 40, "completion_tokens": 20, "total_tokens": 60},
        ),
        openai_response(
            message={"role": "assistant", "content": "Plan recorded."},
            finish="stop",
            usage={"prompt_tokens": 60, "completion_tokens": 10, "total_tokens": 70},
        ),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, todo_enabled=True)
        session_id = core.create_session().session_id

        result = core.run("plan the build", session_id=session_id)
        assert result.status == "completed"
        assert result.output == "Plan recorded."

        # The tool was advertised to the provider under its declared name.
        assert "todo" in _tool_names(server.requests[0])

        # The tool result the model received references the durable body and carries formatted prose.
        tool_messages = [message for message in _messages(server.requests[1]) if message.get("role") == "tool"]
        assert len(tool_messages) == 1
        tool_payload = json.loads(str(tool_messages[0]["content"]))
        assert tool_payload.get("body_omitted") is True
        assert str(tool_payload.get("text", "")).endswith("Todo init applied.\nBuild:\n- [in_progress] compile\n- [pending] test")
        uri = str(tool_payload.get("result_uri", ""))
        assert uri.startswith("artifact://")
        digest = uri.removeprefix("artifact://")
        artifact_page = core.read_artifact(digest)
        durable_result = decode_tool_result(json.loads(artifact_page.data.decode("utf-8")))
        assert durable_result.body is not None
        assert durable_result.body.as_payload().get("mutated") is True

        # The next provider dispatch carries the plan truth while active tasks remain.
        second_request_text = _request_text(server.requests[1])
        assert "Runtime-managed todo state is active for this session." in second_request_text
        assert "- [in_progress] compile" in second_request_text

        # Durable plugin state and its namespaced event are visible to the core.
        events = core.events_after(session_id)
        todo_events = [event for event in events if event.event_type == "plugin.todo.todo_updated"]
        assert len(todo_events) == 1
        assert todo_events[0].source == "plugin.todo"
        assert todo_events[0].payload["active_count"] == 2
        assert todo_events[0].payload["in_progress_count"] == 1
        assert todo_events[0].payload["pending_count"] == 1

        status = _invoke(core, session_id, "todo", {})
        assert status.status == "ok"
        assert status.output == "Build:\n- [in_progress] compile\n- [pending] test"

        # Restart the core on the same state directory: the plan is still there.
        core.close()
        _CREATED_CORES.remove(core)
        reopened = _core(tmp_path, server, todo_enabled=True)
        after_restart = _invoke(reopened, session_id, "todo", {})
        assert after_restart.status == "ok"
        assert after_restart.output == status.output
        assert after_restart.data["revision"] == 1
