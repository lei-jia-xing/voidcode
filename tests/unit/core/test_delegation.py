"""C3/C12 acceptance: core-owned child lifecycle and per-session tool visibility."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import DEACTIVATE_TOOL, DELEGATE_TOOL, PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.json_values import json_wire_object

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "delegation reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        # Hermetic: bundled file tools are irrelevant to delegation semantics.
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _tools_in_request(request: dict[str, object]) -> set[str]:
    tools = request.get("tools")
    assert isinstance(tools, list)
    return {str(tool["function"]["name"]) for tool in tools if isinstance(tool, dict)}


def _tool_outputs(core: Core, session_id: str) -> list[str]:
    outputs = []
    for event in core.events_after(session_id):
        if event.event_type != "core.tool_completed":
            continue
        result = event.payload.get("result")
        if isinstance(result, Mapping):
            output = result.get("output")
            if isinstance(output, Mapping) and isinstance(output.get("text"), str):
                outputs.append(str(output["text"]))
    return outputs


def test_delegated_children_run_and_join_with_a_narrowed_tool_scope(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", delegating=True)
    replies = [
        # Parent: spawn two children, each limited to the probe tool.
        _turn(_call("delegate-1", DELEGATE_TOOL, prompts=["child one", "child two"], allowlist=[PROBE_TOOL])),
        # Any order across the two concurrent children: probe, done, probe, done.
        _turn(_call("probe-a", PROBE_TOOL)),
        _turn(output="child finished"),
        _turn(_call("probe-b", PROBE_TOOL)),
        _turn(output="child finished"),
        # Parent continues after both joins.
        _turn(output="parent done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("parent")
        result = core.run("delegate the work", session_id="parent")
        assert result.status == "completed"
        assert result.output == "parent done"

        delegated = json.loads(_tool_outputs(core, "parent")[-1])
        children = delegated["children"]
        assert len(children) == 2
        assert [child["status"] for child in children] == ["completed", "completed"]
        assert all(child["child_session_id"] for child in children)

        # Each child got its own durable session, linked to the parent, with the narrowed scope.
        for child in children:
            record = core.session(child["child_session_id"])
            assert record.parent_session_id == "parent"
            assert record.depth == 1
            assert record.root_session_id == "parent"
            assert record.tool_allowlist == (PROBE_TOOL,)

        # The child dispatches advertised exactly the allowlisted tool.
        child_requests = [request for request in server.requests[1:5]]
        for request in child_requests:
            assert _tools_in_request(request) == {PROBE_TOOL}

        spawned = [event for event in core.events_after("parent") if event.event_type == "core.child_spawned"]
        finished = [event for event in core.events_after("parent") if event.event_type == "core.child_finished"]
        assert len(spawned) == 2
        assert len(finished) == 2


def test_child_limits_and_scope_are_enforced_by_the_core(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", delegating=True)
    # The chain quota owner is policy.max_root_children (default 4); _configure leaves
    # it unset, so size the provider script to the admitted default.
    budget = 4
    # Every spawned child auto-drives exactly one provider turn: supply one final
    # reply per child the quota admits, then join each child before the server
    # exits so no child thread outlives its provider or the store.
    replies = [_turn(output=f"child {index} done") for index in range(budget)]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        assert core.config.max_root_children == budget
        core.create_session("root")
        session = core.session("root")
        children: list[str] = []

        # A tool outside the admitted catalog cannot be requested.
        with pytest.raises(ValueError, match="names an unknown tool"):
            core.spawn_child("root", "child", tool_allowlist=("no_such_tool",))
        # Narrowing inside a child is allowed; widening is refused.
        child = core.spawn_child("root", "scoped child", tool_allowlist=(PROBE_TOOL,))
        children.append(child)
        with pytest.raises(ValueError, match="widens the parent scope"):
            core.spawn_child(child, "grandchild", tool_allowlist=(PROBE_TOOL, DEACTIVATE_TOOL))
        record = core.session(child)
        assert record.depth == 1
        assert record.root_session_id == "root"
        assert session.depth == 0

        # The chain's child quota is finite and counts every child of the root chain.
        while len(core.store.descendant_sessions("root")) < budget:
            children.append(core.spawn_child("root", "budget child"))
        with pytest.raises(ValueError, match="spawn budget exhausted"):
            core.spawn_child("root", "one too many")

        # Join every driven child while the provider is still listening: each must
        # reach its terminal state, and the join consumes its published fact.
        assert len(children) == budget
        for child_id in children:
            outcome = core.join_child("root", child_id, timeout_seconds=60.0)
            assert outcome.status == "completed", outcome.as_payload()
        assert len(server.requests) == budget


def test_child_cannot_call_a_tool_outside_its_scope(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", delegating=True)
    replies = [
        _turn(_call("delegate-1", DELEGATE_TOOL, prompts=["do something"], allowlist=[PROBE_TOOL])),
        # The child tries a tool it is not allowed to see.
        _turn(_call("note-1", DEACTIVATE_TOOL)),
        _turn(output="child finished"),
        _turn(output="parent done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("parent")
        assert core.run("delegate", session_id="parent").status == "completed"
        children = [record for record in core.sessions() if record.parent_session_id == "parent"]
        assert len(children) == 1
        child_events = list(core.events_after(children[0].session_id))
        denied = [
            event
            for event in child_events
            if event.event_type == "core.tool_completed" and "profile scope" in json.dumps(json_wire_object(event.payload))
        ]
        assert denied, "the out-of-scope call must be denied inside the child"
