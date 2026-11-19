"""Keyboard cancellation reaches an actual local run and durable history."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from tests.unit.test_core_tui import _StubTerminal
from voidcode.bootstrap import bundled_sources, open_core
from voidcode.tui.local_client import CoreLocalClient
from voidcode.tui.terminal_app import CoreTuiApp


@pytest.mark.parametrize("mode", ["result", "error", "unsolicited"])
def test_escape_interrupts_local_run_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    source = tmp_path / "slow"
    source.mkdir()
    (source / "__init__.py").write_text(
        """from pathlib import Path
import time
from voidcode.sdk.plugin import Plugin
from voidcode.sdk.provider import ProviderExecutionError, ProviderTurnResult

class Slow:
    name = "slow"
    def __init__(self, mode):
        self.mode = mode
    def propose_turn(self, request):
        Path(__file__).with_name("started").touch()
        if self.mode != "unsolicited":
            while not request.abort_signal.cancelled:
                time.sleep(0.01)
        if self.mode == "error":
            raise ProviderExecutionError("cancelled", "slow", "test", "provider cancelled")
        return ProviderTurnResult(output="", done_reason="cancelled")

def create_plugin(config):
    return Plugin(providers=(Slow(config["mode"]),))
""",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugins: dict[str, object] = {name: {"enabled": False} for name in bundled_sources()}
    plugins["slow"] = {"path": str(source), "config": {"mode": mode}}
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {"schema_version": 1, "model": {"model": "slow/test"}, "providers": {"slow": {"base_url": "http://127.0.0.1/unused"}}, "plugins": plugins}
        ),
        encoding="utf-8",
    )
    with open_core(workspace, state_dir=tmp_path / "state") as core:
        terminal = _StubTerminal()
        app = CoreTuiApp(CoreLocalClient(core, workspace=workspace), terminal=terminal)
        app._init_session()
        app._start_event_stream()
        try:
            app._dispatch_user_prompt("wait for escape")
            deadline = time.monotonic() + 5
            while not (source / "started").exists() and time.monotonic() < deadline:
                app._drain_events()
                time.sleep(0.01)
            assert (source / "started").exists()
            if mode != "unsolicited":
                app._handle_input_bytes(b"\x1b")
                time.sleep(0.1)  # let the lone-Esc decoder disambiguate the byte
                app._handle_input_bytes(b"")
            expected_status = "failed" if mode == "unsolicited" else "interrupted"
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                app._drain_events()
                session = core.session(app.session_id)
                if session.status == expected_status and app._session_cursors.get(app.session_id, 0) >= session.last_event_sequence:
                    break
                time.sleep(0.01)
            assert core.session(app.session_id).status == expected_status
            terminal_events = [
                event for event in core.store.events(app.session_id) if event.event_type in ("core.run_failed", "core.run_interrupted")
            ]
            assert len(terminal_events) == 1
            if mode == "unsolicited":
                assert sum("provider did not complete its turn: cancelled" in row for row in terminal.committed) == 1
            else:
                assert terminal_events[0].payload["stop_cause"] == "user_stop"
                assert not any("Error:" in row or "usage unknown" in row for row in terminal.committed)
            assert app.status_line == ""
            assert not app._run_active
            assert not app._cancel_pending
            assert not any("Cancellation requested" in row or "No active run" in row for row in terminal.committed)
        finally:
            app._stop_event.set()
            if app._stream_cancel is not None:
                app._stream_cancel.close()
            if app._stream_thread is not None:
                app._stream_thread.join(timeout=2)
                assert not app._stream_thread.is_alive()
