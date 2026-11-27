"""A confirmed command reuses the command runtime's commit, delivery, and drain path."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from voidcode.bootstrap import bundled_sources, open_core
from voidcode.config import dumps_toml
from voidcode.core.engine import Core
from voidcode.core.storage.store import ConfirmationStatus
from voidcode.sdk.commands import CommandResult

_SOURCE = """
from voidcode.sdk.commands import CommandDefinition, CommandResult, ConfirmationRequest
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.tools import ToolEffect

class Probe:
    definition = CommandDefinition(
        name="probe",
        description="Confirmation probe",
        argument_schema={"type": "object"},
        effects=frozenset({ToolEffect.WRITE}),
        busy_gate=False,
    )

    def invoke(self, invocation, services):
        if invocation.context.actor != "user":
            request_id = "req-" + invocation.context.session_id
            return CommandResult(status="pending_confirmation", confirmation=ConfirmationRequest(
                request_id=request_id,
                owner=services.owner,
                command_name="probe",
                arguments={},
                ui_type="confirmation",
                ui_data={"request_id": request_id, "message": "Confirm probe?", "actions": ["allow", "deny"]},
            ))
        services.project_window(covered_from_sequence=1, covered_to_sequence=1, replacement="s")
        services.enqueue_continuation({"kind": "confirmed_followup"})
        return CommandResult(status="ok", output="confirmed")

def create_plugin(config):
    return Plugin(commands=(Probe(),))
"""

_OBSERVER = """
from typing import get_args
from voidcode.sdk.hooks import ObservedFactKind
from voidcode.sdk.plugin import Plugin

def observe(fact, context, services):
    services.stage_event("seen", {"kind": type(fact).__name__, "sequence": fact.event_sequence})

def create_plugin(config):
    return Plugin(observer=observe, observed_facts=get_args(ObservedFactKind))
"""


@pytest.fixture
def core(tmp_path: Path) -> Iterator[Core]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    probe = tmp_path / "probe"
    probe.mkdir()
    (probe / "__init__.py").write_text(_SOURCE, encoding="utf-8")
    observer = tmp_path / "observer"
    observer.mkdir()
    (observer / "__init__.py").write_text(_OBSERVER, encoding="utf-8")
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    plugins["probe"] = {"path": str(probe)}
    plugins["observer"] = {"path": str(observer)}
    (workspace / "voidcode.toml").write_text(dumps_toml({"schema_version": 1, "plugins": plugins}), encoding="utf-8")
    with open_core(workspace, state_dir=tmp_path / "state") as instance:
        instance.create_session("s")
        yield instance


def _pending(core: Core) -> CommandResult:
    pending = core.invoke_command("s", "probe", {}, expected_revision=core.revision_id)
    assert pending.status == "pending_confirmation", pending.as_payload()
    assert pending.confirmation is not None
    return pending


def test_confirmation_allow_commits_decision_delivers_fact_and_drains(core: Core) -> None:
    pending = _pending(core)
    request_id = pending.confirmation.request_id

    result = core.confirm_action("s", request_id, "allow", expected_revision=core.revision_id)
    assert result.status == "ok", result.as_payload()
    assert result.output == "confirmed"

    events = core.events_after("s")
    decided = [event for event in events if event.event_type == "core.confirmation_decided"]
    assert len(decided) == 1
    assert decided[0].payload["request_id"] == request_id
    assert decided[0].payload["decision"] == "allow"
    completed = [event for event in events if event.event_type == "core.command_completed"]
    assert len(completed) == 1
    assert completed[0].payload["name"] == "probe"
    projected = next(event for event in events if event.event_type == "core.window_projected")
    assert projected.payload["eligible_owners"] == ("observer",)
    assert projected.sequence < completed[0].sequence

    seen = [event for event in events if event.event_type == "plugin.observer.seen" and event.payload.get("kind") == "ContextProjectedFact"]
    assert len(seen) == 1
    assert seen[0].sequence > projected.sequence

    # The confirmed command's staged continuation was auto-driven, not left pending.
    assert core.store.pending_continuations("s") == ()
    assert core.store.claimed_continuations("s") == ()
    assert [event for event in events if event.event_type == "core.run_failed"]


def test_confirmation_deny_commits_only_the_decision(core: Core) -> None:
    pending = _pending(core)
    request_id = pending.confirmation.request_id

    result = core.confirm_action("s", request_id, "deny", expected_revision=core.revision_id)
    assert result.status == "ok"
    assert result.output == "Action denied by user"
    record = core.store.confirmation(request_id)
    assert record is not None and record.status is ConfirmationStatus.DENIED

    events = core.events_after("s")
    decided = [event for event in events if event.event_type == "core.confirmation_decided"]
    assert len(decided) == 1
    assert decided[0].payload["decision"] == "deny"
    assert not [event for event in events if event.event_type == "core.command_completed"]
    assert not [event for event in events if event.event_type == "core.window_projected"]


def test_confirmation_read_only_refusal_marks_stale_without_invoking(core: Core) -> None:
    pending = _pending(core)
    request_id = pending.confirmation.request_id
    core.store.set_session_overlay("s", "probe")

    result = core.confirm_action("s", request_id, "allow", expected_revision=core.revision_id)
    assert result.status == "error"
    assert result.error == "read-only policy denies command write effects"
    record = core.store.confirmation(request_id)
    assert record is not None and record.status is ConfirmationStatus.STALE

    events = core.events_after("s")
    decided = [event for event in events if event.event_type == "core.confirmation_decided"]
    assert len(decided) == 1
    assert decided[0].payload["decision"] == "stale"
    assert not [event for event in events if event.event_type == "core.command_completed"]
    assert not [event for event in events if event.event_type == "core.window_projected"]


def test_confirmation_rejects_a_stale_displayed_revision_before_resolving(core: Core) -> None:
    pending = _pending(core)
    request_id = pending.confirmation.request_id

    with pytest.raises(ValueError, match="stale"):
        core.confirm_action("s", request_id, "allow", expected_revision="revision-never-displayed")

    record = core.store.confirmation(request_id)
    assert record is not None and record.status is ConfirmationStatus.PENDING
    assert not [event for event in core.events_after("s") if event.event_type == "core.confirmation_decided"]
