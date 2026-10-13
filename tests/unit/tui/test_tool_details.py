import json
import os
import time
from pathlib import Path

from voidcode.core.tool_context import ArtifactMissing
from voidcode.runtime.config.loading import load_runtime_config
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.tui.app import TuiApp
from voidcode.tui.keys import Key
from voidcode.tui.overlay import SessionPickerOverlay, ToolArtifactOverlay
from voidcode.tui.term import Terminal


def test_f3_selects_and_pages_real_tool_artifact_without_changing_ctrl_o(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    workspace = root / "workspace"
    home = root / "home"
    config_home = root / "config"
    local_app_data = root / "localappdata"
    for directory in (workspace, home, config_home, local_app_data):
        directory.mkdir()
    (workspace / ".voidcode.json").write_text(json.dumps({"tui": {"composer": {"style": "band", "gutter": "prompt", "padding": "normal"}}}))
    (workspace / "earlier.txt").write_text("".join(f"EARLIER_ROW_{index:04d}\n" for index in range(1200)))
    (workspace / "later.txt").write_text("".join(f"LATER_ROW_{index:04d}\n" for index in range(1200)))
    (workspace / "other.txt").write_text("".join(f"OTHER_ROW_{index:04d}\n" for index in range(1200)))
    binary_data = b"\x00\xff\x10\x7f"
    (workspace / "binary.bin").write_bytes(binary_data)
    monkeypatch.setenv("APPDATA", str(config_home))
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_STATE_HOME", str(root / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(root / "data"))
    monkeypatch.setenv("VOIDCODE_EXECUTION_ENGINE", "deterministic")
    for key in tuple(os.environ):
        if key.endswith("_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    runtime = VoidCodeRuntime(workspace=workspace, config=load_runtime_config(workspace))
    app = TuiApp(workspace, runtime=runtime, keymap={"f3": "app.tools.detail"})
    output_path = root / "terminal.log"
    output = output_path.open("wb", buffering=0)
    terminal = Terminal(
        output_fd=output.fileno(),
        is_tty=False,
        size=(80, 24),
        color_system=None,
    )
    runtime_entered = False
    try:
        runtime.__enter__()
        runtime_entered = True
        app._term = terminal
        app._setup(terminal)
        app._start_prompt("read earlier.txt\nread later.txt\nread binary.bin")
        deadline = time.monotonic() + 30
        while app._streaming and time.monotonic() < deadline:
            app._drain_queue()
            time.sleep(0.01)
        app._drain_queue()
        assert not app._streaming
        assert app._session_id is not None
        app._render()
        output.flush()
        terminal_output_before = output_path.read_bytes()

        assert app._view is not None and app._region is not None
        settled_before = tuple(app._view.transcript().settled_rows())
        committed_before = app._region.committed_rows()
        history_before = tuple(runtime.replay_session(session_id=app._session_id).events)

        app._handle_key(Key("f3"))
        assert isinstance(app._overlay, SessionPickerOverlay)
        app._handle_key(Key("enter"))
        assert isinstance(app._overlay, ToolArtifactOverlay)
        first_page = "\n".join(app._overlay.render(80))
        assert "EARLIER_ROW_0000" in first_page
        assert "earlier.txt" in first_page
        assert "LATER_ROW_0000" not in first_page

        app._handle_key(Key("pageDown"))
        assert isinstance(app._overlay, ToolArtifactOverlay)
        second_page = "\n".join(app._overlay.render(80))
        assert "EARLIER_ROW_0020" in second_page

        assert app._artifact_detail is not None
        artifact_id = app._artifact_detail[2]
        assert artifact_id is not None
        unrelated = tuple(runtime.run_stream(RuntimeRequest(prompt="read other.txt", allocate_session_id=True)))
        unrelated_session_id = unrelated[-1].session.session.id
        denied = runtime.read_tool_output_artifact(
            caller_session_id=unrelated_session_id,
            session_id=app._session_id,
            artifact_id=artifact_id,
        )
        assert isinstance(denied, ArtifactMissing)

        app._handle_key(Key("escape"))
        app._render()
        output.flush()
        assert app._region.committed_rows() == committed_before
        assert tuple(app._view.transcript().settled_rows()) == settled_before
        assert output_path.read_bytes().startswith(terminal_output_before)
        app._handle_key(Key("ctrl+o"))
        app._render()
        output.flush()
        assert app._region.committed_rows() == committed_before
        assert tuple(runtime.replay_session(session_id=app._session_id).events) == history_before
        assert output_path.read_bytes().startswith(terminal_output_before)
        app._handle_key(Key("escape"))
        app._handle_key(Key("f3"))
        assert isinstance(app._overlay, SessionPickerOverlay)
        app._handle_key(Key("down"))
        app._handle_key(Key("down"))
        app._handle_key(Key("enter"))
        assert isinstance(app._overlay, ToolArtifactOverlay)
        binary_detail = "\n".join(app._overlay.render(80))
        assert "application/octet-stream" in binary_detail
        assert f"{len(binary_data)} bytes" in binary_detail
    finally:
        if runtime_entered:
            app._shutdown(terminal)
        else:
            terminal.close()
        output.close()
