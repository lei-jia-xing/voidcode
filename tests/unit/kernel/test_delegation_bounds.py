"""Delegation bounds: a kernel guard (not the product budget) and no privilege expansion."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import DELEGATE_TOOL, OVERLAY_COMMAND, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import CHILD_PROMPT_MAX_BYTES, Kernel

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


_CREATED: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_kernels() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
            }
        ),
        encoding="utf-8",
    )


def _open(tmp_path: Path) -> Kernel:
    kernel = open_kernel(tmp_path / "workspace", state_dir=tmp_path / "state", trusted_plugins=frozenset({PLUGIN_ID}))
    _CREATED.append(kernel)
    return kernel


def _results(kernel: Kernel, session_id: str) -> list[Mapping[str, object]]:
    return [
        payload
        for event in kernel.events_after(session_id)
        if event.event_type in ("kernel.tool_completed", "kernel.tool_interrupted")
        for payload in [dict(event.payload).get("result")]
        if isinstance(payload, Mapping)
    ]


def test_child_prompt_bound_is_a_guard_not_a_product_budget(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [_turn(output="child answered")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-parent")

        # 200 KiB passes end to end: a review diff is a legitimate payload, the bound is a guard,
        # and the kernel-driven child run uses the delegated-prompt bound, not client ingress.
        big = "x" * (200 * 1024)
        child = kernel.spawn_child("sess-parent", big, label="review")
        assert child
        kernel.join_child("sess-parent", child, timeout_seconds=60)
        child_events = kernel.events_after(child)
        assert not [event for event in child_events if event.event_type == "kernel.run_failed"]
        assert [event for event in child_events if event.event_type == "kernel.user_message"]
        with pytest.raises(ValueError, match="256 KiB"):
            kernel.spawn_child("sess-parent", "y" * (CHILD_PROMPT_MAX_BYTES + 1), label="too-big")


def test_read_only_stance_may_only_delegate_read_only_work(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", delegating=True, overlay_command=True)
    # The child of the second delegation runs inside its parent's tool call (join blocks),
    # so its own provider reply must be queued between the two parent turns.
    replies = [
        _turn(_call("delegate-1", DELEGATE_TOOL, prompts=["writable child"])),
        _turn(_call("delegate-2", DELEGATE_TOOL, prompts=["read-only child"], readonly=True)),
        _turn(output="child finished"),
        _turn(output="done"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-plan-parent")
        entered = kernel.invoke_command("sess-plan-parent", OVERLAY_COMMAND, {"action": "on"}, expected_revision=kernel.revision_id)
        assert entered.status == "ok", entered.error

        assert kernel.run("delegate", session_id="sess-plan-parent").status == "completed"
        results = _results(kernel, "sess-plan-parent")
        assert len(results) == 2
        # Without the declaration the kernel refuses: a read-only owner cannot widen a child.
        assert results[0]["status"] == "error"
        spawned = [event for event in kernel.events_after("sess-plan-parent") if event.event_type == "kernel.child_spawned"]
        assert len(spawned) == 1, "only the read-only child was ever created"
        # With `readonly=True` the child is allowed and recorded as restricted by the kernel.
        assert results[1]["status"] == "ok", results[1]
        output = results[1]["output"]
        text = output["text"] if isinstance(output, Mapping) else output
        # The fixture emits one JSON line; bundled plugins may append post-tool notes after it
        # (e.g. the hooks plugin's read-only skip note), which are not part of this assertion.
        outcomes = json.loads(str(text).splitlines()[0])
        children = outcomes.get("children") if isinstance(outcomes, Mapping) else None
        assert isinstance(children, list) and children
        child_id = str(children[0]["child_session_id"])
        # The kernel-owned stance is durable and no plugin call can address it.
        assert kernel.store.session_overlays(child_id) == ("read-only",)
        assert kernel.store.clear_session_overlay(child_id, PLUGIN_ID) is False
        assert kernel.store.session_overlays(child_id) == ("read-only",)
