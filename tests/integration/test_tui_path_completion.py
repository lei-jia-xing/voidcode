"""End-to-end ``@path`` completion through local and remote clients."""

from __future__ import annotations

import json
import re
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from voidcode.bootstrap import bundled_sources, open_core
from voidcode.transport.http import create_core_app
from voidcode.tui.client import CoreRpcClient
from voidcode.tui.local_client import CoreLocalClient
from voidcode.tui.terminal_app import CoreTuiApp

TOKEN = "path-test-secret"


class _Terminal:
    width = 120
    height = 30
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        return None


def _plain(rows: list[str]) -> str:
    return "\n".join(re.sub(r"\x1b\[[0-9;]*m", "", row) for row in rows)


def _type(app: CoreTuiApp, text: str) -> None:
    from voidcode.tui.keys import Key

    for char in text:
        app._handle_composer_key(Key(char, text=char))


def _settle(app: CoreTuiApp, predicate: Any, *, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app._drain_events()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"completion did not settle; draft={app.composer.value!r} completion={app.composer.completion!r}")


def _workspace_config(workspace: Path, *, files: bool) -> None:
    plugins = {name: {"enabled": name == "files" and files} for name in bundled_sources()}
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": plugins}), encoding="utf-8")


def test_local_completion_uses_the_workspace_query(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "src").mkdir()
    (workspace / "src" / "only-local-server.py").write_text("x", encoding="utf-8")
    (workspace / "LOCAL-NOTE.md").write_text("x", encoding="utf-8")
    _workspace_config(workspace, files=True)

    with open_core(workspace, state_dir=tmp_path / "state") as core:
        session = core.create_session()
        client = CoreLocalClient(core, workspace=workspace)
        terminal = _Terminal()
        app = CoreTuiApp(client, session_id=session.session_id, terminal=terminal)
        _type(app, "@LOCAL")
        _settle(app, lambda: bool(app.composer.completion))
        assert app.composer.completion == ("LOCAL-NOTE.md",)
        assert app.composer.value == "@LOCAL"

        app.composer.set_text("")
        _type(app, "@src/only")
        _settle(app, lambda: bool(app.composer.completion))
        assert app.composer.completion == ("src/only-local-server.py",)
        assert app.composer.completion_descriptions["src/only-local-server.py"] == "file"


def test_files_disabled_makes_completion_unavailable_without_local_fallback(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "never-listed.txt").write_text("x", encoding="utf-8")
    _workspace_config(workspace, files=False)

    with open_core(workspace, state_dir=tmp_path / "state") as core:
        session = core.create_session()
        client = CoreLocalClient(core, workspace=workspace)
        terminal = _Terminal()
        app = CoreTuiApp(client, session_id=session.session_id, terminal=terminal)
        _type(app, "@never")
        _settle(app, lambda: "unavailable" in _plain(terminal.committed).lower())
        assert app.composer.completion == ()
        assert app.composer.value == "@never"
        assert "@path completion unavailable" in _plain(terminal.committed)
        assert "never-listed" not in _plain(terminal.committed)


@pytest.fixture
def remote(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    workspace = tmp_path / "remote-workspace"
    workspace.mkdir()
    (workspace / "remote-only.txt").write_text("server side\n", encoding="utf-8")
    (workspace / "server-dir").mkdir()
    _workspace_config(workspace, files=True)
    app = create_core_app(workspace=workspace, state_dir=tmp_path / "remote-state", bearer_token=TOKEN)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    worker = threading.Thread(target=server.run, daemon=True)
    worker.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}", workspace
    finally:
        server.should_exit = True
        worker.join(timeout=15)
        assert not worker.is_alive()


def test_remote_completion_is_server_workspace_relative(remote: tuple[str, Path]) -> None:
    base, _workspace = remote
    with CoreRpcClient(base, token=TOKEN) as client:
        session = client.create_session()
        terminal = _Terminal()
        app = CoreTuiApp(client, session_id=session.session_id, terminal=terminal)
        _type(app, "@remote")
        _settle(app, lambda: bool(app.composer.completion))
        assert app.composer.completion == ("remote-only.txt",)
        assert app.composer.value == "@remote"

        app.composer.set_text("")
        _type(app, "@server-dir/")
        _settle(app, lambda: app.composer.value == "@server-dir/" and app._work.pending == 0 and app.event_queue.empty())
        assert app.composer.completion == ()
        assert app.composer.value == "@server-dir/"

        app.composer.set_text("")
        _type(app, "@")
        _settle(app, lambda: bool(app.composer.completion))
        # The client's own working directory is the repository; none of its
        # entries may leak into a remote workspace listing.
        assert app.composer.completion == ("server-dir/", "remote-only.txt", "voidcode.json")
        assert "pyproject.toml" not in app.composer.completion
