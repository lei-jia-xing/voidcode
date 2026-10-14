import os
from pathlib import Path

from voidcode.tui.app import TuiApp, parse_keymap
from voidcode.tui.keys import Key
from voidcode.tui.overlay import AppearanceOverlay, HelpOverlay
from voidcode.tui.term import Terminal


def test_function_key_actions_are_configurable_but_not_default() -> None:
    assert parse_keymap(None) == {"app.tools.expand": Key("ctrl+o")}


def test_f2_opens_appearance_picker(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    local_app_data = tmp_path / "localappdata"
    for directory in (workspace, home, config_home, local_app_data):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    app = TuiApp(workspace, keymap={"f2": "app.tui.appearance"})
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    try:
        app._term = terminal
        app._setup(terminal)
        app._handle_key(Key("f2"))
        assert isinstance(app._overlay, AppearanceOverlay)
    finally:
        terminal.close()
        output.close()


def test_f1_reports_approval_mode_and_auto_theme_source(tmp_path: Path, monkeypatch) -> None:
    workspace = tmp_path / "workspace"
    home = tmp_path / "home"
    config_home = tmp_path / "config"
    local_app_data = tmp_path / "localappdata"
    for directory in (workspace, home, config_home, local_app_data):
        directory.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))

    monkeypatch.setenv("VOIDCODE_APPROVAL_MODE", "ask")
    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    app = TuiApp(workspace, keymap={"f1": "app.help"})
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    try:
        app._term = terminal
        app._setup(terminal)
        app._handle_key(Key("f1"))
        assert isinstance(app._overlay, HelpOverlay)
        rows = "\n".join(app._overlay.render(80))
        assert "ask" in rows
        assert terminal.background_source in rows
    finally:
        terminal.close()
        output.close()
