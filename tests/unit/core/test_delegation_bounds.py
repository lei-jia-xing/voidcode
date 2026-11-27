"""Delegation bounds: a core guard (not the product budget) and no privilege expansion."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest

from tests.fixtures.core_factory import loopback_server, register_core
from tests.fixtures.reload_plugin import DELEGATE_TOOL, OVERLAY_COMMAND, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.core.storage.store import MAX_CHILD_LABEL_BYTES
from voidcode.sdk.child_schema import CHILD_PROMPT_MAX_BYTES

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "delegation reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.toml").write_text(
        dumps_toml(
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


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    register_core(core)
    return core


def _results(core: Core, session_id: str) -> list[Mapping[str, object]]:
    return [
        payload
        for event in core.events_after(session_id)
        if event.event_type in ("core.tool_completed", "core.tool_interrupted")
        for payload in [dict(event.payload).get("result")]
        if isinstance(payload, Mapping)
    ]


def test_child_prompt_bound_is_a_guard_not_a_product_budget(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [_turn(output="child answered")]
    with loopback_server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-parent")

        # 200 KiB passes end to end: a review diff is a legitimate payload, the bound is a guard,
        # and the core-driven child run uses the delegated-prompt bound, not client ingress.
        big = "x" * (200 * 1024)
        child = core.spawn_child("sess-parent", big, label="review")
        assert child
        core.join_child("sess-parent", child, timeout_seconds=60)
        child_events = core.events_after(child)
        assert not [event for event in child_events if event.event_type == "core.run_failed"]
        assert [event for event in child_events if event.event_type == "core.user_message"]
        with pytest.raises(ValueError, match="256 KiB"):
            core.spawn_child("sess-parent", "y" * (CHILD_PROMPT_MAX_BYTES + 1), label="too-big")


def test_child_birth_label_bound_is_measured_in_utf8_bytes(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [_turn(output="child answered")]
    with loopback_server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-parent")

        # Exactly 1 KiB lands in the birth row unchanged.
        edge = "x" * MAX_CHILD_LABEL_BYTES
        child = core.spawn_child("sess-parent", "small prompt", label=edge)
        assert child
        birth = next(event for event in core.events_after(child) if event.event_type == "core.child_reserved")
        assert birth.payload["label"] == edge
        core.join_child("sess-parent", child, timeout_seconds=60)

        # One byte over fails visibly, measured in UTF-8 bytes, not characters.
        with pytest.raises(ValueError, match="1 KiB"):
            core.spawn_child("sess-parent", "small prompt", label="y" * (MAX_CHILD_LABEL_BYTES + 1))
        with pytest.raises(ValueError, match="1 KiB"):
            core.spawn_child("sess-parent", "small prompt", label="é" * 513)


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
    with loopback_server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-plan-parent")
        entered = core.invoke_command("sess-plan-parent", OVERLAY_COMMAND, {"action": "on"}, expected_revision=core.revision_id)
        assert entered.status == "ok", entered.error

        assert core.run("delegate", session_id="sess-plan-parent").status == "completed"
        results = _results(core, "sess-plan-parent")
        assert len(results) == 2
        # Without the declaration the core refuses: a read-only owner cannot widen a child.
        assert results[0]["status"] == "error"
        spawned = [event for event in core.events_after("sess-plan-parent") if event.event_type == "core.child_spawned"]
        assert len(spawned) == 1, "only the read-only child was ever created"
        # With `readonly=True` the child is allowed and recorded as restricted by the core.
        assert results[1]["status"] == "ok", results[1]
        output = results[1]["output"]
        text = output["text"] if isinstance(output, Mapping) else output
        # The fixture emits one JSON line; bundled plugins may append post-tool notes after it
        # (e.g. the hooks plugin's read-only skip note), which are not part of this assertion.
        outcomes = json.loads(str(text).splitlines()[0])
        children = outcomes.get("children") if isinstance(outcomes, Mapping) else None
        assert isinstance(children, list) and children
        child_id = str(children[0]["child_session_id"])
        # The core-owned stance is durable and no plugin call can address it.
        assert core.store.session_overlays(child_id) == ("read-only",)
        assert core.store.clear_session_overlay(child_id, PLUGIN_ID) is False
        assert core.store.session_overlays(child_id) == ("read-only",)
