"""Commands keep cancellation ownership and supervisor observations through completion."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from voidcode.bootstrap import bundled_sources, open_core
from voidcode.core.engine import Core
from voidcode.core.processes import ProcessRegistry
from voidcode.sdk.commands import CommandResult
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.observed_effects import ObservedEffects
from voidcode.sdk.tools import Change

_SOURCE = """
from uuid import uuid4
from voidcode.sdk.commands import CommandDefinition, CommandResult, ConfirmationRequest
from voidcode.sdk.plugin import Plugin, StateSchema
from voidcode.sdk.tools import ToolEffect

class Probe:
    definition = CommandDefinition(
        name="probe", description="Exercise a command process",
        effects=frozenset({ToolEffect.EXECUTE, ToolEffect.SESSION}),
        argument_schema={"type": "object", "properties": {
            "argv": {"type": "array", "items": {"type": "string"}},
            "confirm": {"type": "boolean"}, "fail": {"type": "boolean"}, "identity": {"type": "boolean"}
        }, "required": ["argv"], "additionalProperties": False},
    )
    def invoke(self, invocation, services):
        if invocation.arguments.get("identity"):
            context = invocation.context
            return CommandResult(status="ok", data={
                "session_id": context.session_id, "run_id": context.run_id,
                "invocation_id": context.invocation_id, "revision_id": context.revision_id,
                "actor": context.actor, "logical_turn": context.logical_turn,
                "root_action_id": context.root_action_id, "initial_root_budget": context.initial_root_budget,
                "origin_visible_tools": None if context.origin_visible_tools is None else list(context.origin_visible_tools),
                "origin_readonly": context.origin_readonly,
            })
        if invocation.arguments.get("confirm") and invocation.context.actor != "user":
            request_id = uuid4().hex
            return CommandResult(status="pending_confirmation", confirmation=ConfirmationRequest(
                request_id=request_id, owner=services.owner, command_name="probe",
                arguments=invocation.arguments, ui_type="confirmation", ui_data={"request_id": request_id, "message": "Run probe?"}
            ))
        result = services.run_process(invocation.arguments["argv"], timeout_seconds=30)
        if invocation.arguments.get("fail"):
            raise RuntimeError("failure after external effect")
        _, version = services.get_state("session")
        services.replace_state({"completed": True}, expected_version=version, scope="session")
        services.stage_event("finished", {"cancelled": result.cancelled})
        return CommandResult(status="ok", output=result.stdout)

def create_plugin(config):
    return Plugin(commands=(Probe(),), state=StateSchema(version=1))
