"""Focused kernel regressions for delegated collaboration.

Four behaviors, each proven against the real kernel/storage/host stack:

- a join answers for the child's CURRENT run, never an older terminal fact;
- a completion-notice acknowledgement commits with the tool result that consumed it;
- a terminal child run whose parent fact a crash never committed is republished from
  durable events on restart, with its notice, and without rerunning the child;
- steering authorization follows the CURRENT stance of parent and child.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel
from voidcode.sdk.manifest import PluginFileIdentity, PluginManifest, PluginToolDeclaration
from voidcode.sdk.services import ChildOutcome, child_completion_notice_id
from voidcode.sdk.tools import ToolEffect

PLUGIN_ID = "collaboration-fixture"
ACK_TOOL = "collaboration_ack"
PROBE_TOOL = "collaboration_probe"

_PLUGIN_CODE = '''
"""Fixture: spawn and join one child, acknowledge its completion, or park on a write probe."""

from __future__ import annotations

import json
from pathlib import Path

from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.plugin import Plugin, tool_definition
from voidcode.sdk.tools import TextOutput, ToolSuccess


class CollaborationTool:
    def __init__(self, definition):
        self.definition = definition

    def invoke(self, call, *, context, services):
        if call.tool_name == "{probe}":
            return ToolSuccess(call.tool_name, TextOutput("probe ran"))
        child = services.spawn_child(str(call.arguments["prompt"]), label="delegated", notify_on_completion=True)
        outcome = services.join_child(child, timeout_seconds=30)
        if outcome.run_id is not None:
            services.acknowledge_child_completion(child, outcome.run_id)
        return ToolSuccess(
            call.tool_name,
            TextOutput(json.dumps({{"child": child, "status": outcome.status, "run_id": outcome.run_id}}, sort_keys=True)),
        )


def create_plugin(config):
    manifest = parse_plugin_manifest(json.loads(Path(__file__).with_name("voidcode-plugin.json").read_text()))
    return Plugin(tools=tuple(CollaborationTool(tool_definition(declaration)) for declaration in manifest.tools))
'''


def _write_plugin(source: Path) -> None:
    source.mkdir(parents=True, exist_ok=True)
    (source / "collaboration_plugin.py").write_text(_PLUGIN_CODE.format(probe=PROBE_TOOL), encoding="utf-8")
    (source / "__init__.py").write_text("", encoding="utf-8")
    manifest = PluginManifest(
        plugin_id=PLUGIN_ID,
        version="1.0.0",
        entrypoint="collaboration_plugin:create_plugin",
        files=(
            PluginFileIdentity(path="__init__.py", sha256=hashlib.sha256(b"").hexdigest()),
            PluginFileIdentity(
                path="collaboration_plugin.py",
                sha256=hashlib.sha256((source / "collaboration_plugin.py").read_bytes()).hexdigest(),
            ),
        ),
        tools=(
            PluginToolDeclaration(
                name=ACK_TOOL,
                description="Spawn one child, join it, and acknowledge the run it returned.",
                input_schema={
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                    "additionalProperties": False,
                },
                effects=(ToolEffect.SESSION,),
            ),
            PluginToolDeclaration(
                name=PROBE_TOOL,
                description="A write-tier probe that parks under approval mode ask.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                effects=(ToolEffect.WRITE,),
            ),
        ),
    )
    (source / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
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


_CREATED: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_kernels() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _open(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    *,
    approval_mode: str = "yolo",
    auto_drain: bool = True,
) -> Kernel:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    config_path = workspace / "voidcode.json"
    if not config_path.exists():
        source = tmp_path / "plugin"
        _write_plugin(source)
        config = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": approval_mode, "read_only": False},
            "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=tmp_path / "state", trusted_plugins=frozenset({PLUGIN_ID}), auto_drain=auto_drain)
    _CREATED.append(kernel)
    return kernel


def _wait_for(predicate: Callable[[], bool], *, timeout: float = 15.0, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _child_facts(kernel: Kernel, parent: str) -> list[dict[str, object]]:
    return [dict(event.payload) for event in kernel.events_after(parent) if event.event_type == "kernel.child_finished"]


def _terminal_join(kernel: Kernel, parent: str, child: str, *, timeout: float = 20.0) -> ChildOutcome:
    """Join until the terminal outcome is published; the child's terminal commit precedes it."""
    deadline = time.monotonic() + timeout
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        outcome = kernel.join_child(parent, child, timeout_seconds=remaining)
        if outcome.output is not None or outcome.status in ("failed", "cancelled"):
            return outcome
        assert remaining > 0, "the child's terminal outcome was never published"
        time.sleep(0.05)


