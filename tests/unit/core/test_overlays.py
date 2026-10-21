"""C4 acceptance: owner-owned restrictive overlays (plan mode) tighten only, durably."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import OVERLAY_COMMAND, PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

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


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
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


def _open(tmp_path: Path) -> Core:
    core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _advertised(request: Mapping[str, object]) -> set[str]:
    tools = request.get("tools")
    assert isinstance(tools, list)
    return {str(tool["function"]["name"]) for tool in tools if isinstance(tool, dict)}


def _results(core: Core, session_id: str) -> list[Mapping[str, object]]:
    results: list[Mapping[str, object]] = []
    for event in core.events_after(session_id):
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
        core = _open(tmp_path)
        core.create_session("sess-plan")
        entered = core.invoke_command("sess-plan", OVERLAY_COMMAND, {"action": "on"}, expected_revision=core.revision_id)
        assert entered.status == "ok", entered.error
        assert core.store.session_overlays("sess-plan") == ("read-only",)

        result = core.run("try to write", session_id="sess-plan")
        assert result.status == "completed"
        # The write-tier tool was filtered out of the model's catalog.
        assert PROBE_TOOL not in _advertised(server.requests[-1])
        denied = [item for item in _results(core, "sess-plan") if item.get("status") == "error"]
        assert denied and "read-only policy" in str(denied[-1].get("error"))
        # No side effect: the denied probe never got to write its state note.
        state = {row.owner: row for row in core.store.plugin_state_rows("session", "sess-plan")}
        assert PLUGIN_ID not in state


def test_overlay_clear_restores_mutation_but_never_clears_a_frozen_policy(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", overlay_command=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="wrote"), _turn(output="still restricted")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-plan")
        assert core.invoke_command("sess-plan", OVERLAY_COMMAND, {"action": "on"}, expected_revision=core.revision_id).status == "ok"
        assert core.invoke_command("sess-plan", OVERLAY_COMMAND, {"action": "off"}, expected_revision=core.revision_id).status == "ok"
        assert core.store.session_overlays("sess-plan") == ()
        # With no frozen policy, clearing the overlay restores mutation.
        assert core.run("write now", session_id="sess-plan").status == "completed"
        state = {row.owner: row for row in core.store.plugin_state_rows("session", "sess-plan")}
        assert state[PLUGIN_ID].payload.get("note") == "v1"

    with _server([_turn(_call("probe-2", PROBE_TOOL)), _turn(output="denied")]) as frozen_server:
        frozen = tmp_path / "frozen"
        _configure(frozen / "workspace", frozen_server, source, read_only=True)
        core = open_core(frozen / "workspace", state_dir=frozen / "state")
        _CREATED.append(core)
        core.create_session("sess-frozen")
        assert core.invoke_command("sess-frozen", OVERLAY_COMMAND, {"action": "on"}, expected_revision=core.revision_id).status == "ok"
        assert core.invoke_command("sess-frozen", OVERLAY_COMMAND, {"action": "off"}, expected_revision=core.revision_id).status == "ok"
        assert core.store.session_overlays("sess-frozen") == ()
        # The frozen core policy still denies the write tier even with no overlay left.
        assert core.run("write anyway", session_id="sess-frozen").status == "completed"
        denied = [item for item in _results(core, "sess-frozen") if item.get("status") == "error"]
        assert denied and "read-only policy" in str(denied[-1].get("error"))


def test_overlay_survives_a_capability_reload(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", overlay_command=True)
    replies = [_turn(output="handled"), _turn(output="v2 denied")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-reload-plan")
        assert core.invoke_command("sess-reload-plan", OVERLAY_COMMAND, {"action": "on"}, expected_revision=core.revision_id).status == "ok"
        write_reload_plugin(source, tool_output="v2", overlay_command=True)
        switched = core.invoke_command("sess-reload-plan", "core.reload", {}, expected_revision=core.revision_id)
        assert switched.status == "ok", switched.error
        assert core.store.session_overlays("sess-reload-plan") == ("read-only",)
        result = core.run("write after reload", session_id="sess-reload-plan")
        assert result.status == "completed"
        assert PROBE_TOOL not in _advertised(server.requests[-1])


def test_deactivating_the_owner_revokes_its_overlay_in_the_switch(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", overlay_command=True)
    replies = [_turn(output="handled")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-disable")
        assert core.invoke_command("sess-disable", OVERLAY_COMMAND, {"action": "on"}, expected_revision=core.revision_id).status == "ok"
        # A foreign owner's overlay must not be touched by another owner's deactivation.
        core.store.set_session_overlay("sess-disable", "other-owner")
        config = json.loads((tmp_path / "workspace" / "voidcode.json").read_text(encoding="utf-8"))
        config["plugins"][PLUGIN_ID]["enabled"] = False
        (tmp_path / "workspace" / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")

        switched = core.invoke_command("sess-disable", "core.reload", {}, expected_revision=core.revision_id)
        assert switched.status == "ok", switched.error
        # The disabled owner's constraint is gone with it; the other owner's remains.
        # Exactly the foreign owner's row survives (one tier string), so the disabled owner's
        # overlay was revoked while unrelated owners kept theirs.
        assert core.store.session_overlays("sess-disable") == ("read-only",)
        core.store.clear_session_overlay("sess-disable", "other-owner")
        assert core.store.session_overlays("sess-disable") == ()


_SESSION_PROBE = """
from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult, ConfirmationRequest
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect


