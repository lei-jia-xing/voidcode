"""P3 activation: same-session revision switches, migration, barriers, trust, and restart restore."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import DEACTIVATE_TOOL, PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.storage.store import PluginStateRow, SqliteStore
from voidcode.sdk.json_values import json_wire_object

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "revision loopback reasoning", "tool_calls": list(calls)},
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


def _configure(
    workspace: Path,
    server: OpenAILoopbackServer,
    plugin_path: Path,
    *,
    plugin_id: str = PLUGIN_ID,
    enabled: bool = True,
    model: str = "endpoint/exact-model",
    approval_mode: str = "yolo",
    extra_plugins: dict[str, object] | None = None,
) -> dict[str, object]:
    workspace.mkdir(parents=True, exist_ok=True)
    # Hermetic: the bundled file tools are irrelevant to admission/activation semantics.
    plugins: dict[str, object] = {"files": {"enabled": False}, plugin_id: {"path": str(plugin_path), "enabled": enabled}}
    if extra_plugins:
        plugins.update(extra_plugins)
    config = {
        "schema_version": 1,
        "model": {"model": model},
        "policy": {"approval_mode": approval_mode, "read_only": False},
        "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
        "plugins": plugins,
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    return config


def _open(tmp_path: Path, *, trusted: frozenset[str] = frozenset({PLUGIN_ID})) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _owner_list(data: Mapping[str, object] | None, key: str) -> list[str]:
    assert data is not None
    value = data.get(key)
    assert isinstance(value, (list, tuple)), value
    return [str(item) for item in value]


def _advertised_tools(request: Mapping[str, object]) -> dict[str, str]:
    tools = json_wire_object(request).get("tools")
    assert isinstance(tools, list)
    advertised: dict[str, str] = {}
    for tool in tools:
        assert isinstance(tool, dict)
        function = tool.get("function")
        assert isinstance(function, dict)
        advertised[str(function["name"])] = str(function["description"])
    return advertised


def _events(core: Core, session_id: str) -> list:
    return list(core.events_after(session_id, after_sequence=0))


def _tool_outputs(core: Core, session_id: str) -> list[str]:
    outputs: list[str] = []
    for event in _events(core, session_id):
        if event.event_type != "core.tool_completed":
            continue
        payload = event.payload
        if not isinstance(payload, Mapping):
            continue
        result = payload.get("result")
        if not isinstance(result, Mapping):
            continue
        output = result.get("output")
        if isinstance(output, Mapping) and isinstance(output.get("text"), str):
            outputs.append(str(output["text"]))
    return outputs


def test_same_session_reload_switches_generation_and_preserves_state(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-1", PROBE_TOOL), _call("reload-1", "core_reload", reason="upgrade the probe")),
        _turn(output="switched"),
        _turn(_call("probe-2", PROBE_TOOL)),
        _turn(output="v2 wrote"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        session = core.create_session("sess-reload")
        before = core.revision_id
        # The candidate is what the configured source says NOW; the running generation keeps v1.
        write_reload_plugin(source, tool_output="v2")
        result = core.run("upgrade", session_id=session.session_id)

        assert result.status == "completed"
        assert result.revision_id != before
        events = _events(core, session.session_id)
        types = [event.event_type for event in events]
        assert types.count("core.activation_requested") == 1
        assert types.count("core.activation_committed") == 1
        # The whole first batch belongs to the old generation, results included.
        first_batch = [event for event in events if event.event_type == "core.tool_completed"][:2]
        assert [event.revision_id for event in first_batch] == [before, before]
        staged_note = "activation request recorded; it is applied after this tool batch finishes"
        assert _tool_outputs(core, session.session_id)[:2] == ["probe=v1", staged_note]
        committed = next(event for event in events if event.event_type == "core.activation_committed")
        assert committed.payload["from_revision_id"] == before
        assert committed.payload["to_revision_id"] == result.revision_id
        assert list(committed.payload["migrated_owners"]) == []
        assert committed.payload["source"] == "model"
        # ...and the following dispatch advertised its tool declarations.
        second_dispatch_tools = _advertised_tools(server.requests[1])
        assert "v2" in second_dispatch_tools[PROBE_TOOL]
        assert core.session(session.session_id).active_revision_id == result.revision_id

        with SqliteStore(tmp_path / "state" / "kernel-v1.sqlite3") as store:
            rows = {row.owner: row for row in store.plugin_state_rows("session", session.session_id)}
            assert rows[PLUGIN_ID].schema_version == 1
            assert rows[PLUGIN_ID].payload == {"note": "v1"}
            assert store.revision(result.revision_id)["private_config_sha256"]

        # The very next turn continues automatically in the new generation and writes its state.
        assert core.run("probe the new revision", session_id=session.session_id).status == "completed"
        assert _tool_outputs(core, session.session_id)[2] == "probe=v2"
        with SqliteStore(tmp_path / "state" / "kernel-v1.sqlite3") as store:
            row = _owner_row(store, "session", session.session_id)
            assert row.payload == {"note": "v2"}


@pytest.mark.parametrize("defect", ["syntax", "migration"])
def test_failed_candidate_keeps_the_old_generation(tmp_path: Path, defect: str) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-0", PROBE_TOOL)),
        _turn(_call("reload-1", "core_reload", reason="try a bad candidate")),
        _turn(output="kept"),
        _turn(_call("probe-2", PROBE_TOOL)),
        _turn(output="still v1"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        session = core.create_session("sess-bad-candidate")
        before = core.revision_id
        if defect == "syntax":
            write_reload_plugin(source, tool_output="v2", syntax_error=True)
        else:
            write_reload_plugin(source, tool_output="v2", state_schema_version=2, migration="raise")

        first = core.run("probe then upgrade", session_id=session.session_id)
        assert first.status == "completed"
        assert first.revision_id == before
        types = [event.event_type for event in _events(core, session.session_id)]
        assert "core.activation_failed" in types
        assert "core.activation_committed" not in types

        # Ordinary work continues on the old generation, with its own code and state.
        second = core.run("probe again", session_id=session.session_id)
        assert second.status == "completed"
        assert second.revision_id == before
        assert _tool_outputs(core, session.session_id)[-1] == "probe=v1"
        with SqliteStore(tmp_path / "state" / "kernel-v1.sqlite3") as store:
            row = _owner_row(store, "session", session.session_id)
            assert row.schema_version == 1
            assert row.payload == {"note": "v1"}


def test_activation_is_blocked_by_a_waiting_session(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-a", PROBE_TOOL)),
        _turn(output="resumed"),
        _turn(output="idle chat"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source, approval_mode="ask")
        core = _open(tmp_path)
        parked = core.create_session("sess-parked")
        idle = core.create_session("sess-idle")
        assert core.run("needs approval", session_id=parked.session_id).status == "waiting"
        before = core.revision_id

        write_reload_plugin(source, tool_output="v2")
        outcome = core.invoke_command(
            idle.session_id,
            "core.reload",
            {},
            expected_revision=core.command_catalog().revision_id,
        )
        assert outcome.status == "error"
        assert "blocked" in (outcome.error or "")
        assert "sess-parked" in (outcome.error or "")
        assert core.revision_id == before
        blocked_events = [event for event in _events(core, idle.session_id) if event.event_type == "core.activation_failed"]
        assert blocked_events and blocked_events[0].payload["blockers"]

        # Nothing was cancelled: the parked approval still resolves and completes on v1.
        approval = next(event for event in _events(core, parked.session_id) if event.event_type == "core.approval_requested")
        resumed = core.resume(parked.session_id, approval_request_id=approval.payload["request_id"], decision="allow")
        assert resumed.status == "completed"
        assert _tool_outputs(core, parked.session_id)[0] == "probe=v1"


def _owner_row(store: SqliteStore, scope: str, scope_key: str) -> PluginStateRow:
    """This plugin's own state row: the bundled plugins keep rows of their own in the same session."""
    rows = [row for row in store.plugin_state_rows(scope, scope_key) if row.owner == PLUGIN_ID]
    assert len(rows) == 1, [row.owner for row in store.plugin_state_rows(scope, scope_key)]
    return rows[0]


