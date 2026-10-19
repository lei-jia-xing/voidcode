"""C4 acceptance: owner-owned restrictive overlays (plan mode) tighten only, durably."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import OVERLAY_COMMAND, PROBE_TOOL, SERVICES_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "overlay reasoning", "tool_calls": list(calls)},
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


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path, *, read_only: bool = False) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/exact-model"},
        "policy": {"approval_mode": "yolo", "read_only": read_only},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")


def _open(tmp_path: Path) -> Kernel:
    kernel = open_kernel(tmp_path / "workspace", state_dir=tmp_path / "state", trusted_plugins=frozenset({PLUGIN_ID}))
    _CREATED.append(kernel)
    return kernel


def _advertised(request: Mapping[str, object]) -> set[str]:
    tools = request.get("tools")
    assert isinstance(tools, list)
    return {str(tool["function"]["name"]) for tool in tools if isinstance(tool, dict)}


def _results(kernel: Kernel, session_id: str) -> list[Mapping[str, object]]:
    results: list[Mapping[str, object]] = []
    for event in kernel.events_after(session_id):
        if event.event_type in ("kernel.tool_completed", "kernel.tool_interrupted"):
            payload = event.payload.get("result")
            if isinstance(payload, Mapping):
                results.append(payload)
    return results


def test_overlay_denies_mutation_and_hides_write_tools(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True, overlay_command=True)
    replies = [
        # The model is asked to write while the overlay is active: the tool is not even offered.
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="no mutation attempted"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-plan")
        entered = kernel.invoke_command("sess-plan", OVERLAY_COMMAND, {"action": "on"}, expected_revision=kernel.revision_id)
        assert entered.status == "ok", entered.error
        assert kernel.store.session_overlays("sess-plan") == ("read-only",)

        result = kernel.run("try to write", session_id="sess-plan")
        assert result.status == "completed"
        # The write-tier tool was filtered out of the model's catalog.
        assert PROBE_TOOL not in _advertised(server.requests[-1])
        denied = [item for item in _results(kernel, "sess-plan") if item.get("status") == "error"]
        assert denied and "read-only policy" in str(denied[-1].get("error"))
        # No side effect: the denied probe never got to write its state note.
        state = {row.owner: row for row in kernel.store.plugin_state_rows("session", "sess-plan")}
        assert PLUGIN_ID not in state


def test_overlay_clear_restores_mutation_but_never_clears_a_frozen_policy(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", overlay_command=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="wrote"), _turn(output="still restricted")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-plan")
        assert kernel.invoke_command("sess-plan", OVERLAY_COMMAND, {"action": "on"}, expected_revision=kernel.revision_id).status == "ok"
        assert kernel.invoke_command("sess-plan", OVERLAY_COMMAND, {"action": "off"}, expected_revision=kernel.revision_id).status == "ok"
        assert kernel.store.session_overlays("sess-plan") == ()
        # With no frozen policy, clearing the overlay restores mutation.
        assert kernel.run("write now", session_id="sess-plan").status == "completed"
        state = {row.owner: row for row in kernel.store.plugin_state_rows("session", "sess-plan")}
        assert state[PLUGIN_ID].payload.get("note") == "v1"

    with _server([_turn(_call("probe-2", PROBE_TOOL)), _turn(output="denied")]) as frozen_server:
        frozen = tmp_path / "frozen"
        _configure(frozen / "workspace", frozen_server, source, read_only=True)
        kernel = open_kernel(frozen / "workspace", state_dir=frozen / "state", trusted_plugins=frozenset({PLUGIN_ID}))
        _CREATED.append(kernel)
        kernel.create_session("sess-frozen")
        assert kernel.invoke_command("sess-frozen", OVERLAY_COMMAND, {"action": "on"}, expected_revision=kernel.revision_id).status == "ok"
        assert kernel.invoke_command("sess-frozen", OVERLAY_COMMAND, {"action": "off"}, expected_revision=kernel.revision_id).status == "ok"
        assert kernel.store.session_overlays("sess-frozen") == ()
        # The frozen kernel policy still denies the write tier even with no overlay left.
        assert kernel.run("write anyway", session_id="sess-frozen").status == "completed"
        denied = [item for item in _results(kernel, "sess-frozen") if item.get("status") == "error"]
        assert denied and "read-only policy" in str(denied[-1].get("error"))


def test_model_path_cannot_set_or_clear_the_overlay(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [_turn(_call("overlay-1", SERVICES_TOOL, action="overlay")), _turn(output="done")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-model")
        assert kernel.run("set the overlay from a tool", session_id="sess-model").status == "completed"
        failed = [item for item in _results(kernel, "sess-model") if item.get("status") == "error"]
        assert failed, "the model-driven overlay call must be rejected"
        # The kernel guarantee: no overlay exists, whatever the plugin-side error text is.
        assert kernel.store.session_overlays("sess-model") == ()


def test_overlay_survives_a_capability_reload(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", overlay_command=True)
    replies = [_turn(output="handled"), _turn(output="v2 denied")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-reload-plan")
        assert kernel.invoke_command("sess-reload-plan", OVERLAY_COMMAND, {"action": "on"}, expected_revision=kernel.revision_id).status == "ok"
        write_reload_plugin(source, tool_output="v2", overlay_command=True)
        switched = kernel.invoke_command("sess-reload-plan", "kernel.reload", {}, expected_revision=kernel.revision_id)
        assert switched.status == "ok", switched.error
        assert kernel.store.session_overlays("sess-reload-plan") == ("read-only",)
        result = kernel.run("write after reload", session_id="sess-reload-plan")
        assert result.status == "completed"
        assert PROBE_TOOL not in _advertised(server.requests[-1])


def test_deactivating_the_owner_revokes_its_overlay_in_the_switch(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", overlay_command=True)
    replies = [_turn(output="handled")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-disable")
        assert kernel.invoke_command("sess-disable", OVERLAY_COMMAND, {"action": "on"}, expected_revision=kernel.revision_id).status == "ok"
        # A foreign owner's overlay must not be touched by another owner's deactivation.
        kernel.store.set_session_overlay("sess-disable", "other-owner")
        config = json.loads((tmp_path / "workspace" / "voidcode.json").read_text(encoding="utf-8"))
        config["plugins"][PLUGIN_ID]["enabled"] = False
        (tmp_path / "workspace" / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")

        switched = kernel.invoke_command("sess-disable", "kernel.reload", {}, expected_revision=kernel.revision_id)
        assert switched.status == "ok", switched.error
        # The disabled owner's constraint is gone with it; the other owner's remains.
        # Exactly the foreign owner's row survives (one tier string), so the disabled owner's
        # overlay was revoked while unrelated owners kept theirs.
        assert kernel.store.session_overlays("sess-disable") == ("read-only",)
        kernel.store.clear_session_overlay("sess-disable", "other-owner")
        assert kernel.store.session_overlays("sess-disable") == ()
