"""Restart edges of an opaque generation: frozen fact owners and owed state migrations."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.activation import bundled_sources
from voidcode.kernel.bootstrap import adopt_generation, open_kernel
from voidcode.kernel.images import installed_identity, running_package_root
from voidcode.kernel.preflight import verify_preflight

OBSERVER = """
from __future__ import annotations

from collections.abc import Mapping
from voidcode.sdk.plugin import Plugin


def create_plugin(config: Mapping[str, object]) -> Plugin:
    return Plugin(observer=lambda fact, context, services: None, observed_facts={facts!r})
"""

STATER = """
from __future__ import annotations

import json
from collections.abc import Mapping

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult
from voidcode.sdk.plugin import Plugin, StateSchema
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect


class Touch:
    definition = CommandDefinition(
        name="statep.touch",
        description="Store one state version",
        argument_schema={{"type": "object", "properties": {{}}, "additionalProperties": False}},
        effects=(ToolEffect.SESSION,),
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        state, version = services.get_state("session")
        payload = dict(state or {{}})
        payload["count"] = int(payload.get("count", 0)) + 1
        services.replace_state(payload, expected_version=version, scope="session")
        return CommandResult(status="ok", output=json.dumps(payload))


def migrate(payload, from_version, to_version):
    migrated = dict(payload)
    migrated["migrated_from"] = from_version
    return migrated


def create_plugin(config: Mapping[str, object]) -> Plugin:
    return Plugin(commands=(Touch(),), state=StateSchema(version={version}), migrate_state=migrate)
"""


def _write(root: Path, body: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text(body, encoding="utf-8")
    return root


def _config(workspace: Path, plugins: dict[str, object]) -> None:
    disabled = {plugin_id: {"enabled": False} for plugin_id in bundled_sources()}
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": {**disabled, **plugins}}), encoding="utf-8")


def test_fact_owners_are_frozen_on_the_event_across_restart_with_changed_source(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = _write(workspace / "observer_plugin", OBSERVER.format(facts=("run_finished",)))
    replies = [
        openai_response(message={"role": "assistant", "content": "one"}, finish="stop"),
        openai_response(message={"role": "assistant", "content": "two"}, finish="stop"),
    ]
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        (workspace / "voidcode.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "model": {"model": "endpoint/exact-model"},
                    "policy": {"approval_mode": "yolo"},
                    "providers": {"endpoint": {"api_key": "k", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                    "plugins": {
                        **{plugin_id: {"enabled": False} for plugin_id in bundled_sources()},
                        "providers": {"enabled": True},
                        "observer": {"path": "observer_plugin"},
                    },
                }
            ),
            encoding="utf-8",
        )
        kernel = open_kernel(workspace, state_dir=tmp_path / "state", auto_drain=False)
        kernel.create_session("s")
        assert kernel.run("one", session_id="s").status == "completed"
        old = next(event for event in kernel.events_after("s") if event.event_type == "kernel.run_completed")
        assert old.payload["eligible_owners"] == ("observer",)
        revision = kernel.revision_id
        kernel.close()

        # The source now subscribes to a different fact, under the same restored generation.
        _write(source, OBSERVER.format(facts=("usage",)))
        kernel = open_kernel(workspace, state_dir=tmp_path / "state", auto_drain=False)
        assert kernel.revision_id == revision
        assert kernel._eligible_observers(old) == ("observer",)
        assert kernel.run("two", session_id="s").status == "completed"
        events = kernel.events_after("s")
        new_run = [event for event in events if event.event_type == "kernel.run_completed"][-1]
        new_usage = [event for event in events if event.event_type == "kernel.model_turn"][-1]
        assert new_run.payload["eligible_owners"] == ()
        assert new_usage.payload["eligible_owners"] == ("observer",)
        kernel.close()

        # A second restart still reads the frozen owners, never the current source.
        kernel = open_kernel(workspace, state_dir=tmp_path / "state", auto_drain=False)
        assert kernel._eligible_observers(old) == ("observer",)
        assert kernel._eligible_observers(new_usage) == ("observer",)
        kernel.close()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)


def test_plain_restart_refuses_reinterpretation_and_preflight_adopts_the_migration(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    source = _write(workspace / "statep", STATER.format(version=1))
    _config(workspace, {"statep": {"path": "statep"}})

    kernel = open_kernel(workspace, state_dir=state, auto_drain=False)
    kernel.create_session("s")
    assert kernel.invoke_command("s", "statep.touch", {}, expected_revision=kernel.revision_id).status == "ok"
    kernel.close()

    _write(source, STATER.format(version=2))
    try:
        open_kernel(workspace, state_dir=state, auto_drain=False)
    except ValueError as exc:
        assert "will not reinterpret" in str(exc)
    else:  # pragma: no cover - the guard is the subject
        raise AssertionError("plain restart reinterpreted stored state under a newer schema")

    identity = installed_identity()
    report = verify_preflight(
        database=state / "kernel-v1.sqlite3",
        workspace=str(workspace),
        state_dir=state,
        candidate_package_root=running_package_root(),
        candidate_launch_protocol=1,
        image_digest=identity.digest,
        web_bundle=None,
    )
    assert report.image_changed is False and report.schema.unchanged is True
    assert report.migrated_owners == ("statep",)
    assert report.adoption_required is True

    assert adopt_generation(workspace=workspace, state_dir=state, session_id="s")["migrated_owners"] == ["statep"]
    with sqlite3.connect(state / "kernel-v1.sqlite3") as connection:
        row = connection.execute("SELECT schema_version, payload_json FROM plugin_state WHERE scope='session' AND scope_key='s'").fetchone()
    assert row is not None and row[0] == 2 and json.loads(row[1])["migrated_from"] == 1

    kernel = open_kernel(workspace, state_dir=state, auto_drain=False)
    assert kernel.invoke_command("s", "statep.touch", {}, expected_revision=kernel.revision_id).status == "ok"
    # A live kernel owns the exclusive workspace lease: adoption must refuse instead of
    # moving a generation a running process is still serving.
    with pytest.raises(RuntimeError, match="another kernel owns this workspace"):
        adopt_generation(workspace=workspace, state_dir=state, session_id="s")
    kernel.close()


CONFIRMER = """
from __future__ import annotations

from collections.abc import Mapping
from uuid import uuid4

from voidcode.sdk.commands import CommandDefinition, CommandInvocation, CommandResult, ConfirmationRequest
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.services import PluginServices
from voidcode.sdk.tools import ToolEffect


class Mutate:
    definition = CommandDefinition(
        name="mutate.confirm",
        description="Mutate only after an explicit confirmation",
        argument_schema={"type": "object", "properties": {}, "additionalProperties": False},
        effects=(ToolEffect.WRITE,),
    )

    def invoke(self, invocation: CommandInvocation, services: PluginServices) -> CommandResult:
        state, version = services.get_state("session")
        if invocation.context.actor != "user":
            request_id = uuid4().hex
            return CommandResult(
                status="pending_confirmation",
                confirmation=ConfirmationRequest(
                    request_id=request_id,
                    owner="confirmer",
                    command_name=self.definition.name,
                    arguments={},
                    ui_type="confirmation",
                    ui_data={"request_id": request_id, "message": "mutate?", "actions": ["allow", "deny"]},
                    expected_state_version=version,
                ),
            )
        services.replace_state({"done": True}, expected_version=version, scope="session")
        return CommandResult(status="ok", output="mutated")


def create_plugin(config: Mapping[str, object]) -> Plugin:
    return Plugin(commands=(Mutate(),))
"""


def test_confirmation_is_provenance_not_a_read_only_exemption(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write(workspace / "confirmer", CONFIRMER)
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": {"confirmer": {"path": "confirmer"}}}), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=tmp_path / "state", auto_drain=False)
    try:
        kernel.create_session("s")
        pending = kernel.invoke_command("s", "mutate.confirm", {}, expected_revision=kernel.revision_id)
        assert pending.status == "pending_confirmation" and pending.confirmation is not None
        # A repeated ordinary call still asks: nothing was applied before the decision.
        again = kernel.invoke_command("s", "mutate.confirm", {}, expected_revision=kernel.revision_id)
        assert again.status == "pending_confirmation" and again.confirmation is not None
        assert kernel.invoke_command("s", "plan", {}, expected_revision=kernel.revision_id).status == "ok"
        denied = kernel.confirm_action("s", pending.confirmation.request_id, "allow", expected_revision=kernel.revision_id)
        assert denied.status == "error" and "read-only policy" in (denied.error or "")
        rows = {row.owner: row for row in kernel.store.plugin_state_rows("session", "s")}
        assert "done" not in (rows["confirmer"].payload if "confirmer" in rows else {})
        assert kernel.invoke_command("s", "plan.off", {}, expected_revision=kernel.revision_id).status == "ok"

        # Goal keeps its own confirmation phase: replacing an unfinished goal needs the user.
        assert kernel.invoke_command("s", "goal.set", {"objective": "first"}, expected_revision=kernel.revision_id).status == "ok"
        replace = kernel.invoke_command("s", "goal.set", {"objective": "second"}, expected_revision=kernel.revision_id)
        assert replace.status == "pending_confirmation" and replace.confirmation is not None
        assert kernel.confirm_action("s", replace.confirmation.request_id, "allow", expected_revision=kernel.revision_id).status == "ok"
        goal_rows = {row.owner: row for row in kernel.store.plugin_state_rows("session", "s")}
        assert goal_rows["goal"].payload.get("objective") == "second"
    finally:
        kernel.close()
