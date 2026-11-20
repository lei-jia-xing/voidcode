"""Live command-input polling: refresh cadence, state retention, stale guards."""

from __future__ import annotations

import time
from typing import Any

from voidcode.sdk.client import CommandCatalog
from voidcode.sdk.commands import CommandDescriptor, CommandInputSnapshot, CommandPresentation
from voidcode.tui import catalog
from voidcode.tui.events import LiveInputReadyEvent
from voidcode.tui.gates import ConfirmationGate, ConfirmationRequested
from voidcode.tui.keys import Key
from voidcode.tui.terminal_app import CoreTuiApp
from voidcode.tui.views import InteractiveChoices, InteractiveConfirmation


class _StubTerminal:
    width = 80
    height = 24
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []
        self.frames: list[list[str]] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        self.frames.append(list(frame.rows))


class _LiveClient:
    """command_input answers from a script; catalog stays one revision."""

    def __init__(self, snapshots: list[CommandInputSnapshot]) -> None:
        self.snapshots = list(snapshots)
        self.calls: list[tuple[str, str, dict[str, object], str]] = []

    def command_catalog(self) -> CommandCatalog:
        return CommandCatalog(
            revision_id="rev-1",
            commands=(CommandDescriptor(name="monitor", owner="p", description="Monitor", has_input=True, default_shortcut="alt+a"),),
        )

    def command_input(self, session_id: str, name: str, expected_revision: str, *, arguments=None) -> CommandInputSnapshot:  # type: ignore[no-untyped-def]
        self.calls.append((session_id, name, dict(arguments or {}), expected_revision))
        assert self.snapshots, "unexpected extra command_input call"
        return self.snapshots.pop(0)


def _choices(ids: list[str], *, refresh_after_ms: int | None = 1000, title: str = "Running subagents") -> CommandInputSnapshot:
    return CommandInputSnapshot(
        revision_id="rev-1",
        presentation=CommandPresentation(
            title=title,
            view_type="choices",
            data={
                "options": [{"id": ident, "label": ident} for ident in ids],
                "action": "monitor",
                "argument_name": "target",
                "prepare_input": True,
            },
            refresh_after_ms=refresh_after_ms,
        ),
    )


def _app(client: _LiveClient) -> tuple[CoreTuiApp, _StubTerminal]:
    terminal = _StubTerminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    app.catalog_cache = client.command_catalog().commands
    app.catalog_revision = "rev-1"
    return app, terminal


