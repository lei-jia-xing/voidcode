"""End-to-end integration tests for the Todo plugin on the real kernel stack.

Drives the real kernel, plugin host, loopback provider and durable storage:
- A model tool call writes the plan; the tool result body and the next provider request show it
- Plan truth is contributed to the provider context only while active tasks remain
- Plugin state survives a kernel restart on the same state directory
- Disabling the plugin removes its tool, context and commands without breaking ordinary chat
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel
from voidcode.sdk.commands import CommandResult

_TODO_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "todo"


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED_KERNELS: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_created_kernels() -> Iterator[None]:
    yield
    while _CREATED_KERNELS:
        _CREATED_KERNELS.pop().close()


def _kernel(tmp_path: Path, server: OpenAILoopbackServer, *, todo_enabled: bool, state_dir: Path | None = None) -> Kernel:
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
    kernel = open_kernel(workspace, state_dir=resolved_state, trusted_plugins=frozenset({"todo"}))
    _CREATED_KERNELS.append(kernel)
    return kernel


def _invoke(kernel: Kernel, session_id: str, name: str, arguments: dict[str, object]) -> CommandResult:
    return kernel.invoke_command(session_id, name, arguments, expected_revision=kernel.command_catalog().revision_id)


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
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, todo_enabled=True)
        session_id = kernel.create_session().session_id

        result = kernel.run("plan the build", session_id=session_id)
        assert result.status == "completed"
        assert result.output == "Plan recorded."

        # The tool was advertised to the provider under its declared name.
        assert "todo" in _tool_names(server.requests[0])

        # The tool result the model received carries the durable body, not just prose.
        tool_messages = [message for message in _messages(server.requests[1]) if message.get("role") == "tool"]
        assert len(tool_messages) == 1
        tool_text = str(tool_messages[0]["content"])
        assert '"mutated": true' in tool_text
        assert tool_text.endswith("Todo init applied.\nBuild:\n- [in_progress] compile\n- [pending] test")

        # The next provider dispatch carries the plan truth while active tasks remain.
        second_request_text = _request_text(server.requests[1])
        assert "Runtime-managed todo state is active for this session." in second_request_text
        assert "- [in_progress] compile" in second_request_text

        # Durable plugin state and its namespaced event are visible to the kernel.
        events = kernel.events_after(session_id)
        todo_events = [event for event in events if event.event_type == "plugin.todo.todo_updated"]
        assert len(todo_events) == 1
        assert todo_events[0].source == "plugin.todo"
        assert todo_events[0].payload["active_count"] == 2
        assert todo_events[0].payload["in_progress_count"] == 1
        assert todo_events[0].payload["pending_count"] == 1

        status = _invoke(kernel, session_id, "todo", {})
        assert status.status == "ok"
        assert status.output == "Build:\n- [in_progress] compile\n- [pending] test"

        # Restart the kernel on the same state directory: the plan is still there.
        kernel.close()
        _CREATED_KERNELS.remove(kernel)
        reopened = _kernel(tmp_path, server, todo_enabled=True)
        after_restart = _invoke(reopened, session_id, "todo", {})
        assert after_restart.status == "ok"
        assert after_restart.output == status.output
        assert after_restart.data["revision"] == 1


def test_finished_plan_drops_plan_truth_and_keeps_state(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _todo_call("call-init-1", {"op": "init", "items": ["only task"]}),
                ],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 30, "completion_tokens": 10, "total_tokens": 40},
        ),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_todo_call("call-done-1", {"op": "done", "task": "only task"})],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50},
        ),
        openai_response(
            message={"role": "assistant", "content": "All done."},
            finish="stop",
            usage={"prompt_tokens": 50, "completion_tokens": 8, "total_tokens": 58},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, todo_enabled=True)
        session_id = kernel.create_session().session_id
        result = kernel.run("finish the only task", session_id=session_id)
        assert result.status == "completed"

        assert "Runtime-managed todo state is active for this session." in _request_text(server.requests[1])
        final_request_text = _request_text(server.requests[2])
        assert "Runtime-managed todo state is active for this session." not in final_request_text

        status = _invoke(kernel, session_id, "todo", {})
        assert status.output == "Tasks:\n- [completed] only task"


def test_chatting_turn_without_todos_contributes_no_plan_context(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "hello"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
        )
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, todo_enabled=True)
        session_id = kernel.create_session().session_id
        assert kernel.run("hi", session_id=session_id).status == "completed"
        assert "Runtime-managed todo state is active for this session." not in _request_text(server.requests[0])
        assert _invoke(kernel, session_id, "todo", {}).output == "Todo list is empty."


def test_disabled_plugin_removes_tool_context_and_commands(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": "ordinary answer"},
            finish="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, todo_enabled=False)
        session_id = kernel.create_session().session_id
        result = kernel.run("just chat", session_id=session_id)
        assert result.status == "completed"
        assert result.output == "ordinary answer"
        assert "todo" not in _tool_names(server.requests[0])
        names = [descriptor.name for descriptor in kernel.command_catalog().commands]
        assert "todo" not in names
        assert "todo.clear" not in names


def test_clear_command_resets_the_list_on_the_real_stack(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [_todo_call("call-init-1", {"op": "init", "items": ["a", "b"]})]},
            finish="tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
        openai_response(
            message={"role": "assistant", "content": "recorded"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, todo_enabled=True)
        session_id = kernel.create_session().session_id
        assert kernel.run("plan", session_id=session_id).status == "completed"

        cleared = _invoke(kernel, session_id, "todo.clear", {})
        assert cleared.status == "ok"
        assert cleared.output == "Todo list cleared."
        assert _invoke(kernel, session_id, "todo", {}).output == "Todo list is empty."
        assert _invoke(kernel, session_id, "todo.clear", {}).output == "No todo list to clear."

        # The next provider dispatch no longer carries plan truth (the reminder continuation that
        # auto-drain fired for the still-incomplete list is not visible here: the list is cleared).
        assert kernel.run("after clearing", session_id=session_id) is not None
        assert "Runtime-managed todo state is active for this session." not in _request_text(server.requests[-1])


def test_state_schema_version_is_declared_for_the_owner(tmp_path: Path) -> None:
    """The durable row records the plugin's declared state schema version."""
    replies: list[dict[str, object]] = [
        openai_response(
            message={"role": "assistant", "content": None, "tool_calls": [_todo_call("call-init-1", {"op": "init", "items": ["x"]})]},
            finish="tool_calls",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
        openai_response(
            message={"role": "assistant", "content": "ok"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 5, "total_tokens": 25},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, todo_enabled=True)
        session_id = kernel.create_session().session_id
        kernel.run("plan", session_id=session_id)
        stored = kernel.store.plugin_state("todo", "session", session_id)
        assert stored is not None
        payload, version = stored
        assert payload is not None
        assert payload["schema_version"] == 2
        assert version >= 1


def test_completion_reminder_runs_as_a_user_role_continuation(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_todo_call("call-init-1", {"op": "init", "items": ["one", "two"]})],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 25, "completion_tokens": 10, "total_tokens": 35},
        ),
        openai_response(
            message={"role": "assistant", "content": "I stopped early."},
            finish="stop",
            usage={"prompt_tokens": 35, "completion_tokens": 6, "total_tokens": 41},
        ),
        openai_response(
            message={"role": "assistant", "content": "Reconciled the list."},
            finish="stop",
            usage={"prompt_tokens": 45, "completion_tokens": 6, "total_tokens": 51},
        ),
    ]
    with _loopback_server(replies) as server:
        kernel = _kernel(tmp_path, server, todo_enabled=True)
        session_id = kernel.create_session().session_id
        result = kernel.run("plan then stop", session_id=session_id)
        assert result.status == "completed"

        # auto_drain already claimed and ran the reminder continuation as its own governed run.
        assert kernel.drain_continuations(session_id, limit=1) == ()
        assert len(server.requests) == 3

        # The continuation turn carried the verbatim legacy reminder as a USER message.
        continuation_messages = _messages(server.requests[2])
        user_messages = [message for message in continuation_messages if message.get("role") == "user"]
        assert len(user_messages) == 2
        reminder = str(user_messages[-1]["content"])
        assert reminder == (
            "<system-reminder>\n"
            "You stopped with 2 incomplete todo item(s):\n"
            "- Tasks\n"
            "  - one\n"
            "  - two\n"
            "\n"
            "Please continue working on these tasks or mark them complete if finished.\n"
            "(Reminder 1/3)\n"
            "</system-reminder>"
        )

        # Exactly one continuation was enqueued, the injection is a durable fact, and the
        # reminder budget advanced.
        event_types = [event.event_type for event in kernel.events_after(session_id)]
        assert event_types.count("kernel.continuation_started") == 1
        assert "plugin.todo.reminder_injected" in event_types
        stored = kernel.store.plugin_state("todo", "session", session_id)
        assert stored is not None
        payload, _version = stored
        assert payload is not None
        assert payload["reminders"]["todo"]["attempts"] == 1

        # Draining again finds nothing: the reminder is consumed exactly once.
        assert kernel.drain_continuations(session_id, limit=1) == ()
