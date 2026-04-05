"""Integration tests for the deterministic read-only slice."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Protocol, cast


class EventLike(Protocol):
    event_type: str


class StreamChunkLike(Protocol):
    kind: str
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

    def run_stream(self, request: RuntimeRequestLike) -> tuple[StreamChunkLike, ...]: ...

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

    event_types = [chunk.event.event_type for chunk in stream if chunk.event is not None]
    output_chunks = [chunk.output for chunk in stream if chunk.kind == "output"]

    assert event_types == [
        "runtime.request_received",
        "graph.tool_request_created",
        "runtime.tool_lookup_succeeded",
        "runtime.permission_resolved",
        "runtime.tool_completed",
        "graph.response_ready",
    ]
    assert output_chunks == ["stream proof\n"]


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
