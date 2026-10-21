"""A TUI quit cancels its owned idle follow stream, and the server observes the disconnect.

Both cases use real HTTP responses: the server sends one SSE head frame and then stays silent (no
events, no heartbeats), which is the real idle-follow shape where the client's read blocks with no
timeout. The assertions are observable: app quit/switch ends the worker and the HTTP handler's
blocked read sees the connection close — no logger or traceback is inspected.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from voidcode.core.tui.client import CoreHttpClient
from voidcode.core.tui.terminal_app import CoreTuiApp


class _Terminal:
    """The app's terminal surface without a real TTY: no keys, recorded committed rows."""

    width = 100

    def __init__(self) -> None:
        self.committed: list[str] = []

    def read_bytes(self, *, timeout: float = 0.0) -> bytes:
        return b""

    def commit_rows(self, rows: list[str]) -> None:
        self.committed.extend(rows)

    def paint_frame(self, frame: Any) -> None:
        pass

    def close(self) -> None:
        pass


def test_quit_cancels_an_idle_follow_stream_before_joining() -> None:
    head_sent = threading.Event()
    disconnected = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path.startswith("/api/sessions/s1/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(b'data: {"kind": "head", "session_id": "s1"}\n\n')
                self.wfile.flush()
                head_sent.set()
                # Idle follow: nothing else is written. The client's only wakeup is a connection
                # shutdown, so this read returning is direct proof the server saw the disconnect.
                self.rfile.read(1)
                disconnected.set()
            elif self.path.startswith("/api/sessions/s1"):
                self._json({"session_id": "s1", "status": "idle"})
            elif self.path.startswith("/api/commands"):
                self._json({"revision_id": "rev-1", "commands": []})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_worker = threading.Thread(target=server.serve_forever, daemon=True)
    server_worker.start()
    client = CoreHttpClient(f"http://127.0.0.1:{server.server_address[1]}")
    app = CoreTuiApp(client, session_id="s1", terminal=_Terminal())  # type: ignore[arg-type]
    results: list[int] = []
    failures: list[BaseException] = []

    def run_app() -> None:
        try:
            results.append(app.run())
        except BaseException as exc:
            failures.append(exc)

    app_worker = threading.Thread(target=run_app, daemon=True)
    try:
        app_worker.start()
        assert head_sent.wait(5.0), "the TUI never opened its follow stream"
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and (app._sse_thread is None or not app._sse_thread.is_alive()):
            time.sleep(0.01)
        assert app._sse_thread is not None and app._sse_thread.is_alive()
        time.sleep(0.2)  # let the worker enter the blocked idle read

        app.running = False
        app_worker.join(timeout=5.0)
        assert not app_worker.is_alive(), "the TUI did not quit after the stream cancel"
        assert not failures, failures
        assert results == [0]
        assert not app._sse_thread.is_alive(), "the SSE worker survived quit"
        assert disconnected.wait(5.0), "the server never observed the client disconnect"
        closed_cancel = app._stream_cancel
        assert closed_cancel is not None
        closed_cancel.close()  # idempotent: quit already closed it

        # Open/close race: a stream attached after close must terminate immediately instead of
        # blocking forever on a fresh idle connection.
        frames = client.events_stream("s1", cancel=closed_cancel)

        def consume() -> None:
            try:
                for _frame in frames:
                    pass
            except OSError:
                pass  # shutdown may surface as a read error; either way the stream ended

        consumer = threading.Thread(target=consume, daemon=True)
        consumer.start()
        consumer.join(timeout=5.0)
        assert not consumer.is_alive(), "a stream opened after cancellation blocked"
    finally:
        app.running = False
        app_worker.join(timeout=5.0)
        if app._stream_cancel is not None:
            app._stream_cancel.close()
        server.shutdown()
        server.server_close()
        server_worker.join(timeout=5.0)


def test_session_switch_survives_a_late_old_stream_attach() -> None:
    """A replaced generation's late attach/detach cannot cancel the live replacement stream.

    The old session's response headers are withheld until after the real app flow switches
    sessions (``_switch_session`` advances the generation and starts a new stream). The new stream
    receives its head and a later notice, the old response is released and its worker joins, and
    the replacement stays live until quit closes it. The notice commit is consumer-visible proof
    that old attach/detach touched only its own generation.
    """
    old_requested = threading.Event()
    release_old = threading.Event()
    old_closed = threading.Event()
    new_head_sent = threading.Event()
    new_event_ready = threading.Event()
    new_event_sent = threading.Event()
    new_closed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _json(self, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _open_sse(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

        def do_GET(self) -> None:
            if self.path.startswith("/api/sessions/s1/events"):
                old_requested.set()
                release_old.wait(10.0)
                try:
                    self._open_sse()
                    self.wfile.write(b'data: {"kind": "head", "session_id": "s1"}\n\n')
                    self.wfile.flush()
                    self.rfile.read(1)
                except OSError:
                    pass
                finally:
                    old_closed.set()
            elif self.path.startswith("/api/sessions/s2/events"):
                try:
                    self._open_sse()
                    self.wfile.write(b'data: {"kind": "head", "session_id": "s2"}\n\n')
                    self.wfile.flush()
                    new_head_sent.set()
                    new_event_ready.wait(10.0)
                    self.wfile.write(b'data: {"kind": "notice", "text": "after-switch"}\n\n')
                    self.wfile.flush()
                    new_event_sent.set()
                    self.rfile.read(1)
                except OSError:
                    pass
                finally:
                    new_closed.set()
            elif self.path.startswith("/api/sessions/s1"):
                self._json({"session_id": "s1", "status": "idle"})
            elif self.path.startswith("/api/commands"):
                self._json({"revision_id": "rev-1", "commands": []})
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server_worker = threading.Thread(target=server.serve_forever, daemon=True)
    server_worker.start()
    terminal = _Terminal()
    app = CoreTuiApp(
        CoreHttpClient(f"http://127.0.0.1:{server.server_address[1]}"),
        session_id="s1",
        terminal=terminal,  # type: ignore[arg-type]
    )
    results: list[int] = []
    failures: list[BaseException] = []

    def run_app() -> None:
        try:
            results.append(app.run())
        except BaseException as exc:
            failures.append(exc)

    app_worker = threading.Thread(target=run_app, daemon=True)
    try:
        app_worker.start()
        assert old_requested.wait(5.0), "the old stream request never reached the server"
        old_worker = app._sse_thread
        assert old_worker is not None

        app._switch_session("s2")
        assert new_head_sent.wait(5.0), "the replacement stream never received its head"
        release_old.set()
        assert old_closed.wait(5.0), "the late old connection was not disconnected"
        old_worker.join(timeout=5.0)
        assert not old_worker.is_alive(), "the old stream worker survived the generation switch"

        new_event_ready.set()
        assert new_event_sent.wait(5.0)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not any("after-switch" in row for row in terminal.committed):
            time.sleep(0.01)
        assert any("after-switch" in row for row in terminal.committed), "the replacement stream stopped receiving frames"
        assert not new_closed.is_set(), "the old worker cancelled the replacement connection"

        app.running = False
        app_worker.join(timeout=5.0)
        assert not app_worker.is_alive(), "the TUI did not quit"
        assert not failures, failures
        assert results == [0]
        assert new_closed.wait(5.0), "quit did not close the replacement stream"
    finally:
        release_old.set()
        new_event_ready.set()
        app.running = False
        app_worker.join(timeout=5.0)
        if app._stream_cancel is not None:
            app._stream_cancel.close()
        server.shutdown()
        server.server_close()
        server_worker.join(timeout=5.0)
