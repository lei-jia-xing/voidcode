"""Integration tests for the deterministic read-only slice."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Protocol, cast
from unittest.mock import patch


class EventLike(Protocol):
    event_type: str


class StreamChunkLike(Protocol):
    kind: str
    session: SessionLike
    event: EventLike | None
    output: str | None


class SessionLike(Protocol):
    session: object
    status: str


class SessionRefLike(Protocol):
    id: str


class StoredSessionSummaryLike(Protocol):
    session: SessionRefLike


class RuntimeResponseLike(Protocol):
    events: tuple[EventLike, ...]
    output: str | None
    session: SessionLike


class RuntimeRequestLike(Protocol):
    prompt: str


class RuntimeRequestFactory(Protocol):
    def __call__(self, *, prompt: str, session_id: str | None = None) -> RuntimeRequestLike: ...


class RuntimeRunner(Protocol):
    def run(self, request: RuntimeRequestLike) -> RuntimeResponseLike: ...

    def run_stream(self, request: RuntimeRequestLike) -> Iterator[StreamChunkLike]: ...

    def list_sessions(self) -> tuple[StoredSessionSummaryLike, ...]: ...

    def resume(self, session_id: str) -> RuntimeResponseLike: ...


class RuntimeFactory(Protocol):
    def __call__(self, *, workspace: Path) -> RuntimeRunner: ...


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def _load_runtime_types() -> tuple[RuntimeRequestFactory, RuntimeFactory]:
    contracts_module = importlib.import_module("voidcode.runtime.contracts")
    service_module = importlib.import_module("voidcode.runtime.service")
    runtime_request = cast(RuntimeRequestFactory, contracts_module.RuntimeRequest)
    runtime_class = cast(RuntimeFactory, service_module.VoidCodeRuntime)
    return runtime_request, runtime_class


def test_runtime_executes_read_only_slice_and_emits_events(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("alpha\nbeta\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    runtime = runtime_class(workspace=tmp_path)
    result = runtime.run(runtime_request(prompt="read sample.txt"))

    assert [event.event_type for event in result.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert result.session.status == "completed"
    assert result.output == "alpha\nbeta\n"


def test_cli_run_command_prints_events_and_file_contents(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("slice proof\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "run",
            "read sample.txt",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert result.returncode == 0
    assert "EVENT runtime.request_received" in result.stdout
    assert "EVENT runtime.tool_completed" in result.stdout
    assert "RESULT" in result.stdout
    assert "slice proof" in result.stdout


def test_runtime_persists_and_resumes_session_across_instances(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("persisted slice\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    first_runtime = runtime_class(workspace=tmp_path)
    first_result = first_runtime.run(
        runtime_request(prompt="read sample.txt", session_id="demo-session")
    )

    second_runtime = runtime_class(workspace=tmp_path)
    sessions = second_runtime.list_sessions()
    resumed = second_runtime.resume("demo-session")

    assert [summary.session.id for summary in sessions] == ["demo-session"]
    assert first_result.output == resumed.output
    assert [event.event_type for event in resumed.events] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]


def test_runtime_stream_exposes_ordered_events_and_final_output(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("stream proof\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()

    runtime = runtime_class(workspace=tmp_path)
    stream = runtime.run_stream(runtime_request(prompt="read sample.txt"))

    chunks = list(stream)
    event_chunks = [chunk for chunk in chunks if chunk.event is not None]
    output_chunks = [chunk for chunk in chunks if chunk.kind == "output"]
    pre_finalization_chunks = chunks[:5]
    final_chunks = chunks[5:]

    assert [chunk.event.event_type for chunk in event_chunks if chunk.event is not None] == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert [chunk.session.status for chunk in pre_finalization_chunks] == [
        "running",
        "running",
        "running",
        "running",
        "running",
    ]
    assert [chunk.session.status for chunk in final_chunks] == ["completed", "completed"]
    assert [chunk.output for chunk in output_chunks] == ["stream proof\n"]
    assert len(output_chunks) == 1


def test_runtime_stream_yields_before_tool_completion(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("delayed stream\n", encoding="utf-8")
    runtime_request, runtime_class = _load_runtime_types()
    from voidcode.tools.contracts import ToolCall, ToolResult
    from voidcode.tools.read_file import ReadFileTool

    tool_started = threading.Event()
    allow_tool_completion = threading.Event()
    fifth_chunk_ready = threading.Event()
    fifth_chunk: list[StreamChunkLike] = []

    original_invoke = ReadFileTool.invoke

    def _blocking_invoke(self: ReadFileTool, call: ToolCall, *, workspace: Path) -> ToolResult:
        tool_started.set()
        _ = allow_tool_completion.wait(timeout=2)
        return original_invoke(self, call, workspace=workspace)

    with patch.object(ReadFileTool, "invoke", autospec=True, side_effect=_blocking_invoke):
        runtime = runtime_class(workspace=tmp_path)
        stream = runtime.run_stream(runtime_request(prompt="read sample.txt"))

        first_four_chunks = [next(stream) for _ in range(4)]

        assert [
            chunk.event.event_type for chunk in first_four_chunks if chunk.event is not None
        ] == [
            "runtime.request_received",
            "graph.tool_request_created",
            "runtime.tool_lookup_succeeded",
            "runtime.permission_resolved",
        ]
        assert all(chunk.session.status == "running" for chunk in first_four_chunks)

        def _consume_fifth_chunk() -> None:
            fifth_chunk.append(next(stream))
            fifth_chunk_ready.set()

        worker = threading.Thread(target=_consume_fifth_chunk)
        worker.start()

        assert tool_started.wait(timeout=0.2) is True
        time.sleep(0.05)
        assert fifth_chunk_ready.is_set() is False

        started = time.monotonic()
        allow_tool_completion.set()
        worker.join(timeout=1)
        remaining_chunks = list(stream)
        elapsed = time.monotonic() - started

        assert worker.is_alive() is False
        assert elapsed < 1
        assert [chunk.event.event_type for chunk in fifth_chunk if chunk.event is not None] == [
            "runtime.tool_completed"
        ]
        assert all(chunk.session.status == "running" for chunk in fifth_chunk)
        assert [
            chunk.event.event_type for chunk in remaining_chunks if chunk.event is not None
        ] == ["graph.response_ready"]
        assert [chunk.output for chunk in remaining_chunks if chunk.kind == "output"] == [
            "delayed stream\n"
        ]
        assert all(chunk.session.status == "completed" for chunk in remaining_chunks)


def test_cli_lists_and_resumes_persisted_session(tmp_path: Path) -> None:
    sample_file = tmp_path / "sample.txt"
    _ = sample_file.write_text("resume proof\n", encoding="utf-8")

    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

    first_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "run",
            "read sample.txt",
            "--workspace",
            str(tmp_path),
            "--session-id",
            "demo-session",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    list_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "list",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    resume_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "resume",
            "demo-session",
            "--workspace",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )

    assert first_result.returncode == 0
    assert list_result.returncode == 0
    assert resume_result.returncode == 0
    assert "SESSION id=demo-session status=completed" in list_result.stdout
    assert "RESULT" in resume_result.stdout
    assert "resume proof" in resume_result.stdout
