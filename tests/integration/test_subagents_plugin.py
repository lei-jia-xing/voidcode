"""End-to-end integration test for the Subagents plugin on the real core stack.

Drives the real core, plugin runtime, loopback provider and durable storage: a parent run whose
model calls ``task(prompt=..., run_in_background=false)`` spawns a real child session, the child's
run completes against the same provider, and its outcome comes back inside the parent's tool
result with ``core.child_spawned``/``core.child_finished`` facts on the parent session.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import patch

import pytest

from tests.fixtures.core_factory import loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import BUNDLED_PLUGIN_IDS, open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core, CoreResult
from voidcode.core.events import child_completion_notice_id
from voidcode.plugins.subagents.profiles.builtin import EXPLORE_AGENT
from voidcode.plugins.subagents.subagents_plugin import OUTPUT_PAGE_BYTES
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ToolFailure

_SUBAGENTS_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "subagents"
_TODO_PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "src" / "voidcode" / "plugins" / "todo"
_EXPLORE_PROMPT_BODY = EXPLORE_AGENT.prompt


def _admitted_source(tmp_path: Path) -> Path:
    """The real subagents plugin root, referenced by path."""
    _ = tmp_path
    return _SUBAGENTS_PLUGIN_ROOT


def _core(tmp_path: Path, server: OpenAILoopbackServer, *, minimal: bool = False, read_only: bool = False) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        # Spawning is an external mutation: approval_mode "ask" would park the parent on an
        # approval request instead of running the delegated child.
        "policy": {"approval_mode": "yolo", "read_only": read_only},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            # The bundled files plugin is not under test here and is rewritten by another P4 lane.
            "files": {"enabled": False},
            # Explicitly out of the picture: this suite proves the subagents plugin alone.
            "todo": {"path": str(_TODO_PLUGIN_ROOT), "enabled": False},
            "subagents": {"path": str(_admitted_source(tmp_path)), "enabled": True},
        },
    }
    if minimal:
        config["plugins"] = {
            **{name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS},
            "providers": {"enabled": True},
            "files": {"enabled": True},
            "subagents": {"path": str(_admitted_source(tmp_path)), "enabled": True},
        }
    (workspace / "voidcode.toml").write_text(dumps_toml(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
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


def _full_task_body(core: Core, projection: Mapping[str, object]) -> dict[str, object]:
    """Read the complete task body behind a transcript projection."""
    uri = projection.get("result_uri")
    assert isinstance(uri, str) and uri.startswith("artifact://"), projection
    digest = uri.removeprefix("artifact://")
    chunks: list[bytes] = []
    offset = 0
    while True:
        page = core.read_artifact(digest, offset=offset)
        chunks.append(page.data)
        offset += len(page.data)
        if page.eof:
            break
    result = decode_tool_result(json.loads(b"".join(chunks).decode("utf-8")))
    assert result.body is not None
    return dict(result.body.as_payload())


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
    with loopback_server(replies) as server:
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
        projection = json.loads(tool_text.splitlines()[0])
        assert projection["status"] == "ok"
        body = _full_task_body(core, projection)
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
        stored = core.store.plugin_state("subagents", "session", parent_id)
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
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, minimal=True)
        parent_id = core.create_session("parent").session_id
        result = core.run("explore the visitor guide", session_id=parent_id)

        assert result.status == "completed", result.error
        assert len(server.requests) == 3
        child_prompt = _messages(server.requests[1])[-1]
        assert child_prompt["content"] == f"{_EXPLORE_PROMPT_BODY}\n\nsummarize the visitor guide"

        tool_text = str([message for message in _messages(server.requests[2]) if message.get("role") == "tool"][0]["content"])
        projection = json.loads(tool_text.splitlines()[0])
        assert projection["status"] == "ok"
        body = _full_task_body(core, projection)
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

        stored = core.store.plugin_state("subagents", "session", parent_id)
        assert stored is not None
        payload, _version = stored
        children = payload["children"]
        assert isinstance(children, Mapping)
        assert children[child_id]["subagent_type"] == "explore"


def test_readonly_parent_spawns_a_default_child_readonly(tmp_path: Path) -> None:
    replies: list[dict[str, object]] = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_task_call("call-task-readonly", {"prompt": "inspect the guide", "run_in_background": False})],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "child answer: inspected"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "read-only delegation complete"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, minimal=True, read_only=True)
        parent = core.create_session("parent").session_id
        result = core.run("delegate read-only work", session_id=parent)

        assert result.status == "completed", result.error
        child_id = next(event.payload["child_session_id"] for event in core.events_after(parent) if event.event_type == "core.child_spawned")
        assert core.store.session_overlays(child_id) == ("read-only",), "a read-only parent's stance reaches an unprofiled child"
        assert core.session(child_id).status == "completed"
        tool_messages = [message for message in _messages(server.requests[2]) if message.get("role") == "tool"]
        assert len(tool_messages) == 1
        assert '"status":"ok"' in str(tool_messages[0]["content"])


def test_parent_cancel_during_foreground_join_keeps_mapping_and_stops_promptly(tmp_path: Path) -> None:
    child_requested = Event()
    release_child = Event()
    child_reply = openai_response(message={"role": "assistant", "content": "child answer: done"}, finish="stop")

    def before_reply(payload: dict[str, object], index: int) -> dict[str, object] | None:
        _ = index
        if any(message.get("content") == "child work" for message in _messages(payload)):
            child_requested.set()
            assert release_child.wait(10), "the child reply gate was never released"
            return child_reply
        return None

    replies: list[dict[str, object]] = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_task_call("call-cancel-1", {"prompt": "child work", "run_in_background": False})],
            },
            finish="tool_calls",
        ),
        child_reply,
        openai_response(message={"role": "assistant", "content": "unexpected continuation"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        server.before_reply = before_reply
        core = _core(tmp_path, server, minimal=True)
        parent = core.create_session("parent").session_id
        outcome: dict[str, object] = {}

        def drive() -> None:
            outcome["result"] = core.run("delegate child work", session_id=parent)

        worker = Thread(target=drive, daemon=True)
        worker.start()
        try:
            assert child_requested.wait(10), "the child never reached the provider"
            assert core.cancel_session(parent, "test stop") is True
        finally:
            release_child.set()
        worker.join(timeout=10)
        assert not worker.is_alive(), "the parent stop waited out the join instead of returning within a slice"

        result = outcome.get("result")
        assert isinstance(result, CoreResult)
        assert result.status == "interrupted"
        events = list(core.events_after(parent))
        child_id = next(event.payload["child_session_id"] for event in events if event.event_type == "core.child_spawned")

        stored = core.store.plugin_state("subagents", "session", parent)
        assert stored is not None
        children = stored[0]["children"]
        assert isinstance(children, Mapping)
        assert child_id in children, "the created child mapping survived the parent stop"

        interrupted = [
            event
            for event in events
            if event.event_type == "core.tool_interrupted" and event.payload.get("call", {}).get("tool_call_id") == "call-cancel-1"
        ]
        assert len(interrupted) == 1
        decoded = decode_tool_result(interrupted[0].payload["result"])
        assert isinstance(decoded, ToolFailure)
        if decoded.execution is not None:
            assert decoded.execution.cancellation_signalled is True
            assert decoded.body is not None
            assert decoded.body.as_payload()["task_id"] == child_id


def test_standard_markdown_agent_applies_its_prompt_and_turn_cap(tmp_path: Path) -> None:
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
    with loopback_server(replies) as server:
        core = _core(tmp_path, server, minimal=True)
        directory = core.workspace / ".claude" / "agents"
        directory.mkdir(parents=True)
        (directory / "different-filename.md").write_text(
            "---\nname: inspector\ndescription: Inspect files\nmodel: inherit\nmaxTurns: 1\n---\nInspect carefully.\n"
        )
        parent = core.create_session().session_id
        result = core.run("delegate inspection", session_id=parent)
        assert result.status == "completed", result.error
        child_id = next(event.payload["child_session_id"] for event in core.events_after(parent) if event.event_type == "core.child_spawned")
        child = core.session(child_id)
        assert child.status == "completed" and child.tool_allowlist is None
        assert any(message.get("content") == "Inspect carefully.\n\ninspect" for message in _messages(server.requests[1]))
        assert len(server.requests) == 3


def test_task_output_preserves_provider_turn_budget_exhaustion(tmp_path: Path) -> None:
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _task_call(
                        "budgeted-child",
                        {
                            "prompt": "Try one tool call, then stop at the provider-turn cap.",
                            "max_steps": 1,
                            "run_in_background": False,
                        },
                    )
                ],
            },
            finish="tool_calls",
        ),
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_task_call("child-tool", {"operation": "output", "task_id": "unknown-child"})],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "delegation stopped at its cap"}, finish="stop"),
    ]
    with loopback_server(replies) as server:
        core = _core(tmp_path, server)
        parent = core.create_session("parent").session_id
        result = core.run("delegate one capped task", session_id=parent)

        assert result.status == "completed"
        assert len(server.requests) == 3
        tool_messages = [message for message in _messages(server.requests[2]) if message.get("role") == "tool"]
        assert len(tool_messages) == 1
        tool_text = str(tool_messages[0]["content"])
        payload = json.loads(tool_text.splitlines()[0])
        assert payload["status"] == "ok", "reporting a budget-interrupted child is a successful tool delivery"
        projected_text = payload["text"]
        assert isinstance(projected_text, str)
        assert "status=interrupted" in projected_text
        assert "stop_cause: limit_reached" in projected_text
        assert (
            "budget_exhaustion: dimension=provider_turns, source=max_steps, limit=1, consumed=1, remaining=0, continue_with=resume_with_max_steps"
        ) in projected_text
        assert "status=cancelled" not in projected_text


def test_wait_keeps_paged_inline_old_run_output_immutable_after_followup_starts(tmp_path: Path) -> None:
    first_page_prefix = "a" * (OUTPUT_PAGE_BYTES - 1)
    old_output = first_page_prefix + "🙂OLD_RUN_ONLY" + "z" * 512
    child_holder: dict[str, str] = {}
    child_a_started = Event()
    release_child_a = Event()
    child_b_started = Event()
    release_child_b = Event()

    def call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }

    def result_body(core: Core, request: dict[str, object], call_id: str) -> tuple[dict[str, object], dict[str, object]]:
        content = next(
            (message.get("content") for message in _messages(request) if message.get("role") == "tool" and message.get("tool_call_id") == call_id),
            None,
        )
        assert isinstance(content, str), f"missing tool result {call_id!r}"
        projection = json.loads(content)
        assert isinstance(projection, dict), projection
        uri = projection.get("result_uri")
        assert isinstance(uri, str) and uri.startswith("artifact://"), projection
        digest = uri.removeprefix("artifact://")
        chunks: list[bytes] = []
        offset = 0
        total: int | None = None
        while True:
            page = core.read_artifact(digest, offset=offset)
            assert page.offset == offset and page.artifact.sha256 == digest
            if total is None:
                total = page.artifact.bytes
            assert page.artifact.bytes == total and (page.data or page.eof)
            chunks.append(page.data)
            offset += len(page.data)
            if page.eof:
                assert offset == total
                break
        canonical = b"".join(chunks)
        assert hashlib.sha256(canonical).hexdigest() == digest
        result = decode_tool_result(json.loads(canonical.decode("utf-8")))
        assert result.body is not None
        return dict(result.body.as_payload()), projection

    with loopback_server([]) as server:

        def reply(request: dict[str, object], _index: int) -> dict[str, object] | None:
            if request.get("method") == "GET":
                return {"data": [{"id": "test-model"}]}
            messages = _messages(request)
            last_role = messages[-1].get("role") if messages else None
            last_user = next(
                (str(message.get("content", "")) for message in reversed(messages) if message.get("role") == "user"),
                "",
            )
            if last_user == "child A":
                child_a_started.set()
                if not release_child_a.wait(timeout=30):
                    raise TimeoutError("child A provider gate was not released")
                return openai_response(message={"role": "assistant", "content": old_output}, finish="stop")
            if "child B" in last_user:
                child_b_started.set()
                if not release_child_b.wait(timeout=30):
                    raise TimeoutError("child B provider gate was not released")
                return openai_response(message={"role": "assistant", "content": "NEW_RUN_ONLY"}, finish="stop")
            if last_user == "spawn A":
                if last_role == "tool":
                    return openai_response(message={"role": "assistant", "content": "spawned"}, finish="stop")
                return openai_response(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            call(
                                "spawn-A",
                                "task",
                                {"prompt": "child A", "run_in_background": False, "timeout": 0},
                            )
                        ],
                    },
                    finish="tool_calls",
                )
            if last_user == "start B":
                if last_role == "tool":
                    return openai_response(message={"role": "assistant", "content": "sent"}, finish="stop")
                return openai_response(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [call("send-B", "task", {"operation": "send", "task_id": child_holder["id"], "prompt": "child B"})],
                    },
                    finish="tool_calls",
                )
            if last_user == "wait A":
                if last_role == "tool":
                    return openai_response(message={"role": "assistant", "content": "waited"}, finish="stop")
                return openai_response(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [call("wait-A", "wait", {})],
                    },
                    finish="tool_calls",
                )
            if last_user == "read A remainder":
                if last_role == "tool":
                    return openai_response(message={"role": "assistant", "content": "read"}, finish="stop")
                return openai_response(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            call(
                                "read-A",
                                "read",
                                {
                                    "path": child_holder["output_uri"],
                                    "byte_offset": child_holder["next_offset"],
                                },
                            )
                        ],
                    },
                    finish="tool_calls",
                )
            if last_user == "cancel B":
                if last_role == "tool":
                    return openai_response(message={"role": "assistant", "content": "cancelled"}, finish="stop")
                return openai_response(
                    message={
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [call("cancel-B", "task", {"operation": "cancel", "task_id": child_holder["id"]})],
                    },
                    finish="tool_calls",
                )
            if last_user in ("wait B", "repeat B"):
                if last_role == "tool":
                    return openai_response(message={"role": "assistant", "content": "waited"}, finish="stop")
                call_id = "wait-B" if last_user == "wait B" else "repeat-B"
                return openai_response(
                    message={"role": "assistant", "content": None, "tool_calls": [call(call_id, "wait", {})]},
                    finish="tool_calls",
                )
            return openai_response(message={"role": "assistant", "content": f"unexpected prompt: {last_user!r}"}, finish="stop")

        server.before_reply = reply
        core = _core(tmp_path, server, minimal=True)
        core.auto_drain = False
        parent = core.create_session("parent").session_id
        try:
            spawned = core.run("spawn A", session_id=parent)
            assert spawned.status == "completed", spawned.error
            child = next(event.payload["child_session_id"] for event in core.events_after(parent) if event.event_type == "core.child_spawned")
            child_holder["id"] = str(child)
            assert child_a_started.wait(timeout=15), "foreground A did not reach its gated provider turn"
            run_a = core.session(str(child)).run_id
            assert run_a is not None and core.session(str(child)).status == "running"
            release_child_a.set()
            first = core.join_child(parent, str(child), timeout_seconds=30)
            assert first.status == "completed" and first.output == old_output and first.run_id == run_a
            notice_id = child_completion_notice_id(parent, str(child), run_a)
            assert core.store.continuation_status(notice_id) is None

            sent = core.run("start B", session_id=parent)
            assert sent.status == "completed", sent.error
            assert child_b_started.wait(timeout=15), "follow-up B never reached its gated provider turn"
            run_b = core.session(str(child)).run_id
            assert run_b is not None and run_b != run_a
            assert core.session(str(child)).status == "running"

            waited = core.run("wait A", session_id=parent)
            assert waited.status == "completed", waited.error
            wait_body, wait_projection = result_body(core, server.requests[-1], "wait-A")
            children = wait_body.get("children")
            pending = wait_body.get("pending")
            assert isinstance(children, list) and len(children) == 1
            assert isinstance(pending, list) and len(pending) == 1, (wait_body, core.list_descendants(parent))
            child_result = children[0]
            assert child_result["task_id"] == child and child_result["run_id"] == run_a
            assert child_result["status"] == "completed"
            assert child_result["output"] == first_page_prefix
            assert child_result["output_bytes"] == len(old_output.encode("utf-8"))
            output_uri = child_result.get("output_uri")
            next_offset = child_result.get("next_output_offset")
            assert isinstance(output_uri, str) and output_uri.startswith("artifact://")
            assert type(next_offset) is int and next_offset > 0
            assert pending[0]["task_id"] == child
            assert pending[0]["run_id"] == run_b and pending[0]["status"] == "running"
            projected_text = wait_projection.get("text")
            assert isinstance(projected_text, str)
            assert output_uri in projected_text
            assert projected_text.index(output_uri) < projected_text.index(first_page_prefix)
            child_holder.update({"output_uri": output_uri, "next_offset": next_offset})

            read = core.run("read A remainder", session_id=parent)
            assert read.status == "completed", read.error
            read_body, _read_projection = result_body(core, server.requests[-1], "read-A")
            remainder = read_body.get("raw_content")
            assert isinstance(remainder, str)
            assert first_page_prefix + remainder == old_output
            cancel = core.run("cancel B", session_id=parent)
            assert cancel.status == "completed", cancel.error
            release_child_b.set()
            cancelled = core.join_child(parent, str(child), timeout_seconds=30)
            assert cancelled.status == "cancelled" and cancelled.run_id == run_b
            cancelled_fact = next(
                event for event in core.events_after(parent) if event.event_type == "core.child_finished" and event.payload.get("run_id") == run_b
            )
            assert cancelled_fact.payload["status"] == "cancelled"
            notice_b = child_completion_notice_id(parent, str(child), run_b)
            delivered = core.run("wait B", session_id=parent)
            assert delivered.status == "completed", delivered.error
            cancelled_body, _cancelled_projection = result_body(core, server.requests[-1], "wait-B")
            cancelled_children = cancelled_body.get("children")
            assert cancelled_children == []
            assert cancelled_body.get("pending") == []
            assert core.store.continuation_status(notice_b) == "completed"
            replay = core.run("repeat B", session_id=parent)
            assert replay.status == "completed", replay.error
            replay_body, _replay_projection = result_body(core, server.requests[-1], "repeat-B")
            assert replay_body.get("children") == []
        finally:
            release_child_a.set()
            release_child_b.set()


def test_agent_snapshot_refreshes_current_run_after_concurrent_publication(tmp_path: Path) -> None:
    token = "publish-new-agent-run-during-read"
    second_output = "second accepted publication\n"
    replies = [
        openai_response(
            message={
                "role": "assistant",
                "content": None,
                "tool_calls": [_task_call("first-task", {"prompt": "produce first result", "run_in_background": False})],
            },
            finish="tool_calls",
        ),
        openai_response(message={"role": "assistant", "content": "first accepted publication\n"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "done"}, finish="stop"),
    ]

    def read_call(call_id: str, uri: str) -> dict[str, object]:
        return {
            "id": call_id,
            "type": "function",
            "function": {"name": "read", "arguments": json.dumps({"path": uri})},
        }

    def tool_text(server: OpenAILoopbackServer) -> str:
        tool_messages = [message for message in _messages(server.requests[-1]) if message.get("role") == "tool"]
        assert tool_messages
        payload = json.loads(str(tool_messages[-1]["content"]))
        if isinstance(payload.get("text"), str):
            return payload["text"]
        output = payload.get("output")
        assert isinstance(output, dict) and isinstance(output.get("text"), str), payload
        return output["text"]

    with loopback_server(replies) as server:
        core = _core(tmp_path, server, minimal=True)
        parent = core.create_session("parent").session_id
        assert core.run("create first child result", session_id=parent).status == "completed"
        child = core.list_descendants(parent)[0].session_id
        uri = f"agent://{child}"

        server.replies.extend(
            [
                openai_response(
                    message={"role": "assistant", "content": None, "tool_calls": [read_call("first-read", uri)]},
                    finish="tool_calls",
                ),
                openai_response(message={"role": "assistant", "content": "read complete"}, finish="stop"),
            ]
        )
        assert core.run("read first publication", session_id=parent).status == "completed"
        first_text = tool_text(server)
        first_run_line = next(line for line in first_text.splitlines() if line.startswith("Latest published output run_id: "))
        first_run = first_run_line.split(": ", 1)[1].split(" ", 1)[0]
        first_ref_line = next(line for line in first_text.splitlines() if line.startswith("Immutable output: "))
        first_ref = first_ref_line[len("Immutable output: ") :].removesuffix(".")
        assert first_run and first_ref.startswith("artifact://")

        server.replies.extend(
            [
                openai_response(
                    message={"role": "assistant", "content": None, "tool_calls": [read_call("racing-read", uri)]},
                    finish="tool_calls",
                ),
                openai_response(message={"role": "assistant", "content": "read complete"}, finish="stop"),
            ]
        )
        sampled = Event()
        resume = Event()
        reader: Thread | None = None
        reader_results = []
        original_list = core._delegation.list_descendants

        def gated_list(session_id: str):
            nodes = original_list(session_id)
            if current_thread() is reader and session_id == parent and not sampled.is_set():
                assert any(node.session_id == child and node.run_id == first_run for node in nodes), nodes
                sampled.set()
                if not resume.wait(timeout=20):
                    raise TimeoutError("snapshot race barrier was not released")
            return nodes

        def run_read() -> None:
            reader_results.append(core.run("read while child publishes", session_id=parent))

        def reply_to_second_run(payload: dict[str, object], _ordinal: int) -> dict[str, object] | None:
            messages = payload.get("messages")
            if isinstance(messages, list) and any(
                isinstance(message, dict) and message.get("role") == "user" and token in str(message.get("content", "")) for message in messages
            ):
                return openai_response(message={"role": "assistant", "content": second_output}, finish="stop")
            return None

        server.before_reply = reply_to_second_run
        reader = Thread(target=run_read)
        with patch.object(core._delegation, "list_descendants", side_effect=gated_list):
            reader.start()
            try:
                assert sampled.wait(timeout=10), "agent read did not take its initial descendant sample"
                sent = core.send_child(parent, child, token, notify_on_completion=False)
                assert sent.delivery == "started" and sent.run_id and sent.run_id != first_run
                published = core.join_child(parent, child, timeout_seconds=20)
                assert (published.status, published.run_id, published.output) == ("completed", sent.run_id, second_output)
            finally:
                resume.set()
                reader.join(timeout=20)
        assert not reader.is_alive()
        assert len(reader_results) == 1 and reader_results[0].status == "completed"

        raced_text = tool_text(server)
        assert "Current status: completed" in raced_text
        assert f"Current run_id: {sent.run_id}" in raced_text
        assert f"Latest published output run_id: {sent.run_id} (status=completed)." in raced_text
        raced_ref_line = next(line for line in raced_text.splitlines() if line.startswith("Immutable output: "))
        assert raced_ref_line.endswith(".") and raced_ref_line[len("Immutable output: ") : -1] != first_ref
        assert "This previous output is superseded while the current run is active." not in raced_text
        assert "This is the last published output, not the current session run's result." not in raced_text


# --- task_batch: shared context ---


def test_batch_shared_context_reaches_the_real_child_prompt(tmp_path: Path) -> None:
    """End to end: one declared context is merged into the prompt the real child model sees."""
    shared_context = "The visitor guide hangs on the north wall."
    item_prompt = "count the visitor guide doors"
    merged = f"Shared context:\n{shared_context}\n\nTask:\n{item_prompt}"
    batch_reply = openai_response(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-batch-1",
                    "type": "function",
                    "function": {
                        "name": "task_batch",
                        "arguments": json.dumps({"tasks": [{"prompt": item_prompt, "label": "doors"}], "context": shared_context}),
                    },
                }
            ],
        },
        finish="tool_calls",
    )
    child_reply = openai_response(message={"role": "assistant", "content": "child answer: 12 doors"}, finish="stop")
    wait_reply = openai_response(
        message={
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-wait-1", "type": "function", "function": {"name": "wait", "arguments": "{}"}}],
        },
        finish="tool_calls",
    )
    finish_reply = openai_response(message={"role": "assistant", "content": "delegation complete"}, finish="stop")

    def route(payload: dict[str, object], _ordinal: int) -> dict[str, object] | None:
        # The background child races the parent's second turn, so route by request shape, not arrival order.
        messages = payload.get("messages")
        # NOTE: match the merged prompt against raw message content; json.dumps would escape its newlines.
        if isinstance(messages, list) and any(
            isinstance(message, dict) and message.get("role") == "user" and merged in str(message.get("content", "")) for message in messages
        ):
            return child_reply
        text = json.dumps(payload)
        if "Delivered 1 child result(s)" in text:
            return finish_reply
        if "Started 1 child task(s)" in text:
            return wait_reply
        return batch_reply

    with loopback_server([]) as server:
        server.before_reply = route
        core = _core(tmp_path, server)
        parent_id = core.create_session("parent").session_id
        result = core.run("count the doors", session_id=parent_id)
        assert result.status == "completed"
        assert result.output == "delegation complete"

        # Four provider turns: the parent's three plus the child's one.
        assert len(server.requests) == 4
        assert "task_batch" in _tool_names(server.requests[0])
        child_requests = [
            request
            for request in server.requests
            if any(message.get("role") == "user" and message.get("content") == merged for message in _messages(request))
        ]
        assert len(child_requests) == 1, "exactly the child observes the merged prompt"
        child_users = [message for message in _messages(child_requests[0]) if message.get("role") == "user"]
        assert child_users and child_users[-1]["content"] == merged

        # The wait delivery keeps this child's exact run/output identity.
        tool_messages = [message for message in _messages(server.requests[-1]) if message.get("role") == "tool"]
        wait_text = next(text for text in (str(message.get("content", "")) for message in tool_messages) if "Delivered 1 child result(s)" in text)
        assert "child answer: 12 doors" in wait_text
        projection_line = next(line for line in wait_text.splitlines() if '"result_uri"' in line)
        wait_body = _full_task_body(core, json.loads(projection_line))
        assert wait_body["operation"] == "wait"
        raw_children = wait_body["children"]
        assert isinstance(raw_children, list)
        (delivered,) = [child for child in raw_children if isinstance(child, Mapping)]
        assert delivered["output"] == "child answer: 12 doors"
        assert delivered["status"] == "completed"
        assert isinstance(delivered["run_id"], str) and delivered["run_id"]
        child_id = str(delivered["task_id"])

        child = core.session(child_id)
        assert child.parent_session_id == parent_id
        assert child.status == "completed"

        stored = core.store.plugin_state("subagents", "session", parent_id)
        assert stored is not None
        payload, _version = stored
        assert payload is not None
        groups = payload["groups"]
        assert isinstance(groups, Mapping) and len(groups) == 1
        assert [list(members) for members in groups.values()] == [[child_id]]
        children = payload["children"]
        assert isinstance(children, Mapping) and list(children) == [child_id]