def test_disable_pauses_owner_state_and_reenable_restores_the_paused_copy(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", deactivation="pause", observe=True)
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="v1 written"),
        _turn(output="ordinary chat"),
        _turn(_call("note-1", DEACTIVATE_TOOL)),
        _turn(output="read back"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        session = core.create_session("sess-lifecycle")
        assert core.run("write state", session_id=session.session_id).status == "completed"
        tip = core.session(session.session_id).last_event_sequence

        _configure(tmp_path / "workspace", server, source, enabled=False)
        disabled = core.invoke_command(session.session_id, "core.reload", {}, expected_revision=core.command_catalog().revision_id)
        assert disabled.status == "ok", disabled.error
        assert disabled.data is not None
        assert _owner_list(disabled.data, "deactivated_owners") == [PLUGIN_ID]
        assert PROBE_TOOL not in {command.name for command in core.command_catalog().commands}
        assert core.run("ordinary chat", session_id=session.session_id).status == "completed"
        disabled_tools = _advertised_tools(server.requests[-1])
        assert PROBE_TOOL not in disabled_tools and DEACTIVATE_TOOL not in disabled_tools

        with SqliteStore(tmp_path / "state" / "kernel-v1.sqlite3") as store:
            row = _owner_row(store, "session", session.session_id)
            assert row.payload == {"note": "v1", "paused": True, "tip": tip}
            assert row.schema_version == 1

        # Re-enabling migrates the paused copy; nothing resumes implicitly.
        _configure(tmp_path / "workspace", server, source)
        write_reload_plugin(source, tool_output="v2", deactivation="pause", observe=True)
        enabled = core.invoke_command(session.session_id, "core.reload", {}, expected_revision=core.command_catalog().revision_id)
        assert enabled.status == "ok", enabled.error
        assert enabled.data is not None
        assert _owner_list(enabled.data, "migrated_owners") == []
        assert _owner_list(enabled.data, "deactivated_owners") == []
        with SqliteStore(tmp_path / "state" / "kernel-v1.sqlite3") as store:
            row = _owner_row(store, "session", session.session_id)
            assert row.schema_version == 1
            assert row.payload == {"note": "v1", "paused": True, "tip": tip}
            assert row.version >= 2
        assert core.run("read state", session_id=session.session_id).status == "completed"
        assert json.loads(_tool_outputs(core, session.session_id)[-1]) == {
            "note": "v1",
            "paused": True,
            "tip": tip,
        }


def test_restart_restores_the_persisted_generation_until_an_explicit_reload(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="restart 1"),
        _turn(_call("probe-2", PROBE_TOOL)),
        _turn(output="restart 2"),
        _turn(_call("probe-3", PROBE_TOOL)),
        _turn(output="restart 3"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
        session = core.create_session("sess-restart")
        first = core.run("write v1", session_id=session.session_id)
        assert first.status == "completed"
        persisted = first.revision_id
        core.close()

        # Changed source AND changed model: the recorded generation and model are restored, but
        # the current plugin source is what runs (no byte pin); only an explicit reload moves the id.
        write_reload_plugin(source, tool_output="v2")
        _configure(tmp_path / "workspace", server, source, model="endpoint/other-model")
        restarted = open_core(tmp_path / "workspace", state_dir=tmp_path / "state")
        _CREATED.append(restarted)
        assert restarted.revision_id == persisted
        assert restarted.config.model.model == "endpoint/exact-model"
        resumed = restarted.run("still v1", session_id=session.session_id)
        assert resumed.status == "completed"
        assert resumed.revision_id == persisted
        assert _tool_outputs(restarted, session.session_id)[-1] == "probe=v2"
        assert all(request["model"] == "exact-model" for request in server.requests)

        switched = restarted.invoke_command(session.session_id, "core.reload", {}, expected_revision=restarted.command_catalog().revision_id)
        assert switched.status == "ok", switched.error
        after = restarted.run("now v2", session_id=session.session_id)
        assert after.status == "completed"
        assert after.revision_id != persisted
        assert _tool_outputs(restarted, session.session_id)[-1] == "probe=v2"


def test_new_observer_subscription_never_receives_old_facts(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    secondary = tmp_path / "observer"
    write_reload_plugin(source, tool_output="v1", observe=True)
    write_reload_plugin(
        secondary,
        plugin_id="observer-secondary",
        tool_output="secondary",
        observe=True,
        tools=False,
    )
    replies = [
        _turn(_call("probe-1", PROBE_TOOL)),
        _turn(output="v1 done"),
        _turn(_call("probe-2", PROBE_TOOL)),
        _turn(output="v2 done"),
    ]
    with _server(replies) as server:
        _configure(
            tmp_path / "workspace",
            server,
            source,
            extra_plugins={"observer-secondary": {"path": str(secondary), "enabled": False}},
        )
        core = _open(tmp_path, trusted=frozenset({PLUGIN_ID, "observer-secondary"}))
        session = core.create_session("sess-observers")
        assert core.run("write v1", session_id=session.session_id).status == "completed"
        pre_activation_tip = core.session(session.session_id).last_event_sequence

        # Enabling a second observer must not retro-deliver facts produced before it existed.
        _configure(
            tmp_path / "workspace",
            server,
            source,
            extra_plugins={"observer-secondary": {"path": str(secondary), "enabled": True}},
        )
        write_reload_plugin(source, tool_output="v2", observe=True)
        switched = core.invoke_command(session.session_id, "core.reload", {}, expected_revision=core.command_catalog().revision_id)
        assert switched.status == "ok", switched.error
        assert core.run("write v2", session_id=session.session_id).status == "completed"

        with SqliteStore(tmp_path / "state" / "kernel-v1.sqlite3") as store:
            facts = [event.sequence for event in store.events(session.session_id) if event.event_type == "core.tool_completed"]
            old_facts = [sequence for sequence in facts if sequence <= pre_activation_tip]
            new_facts = [sequence for sequence in facts if sequence > pre_activation_tip]
            assert old_facts and new_facts
            # The pre-existing owner already received each old fact exactly once.
            assert all(store.has_observer_receipt(session.session_id, sequence, PLUGIN_ID) for sequence in old_facts)
            # The new owner has no receipt for any fact from before it was admitted...
            assert not any(store.has_observer_receipt(session.session_id, sequence, "observer-secondary") for sequence in old_facts)
            # ...but it does receive facts produced under its own generation.
            assert any(store.has_observer_receipt(session.session_id, sequence, "observer-secondary") for sequence in new_facts)
            workspace_state = {row.owner: row for row in store.plugin_state_rows("workspace", str(tmp_path / "workspace"))}
            assert "observer-secondary" in workspace_state
            observed = workspace_state["observer-secondary"].payload.get("observed")
            assert isinstance(observed, int) and observed >= 1


def test_failed_cold_start_adopts_nothing_and_retries_after_the_source_is_fixed(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", factory_error=True)
    database = tmp_path / "state" / "kernel-v1.sqlite3"
    with _server([_turn(_call("probe-1", PROBE_TOOL)), _turn(output="recovered")]) as server:
        _configure(tmp_path / "workspace", server, source)
        with pytest.raises(ValueError, match="factory failed"):
            _open(tmp_path)
        with sqlite3.connect(database) as connection:
            assert connection.execute("SELECT COUNT(*) FROM workspace_state").fetchone()[0] == 0
            # A failed cold start publishes no generation at all.
            assert connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0] == 0

        # Fixing the SAME configured source is enough: no database deletion, no trust step.
        write_reload_plugin(source, tool_output="v1")
        core = _open(tmp_path)
        session = core.create_session("sess-cold-start")
        result = core.run("workspace recovered", session_id=session.session_id)
        assert result.status == "completed"
        assert _tool_outputs(core, session.session_id)[0] == "probe=v1"
        assert core.store.workspace_current(str(tmp_path / "workspace")) == core.revision_id


def test_provider_selection_must_be_a_registered_name(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    with _server([_turn(output="unused")]) as server:
        _configure(tmp_path / "workspace", server, source)
        config_path = tmp_path / "workspace" / "voidcode.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert "endpoint" in config["providers"]

        # A configured provider nobody declares is refused at admission, not at dispatch time.
        config["providers"]["ghost-adapter"] = {"base_url": "http://127.0.0.1:1/v1"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(ValueError, match="is not registered by any admitted plugin"):
            _open(tmp_path)

        # A model naming a provider that is not configured is refused too.
        del config["providers"]["ghost-adapter"]
        config["model"] = {"model": "ghost-adapter/whatever"}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with pytest.raises(ValueError, match="must name a configured provider/model"):
            _open(tmp_path)


def test_prepared_approval_target_ignores_later_source_edits(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [
        _turn(_call("reload-1", "core_reload", reason="approved upgrade")),
        _turn(output="activated"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source, approval_mode="ask")
        core = _open(tmp_path)
        session = core.create_session("sess-approval-target")
        # v2 is pinned when the tool call parks for approval...
        write_reload_plugin(source, tool_output="v2")
        parked = core.run("upgrade", session_id=session.session_id)
        assert parked.status == "waiting"
        approval = parked.pending_approval
        assert approval is not None
        approval_payload = json_wire_object(approval)
        pinned = str(approval_payload["target_revision_id"])
        # ...so a later workspace edit cannot change what the user approved.
        write_reload_plugin(source, tool_output="v3")
        assert core.resume(session.session_id, approval_request_id=str(approval_payload["request_id"]), decision="allow").status == "completed"
        assert core.revision_id == pinned
