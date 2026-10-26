"""Progress reaches the existing session SSE endpoint before its tool returns."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import aclosing
from pathlib import Path

import uvicorn

from tests.fixtures.s5_plugin import PLUGIN_ID, PROBE_TOOL
from tests.unit.core.test_nested_tools import _call, _configure, _server, _turn
from voidcode.sdk.client import EventFrame, HeadFrame, RunResult, RunSpec, StreamEvent
from voidcode.transport.http import create_core_app
from voidcode.tui.client import CoreHttpClient


def test_live_tool_progress_uses_existing_session_sse_stream(tmp_path: Path) -> None:
    with _server(
        [
            _turn(_call("progress", PROBE_TOOL, action="progress")),
            _turn(output="complete"),
        ]
    ) as provider:
        workspace = _configure(tmp_path, provider)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        app = create_core_app(workspace=workspace, state_dir=tmp_path / "state", bearer_token="progress-token")
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        http_worker = threading.Thread(target=server.run, daemon=True)
        http_worker.start()
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        assert server.started
        client = CoreHttpClient(f"http://127.0.0.1:{port}", token="progress-token")
        client.create_session("progress-session")
        reports: list[StreamEvent] = []
        results: list[RunResult] = []
        failures: list[BaseException] = []
        observed = threading.Event()

        def observe() -> None:
            async def _consume() -> None:
                async with aclosing(client.events_stream("progress-session", follow=True)) as frames:
                    async for frame in frames:
                        if isinstance(frame, EventFrame):
                            events = [frame.event]
                        elif isinstance(frame, HeadFrame):
                            events = list(frame.events)
                        else:
                            events = []
                        for event in events:
                            if event.type == "core.tool_progress":
                                reports.append(event)
                                observed.set()
                                return

            try:
                asyncio.run(_consume())
            except BaseException as exc:
                failures.append(exc)
                observed.set()

        def run() -> None:
            try:
                results.append(client.run("progress-session", RunSpec("Report progress while waiting.")))
            except BaseException as exc:
                failures.append(exc)

        stream_worker = threading.Thread(target=observe, daemon=True)
        run_worker = threading.Thread(target=run, daemon=True)
        try:
            stream_worker.start()
            run_worker.start()
            assert observed.wait(timeout=10), "no progress event reached the live SSE stream"
            assert not failures
            assert run_worker.is_alive(), "the progress report only arrived after tool completion"
            assert not results
            assert len(reports) == 1
            event = reports[0]
            assert event.session_id == "progress-session"
            assert event.source == f"plugin.{PLUGIN_ID}"
            assert event.payload["owner"] == PLUGIN_ID
            assert dict(event.payload["progress"]) == {"stream": "stdout", "text": "ready"}  # type: ignore[arg-type]
            assert event.payload["invocation_id"]
            assert event.run_id == event.payload["run_id"]
            (workspace / "release").touch()
            run_worker.join(timeout=10)
            stream_worker.join(timeout=5)
            assert not run_worker.is_alive() and not stream_worker.is_alive()
            assert not failures
            assert results[0].status == "completed"
            assert results[0].output == "complete"
        finally:
            (workspace / "release").touch()
            run_worker.join(timeout=10)
            stream_worker.join(timeout=5)
            server.should_exit = True
            http_worker.join(timeout=10)