def _drain(app: CoreTuiApp, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if app.event_queue.empty() and app._work.pending == 0:
            return
        time.sleep(0.005)
    raise AssertionError("event queue never drained")


def test_refresh_syncs_title_when_roster_gains_children() -> None:
    client = _LiveClient(
        [
            _choices([], title="Running subagents (none)"),
            _choices(["a"], title="Running subagents"),
        ]
    )
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert app.active_overlay.title == "Running subagents (none)"
    app._live_refresh_at = time.monotonic() - 0.01
    app._poll_live_input()
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert [opt.id for opt in app.active_overlay.view.options] == ["a"]
    assert app.active_overlay.title == "Running subagents"


def test_shortcut_mounts_live_view_and_refresh_updates_in_place() -> None:
    client = _LiveClient([_choices(["a", "b"]), _choices(["a", "b", "c"])])
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app.composer.set_text("abcd")
    app.composer._cursor = 2
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert app.composer.value == "abcd"
    first = app.active_overlay
    assert app._live_input is not None
    assert app._live_input["arguments"] == {}
    assert app._live_input["source"] is first
    app.active_overlay.handle_key(Key("down"))
    assert app.active_overlay.selected_id == "b"
    app._live_refresh_at = time.monotonic() - 0.01
    app._poll_live_input()
    _drain(app)
    assert app.active_overlay is first
    assert [opt.id for opt in app.active_overlay.view.options] == ["a", "b", "c"]
    assert app.active_overlay.selected_id == "b"
    assert client.calls[0][:3] == ("s1", "monitor", {})
    assert client.calls[1][:3] == ("s1", "monitor", {})
    # Close restores the exact caret: typing inserts at offset 2.
    app._handle_overlay_key(Key("esc"))
    app._handle_composer_key(Key("Z", text="Z"))
    assert app.composer.value == "abZcd"


def test_prepare_selection_threads_arguments_and_stays_live() -> None:
    client = _LiveClient([_choices(["a"]), _choices(["a"]), _choices(["a"])])
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    app._handle_overlay_key(Key("enter"))
    _drain(app)
    assert client.calls[1] == ("s1", "monitor", {"target": "a"}, "rev-1")
    assert isinstance(app.active_overlay, InteractiveChoices)
    assert app._live_input is not None
    assert app._live_input["arguments"] == {"target": "a"}
    assert app._live_input["source"] is app.active_overlay
    # The next poll reuses the detail args, never resets to {}.
    app._live_refresh_at = time.monotonic() - 0.01
    app._poll_live_input()
    _drain(app)
    assert client.calls[2] == ("s1", "monitor", {"target": "a"}, "rev-1")


def test_close_stops_polling_and_stale_replies_never_remount() -> None:
    client = _LiveClient([_choices(["a"])])
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    app._handle_overlay_key(Key("esc"))
    assert app.active_overlay is None
    assert app._live_input is None
    stale_token = app._follower.begin_live()
    app._follower.begin_live()  # supersede the crafted token: the reply is now stale
    app.event_queue.put(
        LiveInputReadyEvent(
            token=stale_token,
            revision_id="rev-1",
            session_id="s1",
            generation=app._generation,
            action="monitor",
            arguments={},
            view=None,
            error=None,
            refresh_after_ms=None,
        )
    )
    app._drain_events()
    assert app.active_overlay is None


def test_confirmation_suspends_live_and_direct_decision_restores() -> None:
    client = _LiveClient([_choices(["a", "b"])])
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    live = app.active_overlay
    live.handle_key(Key("down"))
    assert live.selected_id == "b"
    gate = ConfirmationGate.from_request(
        __import__("voidcode.sdk.commands", fromlist=["ConfirmationRequest"]).ConfirmationRequest(
            request_id="cancel-1",
            owner="p",
            command_name="monitor.cancel",
            arguments={"target": "b"},
            ui_type="confirmation",
            ui_data={"request_id": "cancel-1", "message": "Cancel b?", "actions": ["allow", "deny"]},
        ),
        revision_id="rev-1",
    )
    app._apply_gate_change(ConfirmationRequested(gate))
    assert isinstance(app.active_overlay, InteractiveConfirmation)
    live_token = app._follower.begin_live()
    app.event_queue.put(
        LiveInputReadyEvent(
            token=live_token,
            revision_id="rev-1",
            session_id="s1",
            generation=app._generation,
            action="monitor",
            arguments={},
            view=None,
            error=None,
            refresh_after_ms=1000,
        )
    )
    app._drain_events()
    assert isinstance(app.active_overlay, InteractiveConfirmation)
    from voidcode.tui.gates import GateDecided

    app._apply_gate_change(GateDecided("cancel-1"))
    assert app.active_overlay is live
    assert live.selected_id == "b"
    assert app._live_input is not None


def test_refresh_error_rearms_from_stored_cadence_and_stays_visible() -> None:
    client = _LiveClient([_choices(["a", "b"])])
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    before = time.monotonic()
    token = app._follower.begin_live()
    app.event_queue.put(
        LiveInputReadyEvent(
            token=token,
            revision_id="rev-1",
            session_id="s1",
            generation=app._generation,
            action="monitor",
            arguments={},
            view=None,
            error="boom",
            refresh_after_ms=None,
        )
    )
    app._drain_events()
    assert "Refresh failed" in app.status_line
    assert app._live_refresh_at > before
    assert isinstance(app.active_overlay, InteractiveChoices)


def test_stale_reply_never_touches_replaced_overlay() -> None:
    client = _LiveClient([_choices(["a"])])
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    stale_token = app._follower.begin_live()
    app._follower.begin_live()  # supersede the crafted token: the reply is now stale
    newer = InteractiveChoices(app.active_overlay.view, revision_id="rev-1")
    app.active_overlay = newer
    app.event_queue.put(
        LiveInputReadyEvent(
            token=stale_token,
            revision_id="rev-1",
            session_id="s1",
            generation=app._generation,
            action="monitor",
            arguments={},
            view=None,
            error=None,
            refresh_after_ms=1000,
        )
    )
    app._drain_events()
    assert app.active_overlay is newer
    assert app.status_line == ""


def test_queued_gates_present_before_suspended_monitor() -> None:
    from voidcode.sdk.commands import ConfirmationRequest
    from voidcode.sdk.views import ConfirmationViewData
    from voidcode.tui.gates import GateDecided

    client = _LiveClient([_choices(["a"])])
    app, _ = _app(client)
    catalog.cache(app, client.command_catalog().commands, "rev-1")
    app._handle_composer_key(Key("alt+a"))
    _drain(app)
    assert isinstance(app.active_overlay, InteractiveChoices)
    live = app.active_overlay
    gate = ConfirmationGate.from_request(
        ConfirmationRequest(
            request_id="cancel-1",
            owner="p",
            command_name="monitor.cancel",
            arguments={"target": "a"},
            ui_type="confirmation",
            ui_data={"request_id": "cancel-1", "message": "Cancel a?", "actions": ["allow", "deny"]},
        ),
        revision_id="rev-1",
    )
    app._apply_gate_change(ConfirmationRequested(gate))
    assert isinstance(app.active_overlay, InteractiveConfirmation)
    other = InteractiveConfirmation(ConfirmationViewData(request_id="other-1", message="Other?", actions=("allow", "deny")), revision_id="rev-1")
    app.queued_confirmations["other-1"] = other
    app._apply_gate_change(GateDecided("cancel-1"))
    assert isinstance(app.active_overlay, InteractiveConfirmation)
    assert app.active_overlay.request_id == "other-1"
    app._apply_gate_change(GateDecided("other-1"))
    assert app.active_overlay is live
