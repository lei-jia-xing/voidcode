import os
import time
from pathlib import Path

from voidcode.runtime.config.loading import RuntimeConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import HelpOverlay
from voidcode.tui.term import Terminal


def test_f1_uses_resumed_session_approval_mode_over_global_mode(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    local_app_data = tmp_path / "localappdata"
    for directory in (workspace, home, config_home, local_app_data):
        directory.mkdir()
    (workspace / "settings.txt").write_text("SESSION_SETTINGS_READY\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    monkeypatch.setenv("VOIDCODE_APPROVAL_MODE", "yolo")
    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    runtime = VoidCodeRuntime(
        workspace=workspace,
        config=RuntimeConfig(execution_engine="deterministic", approval_mode="ask"),
    )
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    runtime_entered = False
    app: TuiApp | None = None
    try:
        runtime.__enter__()
        runtime_entered = True
        response = runtime.run(RuntimeRequest(prompt="read settings.txt", session_id="session-settings-resume"))
        session_id = response.session.session.id

        app = TuiApp(workspace, runtime=runtime, keymap={"f1": "app.help"})
        assert app._approval_mode == "yolo"
        app._term = terminal
        app._setup(terminal)
        app._open_resumed_session(session_id)
        deadline = time.monotonic() + 30
        while app._streaming and time.monotonic() < deadline:
            app._drain_queue()
            time.sleep(0.01)
        app._drain_queue()
        assert not app._streaming
        assert app._approval_mode == "ask"

        app._handle_key(Key("f1"))
        assert isinstance(app._overlay, HelpOverlay)
        rows = "\n".join(app._overlay.render(80))
        assert "ask" in rows
        assert terminal.background_source in rows
    finally:
        if app is not None:
            app._shutdown(terminal)
        elif runtime_entered:
            runtime.__exit__(None, None, None)
        else:
            terminal.close()
        output.close()
