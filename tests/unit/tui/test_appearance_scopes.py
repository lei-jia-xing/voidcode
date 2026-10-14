import json
import os
from pathlib import Path

import pytest

from voidcode.runtime.config.loading import runtime_config_path, user_runtime_config_path
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import AppearanceOverlay
from voidcode.tui.term import Terminal


@pytest.mark.parametrize("scope", ("user", "workspace"))
def test_f2_saves_appearance_to_selected_scope(scope: str, tmp_path: Path, monkeypatch) -> None:
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

    monkeypatch.delenv("VOIDCODE_APPROVAL_MODE", raising=False)
    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    app = TuiApp(workspace, keymap={"f2": "app.tui.appearance"})
    output = (tmp_path / "terminal.log").open("wb", buffering=0)
    terminal = Terminal(output_fd=output.fileno(), is_tty=False, size=(80, 24), color_system=None)
    target = user_runtime_config_path() if scope == "user" else runtime_config_path(workspace.resolve())
    other = runtime_config_path(workspace.resolve()) if scope == "user" else user_runtime_config_path()
    try:
        app._term = terminal
        app._setup(terminal)
        original = app._tui_config
        app._handle_key(Key("f2"))
        assert isinstance(app._overlay, AppearanceOverlay)
        app._handle_key(Key("right"))
        assert app._tui_config != original
        app._handle_key(Key("escape"))
        assert app._tui_config == original
        assert not target.exists() and not other.exists()

        app._handle_key(Key("f2"))
        app._handle_key(Key("right"))
        app._handle_key(Key("enter"))
        assert app._overlay is None
        assert app._tui_config != original
        assert not target.exists() and not other.exists()

        app._handle_key(Key("f2"))
        assert isinstance(app._overlay, AppearanceOverlay)
        app._handle_key(Key("s"))
        if scope == "workspace":
            app._handle_key(Key("right"))
        app._handle_key(Key("enter"))

        assert target.is_file()
        assert not other.exists()
        assert app._overlay is None
        saved = json.loads(target.read_text(encoding="utf-8"))
        assert saved["tui"]["theme"]["mode"] == app._tui_config.theme.mode
        assert saved["tui"]["composer"]["style"] == app._tui_config.composer.style
    finally:
        terminal.close()
        output.close()