"""


@pytest.fixture
def core(tmp_path: Path) -> Iterator[Core]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugin = tmp_path / "probe"
    plugin.mkdir()
    (plugin / "__init__.py").write_text(_SOURCE, encoding="utf-8")
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins["probe"] = {"path": str(plugin)}
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": plugins}), encoding="utf-8")
    with open_core(workspace, state_dir=tmp_path / "state") as instance:
        instance.create_session("s")
        yield instance


def _invoke(core: Core, arguments: dict[str, object]) -> CommandResult:
    return core.invoke_command("s", "probe", arguments, expected_revision=core.revision_id)


def _wait_marker(marker: Path) -> None:
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.exists(), "command process never started"


def test_one_captured_invocation_identity_reaches_the_plugin(core: Core) -> None:
    result = _invoke(core, {"argv": [], "identity": True})
    assert result.status == "ok", result.as_payload()
    assert result.data is not None
    data = json_wire_object(result.data)
    assert data["session_id"] == "s"
    assert data["run_id"] and data["invocation_id"] and data["logical_turn"]
    assert data["revision_id"] == core.revision_id
    assert data["actor"] == "client"
    assert data["root_action_id"]
    assert data["initial_root_budget"] is None
    assert data["origin_visible_tools"] == ["core_reload"]
    assert data["origin_readonly"] is False
    completed = [event for event in core.events_after("s") if event.event_type == "core.command_completed"]
    assert len(completed) == 1


@pytest.mark.parametrize("confirmed", [False, True])
def test_cancel_stops_command_and_discards_staged_work(core: Core, monkeypatch: pytest.MonkeyPatch, confirmed: bool) -> None:
    marker = core.workspace / "started"
    late = core.workspace / "late"
    original = ProcessRegistry.run

    def observed_run(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        return replace(result, effects=ObservedEffects(changes=(Change(str(marker), "create"),)))

    monkeypatch.setattr(ProcessRegistry, "run", observed_run)
    script = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30); Path({str(late)!r}).touch()"
    arguments = {"argv": [sys.executable, "-c", script], "confirm": confirmed}
    request_id = None
    if confirmed:
        pending = _invoke(core, arguments)
        assert pending.confirmation is not None
        request_id = pending.confirmation.request_id
    results: list[CommandResult] = []

    def invoke() -> None:
        if request_id is None:
            results.append(_invoke(core, arguments))
        else:
            results.append(core.confirm_action("s", request_id, "allow", expected_revision=core.revision_id))

    worker = threading.Thread(target=invoke, daemon=True)
    worker.start()
    try:
        _wait_marker(marker)
        core.create_session("other")
        assert core.cancel_session("other") is False
        assert worker.is_alive()
        assert core.cancel_session("s") is True
        worker.join(timeout=10)
        assert not worker.is_alive(), "cancellation did not stop the command process"
        assert not late.exists()
        assert len(results) == 1 and results[0].status == "error"
        assert "cancelled" in (results[0].error or "")
        assert core.store.plugin_state("probe", "session", "s")[0] is None
        events = core.events_after("s")
        assert not [e for e in events if e.event_type == "plugin.probe.finished"]
        completed = [e for e in events if e.event_type == "core.command_completed"]
        assert len(completed) == 1
        assert completed[0].payload["cancelled"] is True
        assert completed[0].payload["result"]["status"] == "error"
        assert json_wire_object(completed[0].payload)["observed_changes"] == [{"path": str(marker), "operation": "create"}]
        assert core.cancel_session("s") is False
        # A cancelled command must leave no stale cancellation for the next invocation.
        assert _invoke(core, {"argv": [sys.executable, "-c", "print('fresh')"]}).output == "fresh\n"
    finally:
        core.cancel_session("s")
        worker.join(timeout=5)


def test_finished_command_does_not_unregister_another_live_command(core: Core) -> None:
    marker = core.workspace / "long-started"
    script = f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(30)"
    results: list[CommandResult] = []
    worker = threading.Thread(target=lambda: results.append(_invoke(core, {"argv": [sys.executable, "-c", script]})), daemon=True)
    worker.start()
    try:
        _wait_marker(marker)
        assert _invoke(core, {"argv": [sys.executable, "-c", "print('short')"]}).output == "short\n"
        assert worker.is_alive()
        assert core.cancel_session("s") is True
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert len(results) == 1 and results[0].status == "error"
        assert "cancelled" in (results[0].error or "")
    finally:
        core.cancel_session("s")
        worker.join(timeout=5)


@pytest.mark.parametrize("confirmed", [False, True])
@pytest.mark.parametrize("fail", [False, True])
def test_command_persists_supervisor_changes_even_after_throw(core: Core, monkeypatch: pytest.MonkeyPatch, confirmed: bool, fail: bool) -> None:
    original = ProcessRegistry.run

    def observed_run(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        # Exercise the supervisor-report boundary without requiring a native sandbox.
        change = Change(str(core.workspace / "changed.txt"), "create", content_hash="observed-hash")
        return replace(result, effects=ObservedEffects(changes=(change, change, Change("/tmp/scratch", scratch=True))))

    monkeypatch.setattr(ProcessRegistry, "run", observed_run)
    arguments = {
        "argv": [sys.executable, "-c", "from pathlib import Path; Path('changed.txt').write_text('changed')"],
        "confirm": confirmed,
        "fail": fail,
    }
    result = _invoke(core, arguments)
    if confirmed:
        assert result.confirmation is not None
        result = core.confirm_action("s", result.confirmation.request_id, "allow", expected_revision=core.revision_id)
    assert result.status == ("error" if fail else "ok")
    assert (core.workspace / "changed.txt").read_text() == "changed"
    completed = [e for e in core.events_after("s") if e.event_type == "core.command_completed"]
    assert len(completed) == 1
    payload = json_wire_object(completed[0].payload)
    assert payload["observed_changes"] == [{"path": str(core.workspace / "changed.txt"), "operation": "create", "content_hash": "observed-hash"}]
    assert payload["cancelled"] is False