class SessionProbe:
    definition = CommandDefinition(
        name="session_probe",
        description="Stage session state, then attempt a real child process.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        state, version = services.get_state("session")
        services.replace_state({"probed": True}, expected_version=version, scope="session")
        try:
            services.run_process(["touch", "ro-probe-marker.txt"], cwd=invocation.context.workspace)
        except PermissionError as exc:
            return CommandResult(status="ok", output=f"staged; machine refused: {exc}")
        return CommandResult(status="ok", output="staged; machine ran")


class ConfirmProbe:
    definition = CommandDefinition(
        name="confirm_probe",
        description="Ask first; on explicit confirm, stage state then attempt a real child process.",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=frozenset({ToolEffect.SESSION}),
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        if invocation.context.actor != "user":
            request_id = uuid4().hex
            return CommandResult(
                status="pending_confirmation",
                confirmation=ConfirmationRequest(
                    request_id=request_id,
                    owner="probe",
                    command_name=self.definition.name,
                    arguments={},
                    ui_type="confirmation",
                    ui_data={"request_id": request_id, "message": "probe?", "actions": ["allow", "deny"]},
                ),
            )
        state, version = services.get_state("session")
        services.replace_state({"confirmed": True}, expected_version=version, scope="session")
        try:
            services.run_process(["touch", "ro-probe-confirm-marker.txt"], cwd=invocation.context.workspace)
        except PermissionError as exc:
            return CommandResult(status="ok", output=f"staged; machine refused: {exc}")
        return CommandResult(status="ok", output="staged; machine ran")


def create_plugin(config: Mapping[str, object] | None = None) -> Plugin:
    return Plugin(commands=(SessionProbe(), ConfirmProbe()))
"""


def test_confirmed_session_command_under_read_only_stages_state_but_cannot_touch_the_machine(tmp_path: Path) -> None:
    """The confirm callpoint carries no RO exemption: same SESSION staging + machine refusal."""
    source = tmp_path / "probe"
    source.mkdir()
    (source / "__init__.py").write_text(_SESSION_PROBE, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": {"approval_mode": "yolo", "read_only": True},
                "plugins": {"probe": {"path": "../probe"}},
            }
        ),
        encoding="utf-8",
    )
    core = open_core(workspace, state_dir=tmp_path / "state", auto_drain=False)
    _CREATED.append(core)
    try:
        core.create_session("sess-ro-confirm")
        revision = core.revision_id
        pending = core.invoke_command("sess-ro-confirm", "confirm_probe", {}, expected_revision=revision)
        assert pending.status == "pending_confirmation", pending.as_payload()
        assert pending.confirmation is not None
        decided = core.confirm_action("sess-ro-confirm", pending.confirmation.request_id, "allow", expected_revision=revision)
        assert decided.status == "ok", decided.as_payload()
        assert "machine refused" in (decided.output or ""), decided.as_payload()
        stored, _ = core.store.plugin_state("probe", "session", "sess-ro-confirm")
        assert stored is not None and stored.get("confirmed") is True, stored
        assert not (workspace / "ro-probe-confirm-marker.txt").exists()
    finally:
        core.close()
        _CREATED.remove(core)


def test_session_command_under_read_only_stages_state_but_cannot_touch_the_machine(tmp_path: Path) -> None:
    source = tmp_path / "probe"
    source.mkdir()
    (source / "__init__.py").write_text(_SESSION_PROBE, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "policy": {"approval_mode": "yolo", "read_only": True},
                "plugins": {"probe": {"path": "../probe"}},
            }
        ),
        encoding="utf-8",
    )
    core = open_core(workspace, state_dir=tmp_path / "state", auto_drain=False)
    _CREATED.append(core)
    try:
        core.create_session("sess-ro-probe")
        result = core.invoke_command("sess-ro-probe", "session_probe", {}, expected_revision=core.revision_id)
        assert result.status == "ok", result.error
        assert "machine refused" in (result.output or "")
        # The SESSION staging half kept working: state committed.
        stored, _ = core.store.plugin_state("probe", "session", "sess-ro-probe")
        assert stored is not None and stored.get("probed") is True
        # The machine half was refused: no external marker exists.
        assert not (workspace / "ro-probe-marker.txt").exists()
    finally:
        core.close()
        _CREATED.remove(core)
