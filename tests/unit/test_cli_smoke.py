"""Smoke tests for the CLI entrypoints."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch


@dataclass(frozen=True)
class _StubEvent:
    event_type: str
    source: str
    payload: dict[str, object]


@dataclass(frozen=True)
class _StubSessionRef:
    id: str


@dataclass(frozen=True)
class _StubSession:
    session: _StubSessionRef
    status: str


@dataclass(frozen=True)
class _StubChunk:
    kind: str
    session: _StubSession
    event: _StubEvent | None = None
    output: str | None = None


class _StubTtyInput:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)

    def isatty(self) -> bool:
        return True

    def readline(self) -> str:
        if self._responses:
            return self._responses.pop(0)
        return ""


class _StubNonInteractiveInput:
    def isatty(self) -> bool:
        return False

    def readline(self) -> str:
        raise AssertionError("readline should not be called for non-interactive runs")


class _StubTtyStderr:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def isatty(self) -> bool:
        return True

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None


class _StubNonInteractiveStderr(_StubTtyStderr):
    def isatty(self) -> bool:
        return False


class _StubStdout:
    def __init__(self) -> None:
        self.writes: list[str] = []

    def write(self, text: str) -> int:
        self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        return "".join(self.writes)


def _approval_requested_event(
    *, request_id: str = "req-1", tool: str = "write_file", target_summary: str = "sample.txt"
) -> _StubEvent:
    return _StubEvent(
        event_type="runtime.approval_requested",
        source="runtime",
        payload={
            "request_id": request_id,
            "tool": tool,
            "target_summary": target_summary,
        },
    )


def _runtime_event(event_type: str, *, source: str = "runtime", **payload: object) -> _StubEvent:
    return _StubEvent(event_type=event_type, source=source, payload=dict(payload))


def _make_chunk(
    *, session_id: str, status: str, event: _StubEvent | None = None, output: str | None = None
) -> _StubChunk:
    return _StubChunk(
        kind="output" if output is not None else "event",
        session=_StubSession(session=_StubSessionRef(id=session_id), status=status),
        event=event,
        output=output,
    )


def test_python_module_help_works() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "voidcode", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_console_script_help_works() -> None:
    result = subprocess.run(
        ["voidcode", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()


def test_sessions_resume_rejects_partial_approval_flags() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "voidcode",
            "sessions",
            "resume",
            "demo-session",
            "--approval-decision",
            "allow",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "must be provided together" in result.stderr


def test_sessions_resume_surfaces_approval_resolution_errors_cleanly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

        setup_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "voidcode",
                "run",
                "read sample.txt",
                "--workspace",
                str(workspace),
                "--session-id",
                "demo-session",
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
                str(workspace),
                "--approval-request-id",
                "wrong",
                "--approval-decision",
                "allow",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

    assert setup_result.returncode == 0
    assert resume_result.returncode != 0
    assert "error:" in resume_result.stderr
    assert "approval" in resume_result.stderr.lower() or "pending" in resume_result.stderr.lower()
    assert "Traceback" not in resume_result.stderr


def test_serve_command_forwards_host_port_and_workspace() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="deny")

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config) as load_mock:
        with patch.object(cli, "serve", autospec=True) as serve_mock:
            result = cli.main(
                [
                    "serve",
                    "--workspace",
                    str(workspace),
                    "--host",
                    "0.0.0.0",
                    "--port",
                    "9000",
                    "--approval-mode",
                    "deny",
                ]
            )

    assert result == 0
    load_mock.assert_called_once_with(workspace, approval_mode="deny")
    serve_mock.assert_called_once_with(
        workspace=workspace,
        host="0.0.0.0",
        port=9000,
        config=config,
    )


def test_run_command_loads_config_and_forwards_it_to_runtime() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="allow")
    chunks = (
        _make_chunk(
            session_id="demo-session",
            status="completed",
            event=_runtime_event("runtime.request_received", prompt="read README.md"),
        ),
        _make_chunk(session_id="demo-session", status="completed", output="done\n"),
    )

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config) as load_mock:
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.run_stream.return_value = iter(chunks)
            result = cli.main(
                [
                    "run",
                    "read README.md",
                    "--workspace",
                    str(workspace),
                    "--approval-mode",
                    "allow",
                ]
            )

    assert result == 0
    load_mock.assert_called_once_with(workspace, approval_mode="allow")
    runtime_class.assert_called_once_with(workspace=workspace, config=config)
    runtime_class.return_value.run_stream.assert_called_once()
    runtime_class.return_value.resume.assert_not_called()


def test_run_command_interactively_allows_inline_approval(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="ask")
    first_stream = (
        _make_chunk(
            session_id="demo-session",
            status="running",
            event=_runtime_event("runtime.request_received", prompt="write sample.txt hi"),
        ),
        _make_chunk(
            session_id="demo-session",
            status="waiting",
            event=_approval_requested_event(),
        ),
    )
    resumed_response = SimpleNamespace(
        session=_StubSession(session=_StubSessionRef(id="demo-session"), status="completed"),
        events=(
            first_stream[0].event,
            first_stream[1].event,
            _runtime_event(
                "runtime.approval_resolved",
                request_id="req-1",
                decision="allow",
            ),
            _runtime_event("runtime.tool_completed", source="tool", tool="write_file"),
        ),
        output="done\n",
    )
    stderr = _StubTtyStderr()

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            runtime.resume.return_value = resumed_response
            with patch.object(cli.sys, "stdin", _StubTtyInput("yes\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(["run", "write sample.txt hi", "--workspace", str(workspace)])

    captured = capsys.readouterr()

    assert result == 0
    runtime.resume.assert_called_once_with(
        "demo-session",
        approval_request_id="req-1",
        approval_decision="allow",
    )
    assert captured.out.count("EVENT runtime.approval_requested") == 1
    assert (
        "EVENT runtime.approval_resolved source=runtime decision=allow request_id=req-1"
        in captured.out
    )
    assert captured.out.rstrip().endswith("done")
    assert stderr.writes == ["Approve write_file for sample.txt? [y/N]: "]
    assert captured.err == ""


def test_run_command_interactively_streams_initial_events_incrementally() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="ask")
    stdout = _StubStdout()
    stderr = _StubTtyStderr()
    request_received = _runtime_event("runtime.request_received", prompt="write sample.txt hi")
    approval_requested = _approval_requested_event()

    def _stream() -> Any:
        yield _make_chunk(session_id="demo-session", status="running", event=request_received)
        assert (
            stdout.getvalue()
            == "EVENT runtime.request_received source=runtime prompt=write sample.txt hi\n"
        )
        yield _make_chunk(session_id="demo-session", status="waiting", event=approval_requested)
        assert (
            "EVENT runtime.approval_requested source=runtime "
            "request_id=req-1 target_summary=sample.txt tool=write_file\n" in stdout.getvalue()
        )
        assert "RESULT\n" not in stdout.getvalue()

    resumed_response = SimpleNamespace(
        session=_StubSession(session=_StubSessionRef(id="demo-session"), status="completed"),
        events=(
            request_received,
            approval_requested,
            _runtime_event(
                "runtime.approval_resolved",
                request_id="req-1",
                decision="allow",
            ),
            _runtime_event("runtime.tool_completed", source="tool", tool="write_file"),
        ),
        output="done\n",
    )

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = _stream()
            runtime.resume.return_value = resumed_response
            with patch.object(cli.sys, "stdin", _StubTtyInput("yes\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    with patch.object(cli.sys, "stdout", stdout):
                        result = cli.main(
                            ["run", "write sample.txt hi", "--workspace", str(workspace)]
                        )

    assert result == 0
    assert stdout.getvalue().endswith("RESULT\ndone\n")
    assert stdout.getvalue().index("EVENT runtime.approval_requested") < stdout.getvalue().index(
        "RESULT\n"
    )
    assert stderr.writes == ["Approve write_file for sample.txt? [y/N]: "]


def test_run_command_interactively_denies_on_empty_input(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="ask")
    first_stream = (
        _make_chunk(
            session_id="demo-session",
            status="running",
            event=_runtime_event("runtime.request_received", prompt="write sample.txt hi"),
        ),
        _make_chunk(
            session_id="demo-session",
            status="waiting",
            event=_approval_requested_event(),
        ),
    )
    resumed_response = SimpleNamespace(
        session=_StubSession(session=_StubSessionRef(id="demo-session"), status="failed"),
        events=(
            first_stream[0].event,
            first_stream[1].event,
            _runtime_event(
                "runtime.approval_resolved",
                request_id="req-1",
                decision="deny",
            ),
            _runtime_event(
                "runtime.failed",
                error="permission denied for tool: write_file",
            ),
        ),
        output=None,
    )
    stderr = _StubTtyStderr()

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            runtime.resume.return_value = resumed_response
            with patch.object(cli.sys, "stdin", _StubTtyInput("\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(["run", "write sample.txt hi", "--workspace", str(workspace)])

    captured = capsys.readouterr()

    assert result == 0
    runtime.resume.assert_called_once_with(
        "demo-session",
        approval_request_id="req-1",
        approval_decision="deny",
    )
    assert (
        "EVENT runtime.approval_resolved source=runtime decision=deny request_id=req-1"
        in captured.out
    )
    assert (
        "EVENT runtime.failed source=runtime error=permission denied for tool: write_file"
        in captured.out
    )
    assert captured.out.rstrip().endswith("RESULT")
    assert stderr.writes == ["Approve write_file for sample.txt? [y/N]: "]
    assert captured.err == ""


def test_run_command_interactively_handles_repeated_approval_requests(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="ask")
    first_stream = (
        _make_chunk(
            session_id="demo-session",
            status="running",
            event=_runtime_event("runtime.request_received", prompt="write sample.txt hi"),
        ),
        _make_chunk(
            session_id="demo-session",
            status="waiting",
            event=_approval_requested_event(request_id="req-1", target_summary="sample.txt"),
        ),
    )
    first_resume_response = SimpleNamespace(
        session=_StubSession(session=_StubSessionRef(id="demo-session"), status="waiting"),
        events=(
            first_stream[0].event,
            first_stream[1].event,
            _runtime_event(
                "runtime.approval_resolved",
                request_id="req-1",
                decision="allow",
            ),
            _runtime_event("runtime.tool_completed", source="tool", tool="write_file"),
            _approval_requested_event(
                request_id="req-2", tool="shell_exec", target_summary="build.sh"
            ),
        ),
        output=None,
    )
    second_resume_response = SimpleNamespace(
        session=_StubSession(session=_StubSessionRef(id="demo-session"), status="completed"),
        events=(
            *first_resume_response.events,
            _runtime_event(
                "runtime.approval_resolved",
                request_id="req-2",
                decision="allow",
            ),
            _runtime_event("runtime.tool_completed", source="tool", tool="shell_exec"),
        ),
        output="done\n",
    )
    stderr = _StubTtyStderr()

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            runtime.resume.side_effect = [first_resume_response, second_resume_response]
            with patch.object(cli.sys, "stdin", _StubTtyInput("yes\n", "y\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(["run", "write sample.txt hi", "--workspace", str(workspace)])

    captured = capsys.readouterr()

    assert result == 0
    assert runtime.resume.call_count == 2
    assert runtime.resume.call_args_list == [
        (("demo-session",), {"approval_request_id": "req-1", "approval_decision": "allow"}),
        (("demo-session",), {"approval_request_id": "req-2", "approval_decision": "allow"}),
    ]
    assert stderr.writes == [
        "Approve write_file for sample.txt? [y/N]: ",
        "Approve shell_exec for build.sh? [y/N]: ",
    ]
    assert captured.out.count("EVENT runtime.request_received") == 1
    assert captured.out.count("EVENT runtime.approval_requested") == 2
    assert captured.out.count("EVENT runtime.approval_resolved") == 2
    assert captured.out.count("EVENT runtime.tool_completed source=tool tool=write_file") == 1
    assert captured.out.count("EVENT runtime.tool_completed source=tool tool=shell_exec") == 1
    assert captured.out.index("request_id=req-1") < captured.out.index("request_id=req-2")
    assert captured.out.rstrip().endswith("done")
    assert captured.err == ""


def test_run_command_does_not_prompt_or_resume_when_not_interactive(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="ask")
    first_stream = (
        _make_chunk(
            session_id="demo-session",
            status="running",
            event=_runtime_event("runtime.request_received", prompt="write sample.txt hi"),
        ),
        _make_chunk(
            session_id="demo-session",
            status="waiting",
            event=_approval_requested_event(),
        ),
    )
    stderr = _StubNonInteractiveStderr()

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            with patch.object(cli.sys, "stdin", _StubNonInteractiveInput()):
                with patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(["run", "write sample.txt hi", "--workspace", str(workspace)])

    captured = capsys.readouterr()

    assert result == 0
    runtime.resume.assert_not_called()
    assert "EVENT runtime.approval_requested" in captured.out
    assert captured.out.rstrip().endswith("RESULT")
    assert stderr.writes == []
    assert captured.err == ""


def test_run_command_uses_repo_local_config_to_allow_write_request() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / ".voidcode.json").write_text(
            json.dumps({"approval_mode": "allow"}),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "voidcode",
                "run",
                "write danger.txt config approved",
                "--workspace",
                str(workspace),
                "--session-id",
                "config-run-session",
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )

        written = (workspace / "danger.txt").read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "EVENT runtime.approval_resolved" in result.stdout
    assert "decision=allow" in result.stdout
    assert written == "config approved"