def test_join_answers_for_the_current_run_not_an_older_fact(tmp_path: Path) -> None:
    replies = [
        _turn(output="first run done"),
        # The follow-up run asks for a write under approval mode ask: it parks, not completes.
        _turn(_call("probe-1", PROBE_TOOL)),
    ]
    with _server(replies) as server:
        kernel = _open(tmp_path, server, approval_mode="ask", auto_drain=False)
        kernel.create_session("parent")
        child = kernel.spawn_child("parent", "first task")
        first = _terminal_join(kernel, "parent", child)
        assert (first.status, first.output) == ("completed", "first run done")

        receipt = kernel.send_child("parent", child, "second task")
        assert receipt.delivery == "started"
        assert receipt.run_id is not None

        outcome = kernel.join_child("parent", child, timeout_seconds=15)
        assert outcome.run_id == receipt.run_id, "the join must belong to the current run"
        assert outcome.status == "waiting", "the parked follow-up is not the old completed run"
        assert outcome.output is None, "an older run's output must never satisfy a newer join"

        facts = _child_facts(kernel, "parent")
        assert [fact["run_id"] for fact in facts] == [first.run_id], "only the first run is terminal"


def test_completion_notice_is_acked_with_the_tool_result_that_consumed_it(tmp_path: Path) -> None:
    replies = [
        _turn(_call("ack-1", ACK_TOOL, prompt="child work")),
        _turn(output="child answer"),
        _turn(output="parent done"),
    ]
    with _server(replies) as server:
        kernel = _open(tmp_path, server, auto_drain=False)
        parent = kernel.create_session("parent").session_id

        result = kernel.run("delegate and consume", session_id=parent)
        assert result.status == "completed", result.error
        assert result.output == "parent done"

        facts = _child_facts(kernel, parent)
        assert len(facts) == 1
        child = str(facts[0]["child_session_id"])
        run_id = str(facts[0]["run_id"])
        assert facts[0]["status"] == "completed"
        notice_id = child_completion_notice_id(parent, child, run_id)

        # The ack cancelled the notice in the same commit as the consuming tool result.
        assert kernel.store.continuation_status(notice_id) == "cancelled"
        assert kernel.store.pending_continuations(parent, limit=10) == ()
        started = [event for event in kernel.events_after(parent) if event.event_type == "kernel.continuation_started"]
        assert all(event.payload.get("continuation_id") != notice_id for event in started)

        # The next request never re-delivers the consumed notice.
        assert len(server.requests) == 3
        assert notice_id not in json.dumps(server.requests[2]["messages"])
        assert "child_finished" not in json.dumps(server.requests[2]["messages"])


def test_restart_republishes_an_uncommitted_child_fact_once_without_rerunning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = [
        _turn(output="recovered output"),
        _turn(output="notice consumed"),
    ]
    with _server(replies) as server:
        kernel = _open(tmp_path, server)
        parent = kernel.create_session("parent").session_id

        # Simulate a crash after the child's terminal commit and before its parent fact.
        monkeypatch.setattr(kernel, "_publish_child_terminal", lambda *args, **kwargs: None)
        child = kernel.spawn_child(parent, "child prompt", notify_on_completion=True)
        joined = kernel.join_child(parent, child, timeout_seconds=15)
        assert joined.status == "completed"
        assert joined.run_id is not None
        assert _child_facts(kernel, parent) == []
        kernel.close()
        _CREATED.remove(kernel)

        reopened = _open(tmp_path, server)
        _wait_for(lambda: bool(_child_facts(reopened, parent)), what="recovered child publication")
        _wait_for(lambda: len(server.requests) == 2, what="the recovered notice delivery")

        facts = _child_facts(reopened, parent)
        assert len(facts) == 1
        fact = facts[0]
        assert fact["child_session_id"] == child
        assert fact["run_id"] == joined.run_id
        assert fact["status"] == "completed"
        assert fact["output"] == "recovered output"
        assert fact["notify_on_completion"] is True
        history = fact["history"]
        assert isinstance(history, Mapping) and history["sha256"]

        # The child was not rerun: exactly one provider request carried its prompt.
        child_requests = [
            request
            for request in server.requests
            if (messages := request.get("messages")) and isinstance(messages, list) and messages[-1].get("content") == "child prompt"
        ]
        assert len(child_requests) == 1
        notice_id = child_completion_notice_id(parent, child, joined.run_id)
        _wait_for(lambda: reopened.store.continuation_status(notice_id) == "completed", what="the recovered notice consumption")


def test_send_authorization_follows_the_current_stance(tmp_path: Path) -> None:
    replies = [
        _turn(output="child one done"),
        _turn(output="child two done"),
    ]
    with _server(replies) as server:
        kernel = _open(tmp_path, server, auto_drain=False)
        parent = kernel.create_session("parent").session_id
        other = kernel.create_session("other").session_id
        child = kernel.spawn_child(parent, "first task")
        first = _terminal_join(kernel, parent, child)
        assert (first.status, first.output) == ("completed", "child one done")

        with pytest.raises(PermissionError, match="does not belong"):
            kernel.send_child(other, child, "steer")

        kernel.store.set_session_overlay(parent, "kernel")
        with pytest.raises(PermissionError, match="read-only parent cannot steer"):
            kernel.send_child(parent, child, "steer")
        assert kernel.store.pending_continuations(child, limit=10) == (), "a refused send enqueues nothing"

        kernel.store.set_session_overlay(child, "kernel")
        receipt = kernel.send_child(parent, child, "second task", notify_on_completion=False)
        assert receipt.delivery == "started"
        assert receipt.run_id is not None and receipt.run_id != first.run_id

        second = _terminal_join(kernel, parent, child)
        assert (second.status, second.output) == ("completed", "child two done")
        assert second.run_id == receipt.run_id
