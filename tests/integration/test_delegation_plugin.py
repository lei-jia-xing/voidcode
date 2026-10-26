"""End-to-end integration test for the Delegation plugin on the real core stack.

Drives the real core, plugin runtime, loopback provider and durable storage: a parent run whose
model calls ``task(prompt=..., run_in_background=false)`` spawns a real child session, the child's
run completes against the same provider, and its outcome comes back inside the parent's tool
result with ``kernel.child_spawned``/``kernel.child_finished`` facts on the parent session.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.core.engine import Core
from voidcode.plugins.delegation.profiles.builtin import EXPLORE_AGENT

_DELEGATION_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "delegation"
_TODO_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "todo"
_EXPLORE_PROMPT_BODY = EXPLORE_AGENT.prompt


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


_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


def _admitted_source(tmp_path: Path) -> Path:
    """The real delegation plugin root, referenced by path."""
    _ = tmp_path
    return _DELEGATION_PLUGIN_ROOT


def _core(tmp_path: Path, server: OpenAILoopbackServer, *, minimal: bool = False) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        # Spawning is an external mutation: approval_mode "ask" would park the parent on an
        # approval request instead of running the delegated child.
        "policy": {"approval_mode": "yolo"},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            # Explicitly out of the picture: this suite proves the delegation plugin alone.
            "todo": {"path": str(_TODO_PLUGIN_ROOT), "enabled": False},
            "delegation": {"path": str(_admitted_source(tmp_path)), "enabled": True},
        },
    }
    if minimal:
        config["plugins"] = {
            **{name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS},
            "providers": {"enabled": True},
            "files": {"enabled": True},
            "delegation": {"path": str(_admitted_source(tmp_path)), "enabled": True},
        }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core


def _task_call(call_id: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "task", "arguments": json.dumps(arguments)},
    }


def _tool_names(request: dict[str, object]) -> list[str]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    return [str(entry["function"]["name"]) for entry in tools if isinstance(entry, dict) and isinstance(entry.get("function"), dict)]  # type: ignore[union-attr]


def _messages(request: dict[str, object]) -> list[dict[str, object]]:
    messages = request.get("messages")
    return [entry for entry in messages if isinstance(entry, dict)] if isinstance(messages, list) else []


def test_parent_spawns_a_real_child_and_receives_its_output(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        # Parent: delegate the work and wait for the child in the same call.
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_task_call("call-task-1", {"prompt": "count the visitor guide doors", "label": "doors", "run_in_background": False})],
            },
            finish="tool_calls",
            usage={"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
        ),
        # Child: one turn, one answer.
        openai_response(
            message={"role": "assistant", "content": "child answer: 12 doors"},
            finish="stop",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
        # Parent: the tool result is in context, so the run can finish.
        openai_response(
            message={"role": "assistant", "content": "delegation complete"},
            finish="stop",
            usage={"prompt_tokens": 80, "completion_tokens": 6, "total_tokens": 86},
        ),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id
        result = core.run("count the doors", session_id=parent_id)
        assert result.status == "completed"
        assert result.output == "delegation complete"

        # The real child session ran the delegated prompt itself.
        assert len(server.requests) == 3
        assert "task" in _tool_names(server.requests[0])
        child_prompt = _messages(server.requests[1])[-1]
        assert child_prompt["role"] == "user"
        assert child_prompt["content"] == "count the visitor guide doors"

        # The child's own output came back inside the parent's tool result.
        tool_messages = [message for message in _messages(server.requests[2]) if message.get("role") == "tool"]
        assert len(tool_messages) == 1
        tool_text = str(tool_messages[0]["content"])
        body = json.loads(tool_text.splitlines()[0])
        assert body["operation"] == "spawn"
        assert body["label"] == "doors"
        assert body["status"] == "completed"
        assert body["output"] == "child answer: 12 doors"
        assert "child answer: 12 doors" in tool_text
        child_id = str(body["task_id"])

        # A real child session is linked to the parent, and its run left a durable terminal fact.
        child = core.session(child_id)
        assert child.parent_session_id == parent_id
        assert child.status == "completed"

        parent_events = core.events_after(parent_id)
        spawned = [event for event in parent_events if event.event_type == "core.child_spawned"]
        finished = [event for event in parent_events if event.event_type == "core.child_finished"]
        assert len(spawned) == 1
        assert spawned[0].payload["child_session_id"] == child_id
        assert spawned[0].payload["label"] == "doors"
        assert spawned[0].payload["depth"] == 1
        assert len(finished) == 1
        assert finished[0].payload["child_session_id"] == child_id
        assert finished[0].payload["status"] == "completed"
        assert finished[0].payload["output"] == "child answer: 12 doors"

        # The plugin's own session state records the child it started.
        stored = core.store.plugin_state("delegation", "session", parent_id)
        assert stored is not None
        payload, version = stored
        assert payload is not None
        assert payload["schema_version"] == 1
        children = payload["children"]
        assert isinstance(children, Mapping)
        assert list(children) == [child_id]
        assert version >= 1


@pytest.mark.parametrize("scope", [None, ["read"]])
def test_profile_child_inherits_parent_scope_or_uses_explicit_narrowing(tmp_path: Path, scope) -> None:
    """Disabling the files adapter keeps baseline discovery available to a child."""
    replies: list[dict[str, object]] = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _task_call(
                        "call-task-1",
                        {
                            "prompt": "summarize the visitor guide",
                            "subagent_type": "explore",
                            "run_in_background": False,
                            **({"tool_allowlist": scope} if scope is not None else {}),
                        },
                    )
                ],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "child answer: explored"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "profile delegation complete"}, finish="stop"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, minimal=True)
        parent_id = core.create_session("parent").session_id
        result = core.run("explore the visitor guide", session_id=parent_id)

        assert result.status == "completed", result.error
        assert len(server.requests) == 3
        child_prompt = _messages(server.requests[1])[-1]
        assert child_prompt["content"] == f"{_EXPLORE_PROMPT_BODY}\n\nsummarize the visitor guide"

        tool_text = str([message for message in _messages(server.requests[2]) if message.get("role") == "tool"][0]["content"])
        body = json.loads(tool_text.splitlines()[0])
        assert body["subagent_type"] == "explore"
        assert body["status"] == "completed"
        assert body["output"] == "child answer: explored"
        assert "notes" not in body
        assert set(scope or ["read", "glob", "grep"]) <= set(_tool_names(server.requests[1]))
        assert "write" not in _tool_names(server.requests[1])
        assert "profile=explore" in tool_text

        child_id = str(body["task_id"])
        child = core.session(child_id)
        assert child.parent_session_id == parent_id
        assert child.tool_allowlist == (tuple(scope) if scope is not None else core.session(parent_id).tool_allowlist)
        assert core.store.session_overlays(child_id) == ("read-only",), "a read-only profile keeps its stance"

        stored = core.store.plugin_state("delegation", "session", parent_id)
        assert stored is not None
        payload, _version = stored
        children = payload["children"]
        assert isinstance(children, Mapping)
        assert children[child_id]["subagent_type"] == "explore"


def test_standard_markdown_agent_applies_its_prompt_tools_and_turn_cap(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_task_call("custom", {"prompt": "inspect", "subagent_type": "inspector", "run_in_background": False})],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "inspection complete"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "done"}, finish="stop"),
    ]
    with _loopback_server(replies) as server:
        core = _core(tmp_path, server, minimal=True)
        directory = core.workspace / ".claude" / "agents"
        directory.mkdir(parents=True)
        (directory / "different-filename.md").write_text(
            "---\nname: inspector\ndescription: Inspect files\ntools: Read, Grep\nmodel: inherit\nmaxTurns: 1\n---\nInspect carefully.\n"
        )
        parent = core.create_session().session_id
        result = core.run("delegate inspection", session_id=parent)
        assert result.status == "completed", result.error
        child_id = next(event.payload["child_session_id"] for event in core.events_after(parent) if event.event_type == "core.child_spawned")
        child = core.session(child_id)
        assert child.status == "completed" and child.tool_allowlist == ("grep", "read")
        assert set(_tool_names(server.requests[1])) == {"grep", "read"}
        assert any(message.get("content") == "Inspect carefully.\n\ninspect" for message in _messages(server.requests[1]))
        assert len(server.requests) == 3
