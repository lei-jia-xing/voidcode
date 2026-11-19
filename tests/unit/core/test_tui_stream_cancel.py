"""TUI stream ownership over a real RPC connection: quit, switch, and late attach."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from voidcode.bootstrap import bundled_sources, open_core
from voidcode.core.storage.store import EventDraft
from voidcode.sdk.client import StreamCancellation
from voidcode.transport.http import CoreTransportApp
from voidcode.transport.rpc import RpcConnection
from voidcode.tui.client import CoreRpcClient
from voidcode.tui.terminal_app import CoreTuiApp


class _Terminal:
    """The app's terminal surface without a real TTY: no keys, recorded committed rows."""

    width = 100
    eof = False
    color_system = None

    def query_default_background_rgb(self) -> None:
        return None

    def __init__(self) -> None:
        self.committed: list[str] = []

    def read_bytes(self, *, timeout: float = 0.0) -> bytes:
        return b""

    def drain_kitty_replies(self) -> tuple[()]:
        return ()

    def resize_pending(self) -> bool:
        return False

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        pass

    def close(self) -> None:
        pass


@pytest.fixture
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[str, object, dict[str, threading.Event], dict[str, threading.Event]]]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plugins = {name: {"enabled": False} for name in bundled_sources()}
    (workspace / "voidcode.json").write_text(json.dumps({"schema_version": 1, "plugins": plugins}), encoding="utf-8")
    core = open_core(workspace, state_dir=tmp_path / "state")
    core.create_session("s1")
    core.create_session("s2")
    heads = {name: threading.Event() for name in ("s1", "s2")}
    closed = {name: threading.Event() for name in ("s1", "s2")}
    original = RpcConnection._events

    async def watch(self, params, session, ready):
        try:
            heads[params.session_id].set()
            await original(self, params, session, ready)
        finally:
            closed[params.session_id].set()

    monkeypatch.setattr(RpcConnection, "_events", watch)
    app = CoreTransportApp(core_factory=lambda: core)
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
        yield f"http://127.0.0.1:{port}", core, heads, closed
    finally:
        server.should_exit = True
        worker.join(timeout=10)
        assert not worker.is_alive()


def test_quit_cancels_an_idle_follow_stream_before_joining(remote) -> None:
    base, _core, heads, closed = remote
    client = CoreRpcClient(base)
    app = CoreTuiApp(client, session_id="s1", terminal=_Terminal())  # type: ignore[arg-type]
    results: list[int] = []
    worker = threading.Thread(target=lambda: results.append(app.run()), daemon=True)
    worker.start()
    try:
        assert heads["s1"].wait(5)
        app.running = False
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert results == [0]
        assert app._stream_thread is not None and not app._stream_thread.is_alive()
        assert closed["s1"].wait(5), "server subscription survived quit"
        cancelled = app._stream_cancel
        assert cancelled is not None
        cancelled.close()

        async def consume():
            async for _frame in client.events_stream("s1", cancel=cancelled):
                raise AssertionError("a pre-cancelled stream must not attach")

        asyncio.run(consume())
    finally:
        app.running = False
        worker.join(timeout=5)
        client.close()


def test_session_switch_survives_a_late_old_stream_attach(remote) -> None:
    base, core, heads, closed = remote
    requested = threading.Event()
    release = threading.Event()

    class DelayedClient(CoreRpcClient):
        async def events_stream(self, session_id: str, after_sequence: int = 0, *, follow: bool = True, cancel: StreamCancellation | None = None):
            if session_id == "s1":
                requested.set()
                await asyncio.to_thread(release.wait, 10)
            async for frame in super().events_stream(session_id, after_sequence, follow=follow, cancel=cancel):
                yield frame

    client = DelayedClient(base)
    terminal = _Terminal()
    app = CoreTuiApp(client, session_id="s1", terminal=terminal)  # type: ignore[arg-type]
    results: list[int] = []
    worker = threading.Thread(target=lambda: results.append(app.run()), daemon=True)
    worker.start()
    try:
        assert requested.wait(5)
        old_worker = app._stream_thread
        assert old_worker is not None
        app._switch_session("s2")
        assert heads["s2"].wait(5)
        release.set()
        old_worker.join(timeout=5)
        assert not old_worker.is_alive()
        session = core.session("s2")
        core.store.commit_staged(
            "s2",
            expected_sequence=session.last_event_sequence,
            expected_run_id=session.run_id,
            run_id=session.run_id or "notice",
            revision_id=core.revision_id,
            events=(EventDraft("core.observer_error", "kernel", "system", {"error": "after-switch"}),),
            status=session.status,
            checkpoint_kind=None,
            checkpoint_payload=None,
        )
        core.notify_commit()  # direct harness write: notify like the engine paths do
        deadline = time.monotonic() + 5
        while not any("after-switch" in row for row in terminal.committed) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert any("after-switch" in row for row in terminal.committed)
        assert not closed["s2"].is_set()
        app.running = False
        worker.join(timeout=5)
        assert not worker.is_alive() and results == [0]
        assert closed["s2"].wait(5)
    finally:
        release.set()
        app.running = False
        worker.join(timeout=5)
        client.close()
