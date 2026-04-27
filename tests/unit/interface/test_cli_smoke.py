"""Smoke tests for the CLI entrypoints."""

from __future__ import annotations

import importlib
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

from .._paths import with_src_pythonpath


@dataclass(frozen=True)
class _StubEvent:
    sequence: int
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
    turn: int = 1
    metadata: dict[str, object] | None = None


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
    *,
    sequence: int = 0,
    request_id: str = "req-1",
    tool: str = "write_file",
    target_summary: str = "sample.txt",
) -> _StubEvent:
    return _StubEvent(
        sequence=sequence,
        event_type="runtime.approval_requested",
        source="runtime",
        payload={
            "request_id": request_id,
            "tool": tool,
            "target_summary": target_summary,
        },
    )


def _runtime_event(
    event_type: str,
    *,
    sequence: int = 0,
    source: str = "runtime",
    **payload: object,
) -> _StubEvent:
    return _StubEvent(
        sequence=sequence, event_type=event_type, source=source, payload=dict(payload)
    )


def _make_chunk(
    *, session_id: str, status: str, event: _StubEvent | None = None, output: str | None = None
) -> _StubChunk:
    return _StubChunk(
        kind="output" if output is not None else "event",
        session=_StubSession(session=_StubSessionRef(id=session_id), status=status),
        event=event,
        output=output,
    )


def _configure_resume_stream(runtime: Any, *streams: Iterable[_StubChunk]) -> None:
    runtime.resume_stream = MagicMock(side_effect=[iter(stream) for stream in streams])


def _run_module_cli(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "voidcode", *args],
        capture_output=True,
        text=True,
        check=False,
        env=with_src_pythonpath(env),
    )


def test_python_module_help_works() -> None:
    result = _run_module_cli("--help")

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


def test_web_command_help_works() -> None:
    result = _run_module_cli("web", "--help")

    assert result.returncode == 0
    assert "launcher" in result.stdout.lower()
    assert "web" in result.stdout.lower()


def test_serve_command_help_works() -> None:
    result = _run_module_cli("serve", "--help")

    assert result.returncode == 0
    assert "transport" in result.stdout.lower()


def test_web_command_forwards_runtime_config_and_server_entry() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/web-workspace")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(
        cli, "load_runtime_config", autospec=True, return_value=config
    ) as config_mock:
        with patch.object(cli, "web", autospec=True) as web_mock:
            result = cli.main(
                [
                    "web",
                    "--workspace",
                    str(workspace),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8012",
                ]
            )

    assert result == 0
    config_mock.assert_called_once_with(workspace, approval_mode=None)
    web_mock.assert_called_once_with(
        workspace=workspace,
        host="127.0.0.1",
        port=8012,
        config=config,
        open_browser=True,
    )


def test_web_command_forwards_no_open_flag() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/web-workspace")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(
        cli, "load_runtime_config", autospec=True, return_value=config
    ) as config_mock:
        with patch.object(cli, "web", autospec=True) as web_mock:
            result = cli.main(
                [
                    "web",
                    "--workspace",
                    str(workspace),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8012",
                    "--no-open",
                ]
            )

    assert result == 0
    config_mock.assert_called_once_with(workspace, approval_mode=None)
    web_mock.assert_called_once_with(
        workspace=workspace,
        host="127.0.0.1",
        port=8012,
        config=config,
        open_browser=False,
    )


def test_serve_command_forwards_runtime_config_and_server_entry() -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/serve-workspace")
    config = SimpleNamespace(approval_mode="allow")

    with patch.object(
        cli, "load_runtime_config", autospec=True, return_value=config
    ) as config_mock:
        with patch.object(cli, "serve", autospec=True) as serve_mock:
            result = cli.main(
                [
                    "serve",
                    "--workspace",
                    str(workspace),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8013",
                ]
            )

    assert result == 0
    config_mock.assert_called_once_with(workspace, approval_mode=None)
    serve_mock.assert_called_once_with(
        workspace=workspace,
        host="127.0.0.1",
        port=8013,
        config=config,
    )


def test_sessions_resume_rejects_partial_approval_flags() -> None:
    result = _run_module_cli(
        "sessions",
        "resume",
        "demo-session",
        "--approval-decision",
        "allow",
    )

    assert result.returncode == 2
    assert "must be provided together" in result.stderr


def test_sessions_resume_surfaces_approval_resolution_errors_cleanly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
        env = with_src_pythonpath(os.environ.copy())

        setup_result = _run_module_cli(
            "run",
            "read sample.txt",
            "--workspace",
            str(workspace),
            "--session-id",
            "demo-session",
            env=env,
        )

        resume_result = _run_module_cli(
            "sessions",
            "resume",
            "demo-session",
            "--workspace",
            str(workspace),
            "--approval-request-id",
            "wrong",
            "--approval-decision",
            "allow",
            env=env,
        )

    assert setup_result.returncode == 0
    assert resume_result.returncode != 0
    assert "error:" in resume_result.stderr
    assert "approval" in resume_result.stderr.lower() or "pending" in resume_result.stderr.lower()
    assert "Traceback" not in resume_result.stderr


def test_sessions_debug_outputs_json_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        _ = (workspace / "sample.txt").write_text("sample\n", encoding="utf-8")
        env = with_src_pythonpath(os.environ.copy())

        setup_result = _run_module_cli(
            "run",
            "read sample.txt",
            "--workspace",
            str(workspace),
            "--session-id",
            "debug-session",
            env=env,
        )
        debug_result = _run_module_cli(
            "sessions",
            "debug",
            "debug-session",
            "--workspace",
            str(workspace),
            env=env,
        )

    payload = json.loads(debug_result.stdout)
    assert setup_result.returncode == 0
    assert debug_result.returncode == 0
    assert payload["prompt"] == "read sample.txt"
    assert payload["persisted_status"] == "completed"
    assert payload["current_status"] == "completed"
    assert payload["active"] is False
    assert payload["terminal"] is True
    assert payload["replayable"] is True
    assert payload["resume_checkpoint_kind"] == "terminal"
    assert payload["pending_approval"] is None
    assert payload["pending_question"] is None
    assert payload["last_relevant_event"]["event_type"] == "graph.response_ready"
    assert payload["suggested_operator_action"] == "replay"
    assert "Traceback" not in debug_result.stderr


def test_sessions_debug_missing_session_returns_clean_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        env = with_src_pythonpath(os.environ.copy())

        result = _run_module_cli(
            "sessions",
            "debug",
            "missing-session",
            "--workspace",
            str(workspace),
            env=env,
        )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "error: unknown session: missing-session" in result.stderr
    assert "Traceback" not in result.stderr


def test_tui_command_forwards_workspace_and_approval_mode() -> None:
    cli = importlib.import_module("voidcode.cli")
    tui = importlib.import_module("voidcode.tui")
    workspace = Path("/tmp/demo-workspace")

    with patch.object(tui, "VoidCodeTUI", autospec=True) as tui_class:
        result = cli.main(
            [
                "tui",
                "--workspace",
                str(workspace),
                "--approval-mode",
                "ask",
            ]
        )

    assert result == 0
    tui_class.assert_called_once_with(workspace=workspace, approval_mode="ask")
    tui_class.return_value.run.assert_called_once_with()


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


def test_run_command_accepts_skills_max_steps_and_provider_stream_flags() -> None:
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

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.run_stream.return_value = iter(chunks)
            result = cli.main(
                [
                    "run",
                    "read README.md",
                    "--workspace",
                    str(workspace),
                    "--skills",
                    "demo",
                    "review",
                    "--max-steps",
                    "7",
                    "--provider-stream",
                ]
            )

    assert result == 0
    runtime_class.return_value.run_stream.assert_called_once()
    request = runtime_class.return_value.run_stream.call_args.args[0]
    assert request.prompt == "read README.md"
    assert request.metadata["skills"] == ["demo", "review"]
    assert request.metadata["max_steps"] == 7
    assert request.metadata["provider_stream"] is True


def test_run_command_prints_request_observability_event(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    workspace = Path("/tmp/demo-workspace")
    config = SimpleNamespace(approval_mode="allow")
    chunks = (
        _make_chunk(
            session_id="demo-session",
            status="completed",
            event=_runtime_event(
                "runtime.request_received",
                prompt="read README.md",
                agent_preset="leader",
            ),
        ),
        _make_chunk(session_id="demo-session", status="completed", output="done\n"),
    )

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.run_stream.return_value = iter(chunks)
            result = cli.main(
                [
                    "run",
                    "read README.md",
                    "--workspace",
                    str(workspace),
                ]
            )

    captured = capsys.readouterr()

    assert result == 0
    assert (
        "EVENT runtime.request_received source=runtime "
        "agent_preset=leader prompt=read README.md" in captured.out
    )


def test_run_command_real_cli_reports_current_request_truth_without_agent_preset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        env = with_src_pythonpath(os.environ.copy())

        result = _run_module_cli(
            "run",
            "read README.md",
            "--workspace",
            str(workspace),
            env=env,
        )

    assert result.returncode != 0
    assert "EVENT runtime.request_received source=runtime prompt=read README.md" in result.stdout
    assert "EVENT runtime.plan_created source=runtime" not in result.stdout
    assert "read_file target does not exist: README.md" in result.stdout
    assert "read_file target does not exist: README.md" in result.stderr
    assert "Traceback" not in result.stderr


def test_run_command_interactively_allows_inline_approval(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
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
    stderr = _StubTtyStderr()

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            _configure_resume_stream(
                runtime,
                (
                    _make_chunk(
                        session_id="demo-session",
                        status="running",
                        event=_runtime_event(
                            "runtime.approval_resolved",
                            sequence=3,
                            request_id="req-1",
                            decision="allow",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="completed",
                        event=_runtime_event(
                            "runtime.tool_completed",
                            sequence=4,
                            source="tool",
                            tool="write_file",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="completed",
                        output="done\n",
                    ),
                ),
            )
            with patch.object(cli.sys, "stdin", _StubTtyInput("yes\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(
                        ["run", "write sample.txt hi", "--workspace", "/tmp/demo-workspace"]
                    )

    captured = capsys.readouterr()

    assert result == 0
    runtime.resume_stream.assert_called_once_with(
        session_id="demo-session",
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

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = _stream()
            _configure_resume_stream(
                runtime,
                (
                    _make_chunk(
                        session_id="demo-session",
                        status="completed",
                        event=_runtime_event(
                            "runtime.approval_resolved",
                            sequence=3,
                            request_id="req-1",
                            decision="allow",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="completed",
                        event=_runtime_event(
                            "runtime.tool_completed",
                            sequence=4,
                            source="tool",
                            tool="write_file",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="completed",
                        output="done\n",
                    ),
                ),
            )
            with patch.object(cli.sys, "stdin", _StubTtyInput("yes\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    with patch.object(cli.sys, "stdout", stdout):
                        result = cli.main(
                            ["run", "write sample.txt hi", "--workspace", "/tmp/demo-workspace"]
                        )

    assert result == 0
    assert stdout.getvalue().endswith("RESULT\ndone\n")
    assert stdout.getvalue().index("EVENT runtime.approval_requested") < stdout.getvalue().index(
        "RESULT\n"
    )
    assert stderr.writes == ["Approve write_file for sample.txt? [y/N]: "]


def test_run_command_interactively_streams_resumed_events_incrementally() -> None:
    cli = importlib.import_module("voidcode.cli")
    config = SimpleNamespace(approval_mode="ask")
    stdout = _StubStdout()
    stderr = _StubTtyStderr()
    request_received = _runtime_event("runtime.request_received", prompt="write sample.txt hi")
    approval_requested = _approval_requested_event()
    approval_resolved = _runtime_event(
        "runtime.approval_resolved",
        sequence=3,
        request_id="req-1",
        decision="allow",
    )
    tool_completed = _runtime_event(
        "runtime.tool_completed",
        sequence=4,
        source="tool",
        tool="write_file",
    )

    def _resumed_stream() -> Any:
        yield _make_chunk(session_id="demo-session", status="running", event=approval_resolved)
        assert (
            stdout.getvalue().count(
                "EVENT runtime.approval_resolved source=runtime decision=allow request_id=req-1\n"
            )
            == 1
        )
        assert "EVENT runtime.tool_completed source=tool tool=write_file\n" not in stdout.getvalue()
        assert "RESULT\n" not in stdout.getvalue()
        yield _make_chunk(session_id="demo-session", status="completed", event=tool_completed)
        assert "EVENT runtime.tool_completed source=tool tool=write_file\n" in stdout.getvalue()
        assert "RESULT\n" not in stdout.getvalue()
        yield _make_chunk(session_id="demo-session", status="completed", output="done\n")

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(
                (
                    _make_chunk(
                        session_id="demo-session", status="running", event=request_received
                    ),
                    _make_chunk(
                        session_id="demo-session", status="waiting", event=approval_requested
                    ),
                )
            )
            _configure_resume_stream(
                runtime,
                _resumed_stream(),
            )
            with patch.object(cli.sys, "stdin", _StubTtyInput("yes\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    with patch.object(cli.sys, "stdout", stdout):
                        result = cli.main(
                            ["run", "write sample.txt hi", "--workspace", "/tmp/demo-workspace"]
                        )

    assert result == 0
    assert stdout.getvalue().count("EVENT runtime.approval_requested") == 1
    assert stdout.getvalue().index("EVENT runtime.approval_resolved") < stdout.getvalue().index(
        "EVENT runtime.tool_completed"
    )
    assert stdout.getvalue().index("EVENT runtime.tool_completed") < stdout.getvalue().index(
        "RESULT\n"
    )
    assert stderr.writes == ["Approve write_file for sample.txt? [y/N]: "]
    runtime.resume_stream.assert_called_once_with(
        session_id="demo-session",
        approval_request_id="req-1",
        approval_decision="allow",
    )


def test_run_command_interactively_denies_on_empty_input(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
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
    stderr = _StubTtyStderr()

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            _configure_resume_stream(
                runtime,
                (
                    _make_chunk(
                        session_id="demo-session",
                        status="running",
                        event=_runtime_event(
                            "runtime.approval_resolved",
                            sequence=3,
                            request_id="req-1",
                            decision="deny",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="failed",
                        event=_runtime_event(
                            "runtime.failed",
                            sequence=4,
                            error="permission denied for tool: write_file",
                        ),
                    ),
                ),
            )
            with patch.object(cli.sys, "stdin", _StubTtyInput("\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(
                        ["run", "write sample.txt hi", "--workspace", "/tmp/demo-workspace"]
                    )

    captured = capsys.readouterr()

    assert result == 0
    runtime.resume_stream.assert_called_once_with(
        session_id="demo-session",
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
    stderr = _StubTtyStderr()

    with patch.object(cli, "load_runtime_config", autospec=True, return_value=config):
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime = runtime_class.return_value
            runtime.run_stream.return_value = iter(first_stream)
            _configure_resume_stream(
                runtime,
                (
                    _make_chunk(
                        session_id="demo-session",
                        status="running",
                        event=_runtime_event(
                            "runtime.approval_resolved",
                            sequence=3,
                            request_id="req-1",
                            decision="allow",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="running",
                        event=_runtime_event(
                            "runtime.tool_completed",
                            sequence=4,
                            source="tool",
                            tool="write_file",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="waiting",
                        event=_approval_requested_event(
                            sequence=5,
                            request_id="req-2",
                            tool="shell_exec",
                            target_summary="build.sh",
                        ),
                    ),
                ),
                (
                    _make_chunk(
                        session_id="demo-session",
                        status="running",
                        event=_runtime_event(
                            "runtime.approval_resolved",
                            sequence=6,
                            request_id="req-2",
                            decision="allow",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="completed",
                        event=_runtime_event(
                            "runtime.tool_completed",
                            sequence=7,
                            source="tool",
                            tool="shell_exec",
                        ),
                    ),
                    _make_chunk(
                        session_id="demo-session",
                        status="completed",
                        output="done\n",
                    ),
                ),
            )
            with patch.object(cli.sys, "stdin", _StubTtyInput("yes\n", "y\n")):
                with patch.object(cli.sys, "stderr", stderr):
                    result = cli.main(
                        ["run", "write sample.txt hi", "--workspace", "/tmp/demo-workspace"]
                    )

    captured = capsys.readouterr()

    assert result == 0
    assert runtime.resume_stream.call_count == 2
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
    assert runtime.resume_stream.call_args_list == [
        (
            (),
            {
                "session_id": "demo-session",
                "approval_request_id": "req-1",
                "approval_decision": "allow",
            },
        ),
        (
            (),
            {
                "session_id": "demo-session",
                "approval_request_id": "req-2",
                "approval_decision": "allow",
            },
        ),
    ]


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
    runtime.resume_stream.assert_not_called()
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
        env = with_src_pythonpath(os.environ.copy())

        result = _run_module_cli(
            "run",
            "write danger.txt config approved",
            "--workspace",
            str(workspace),
            "--session-id",
            "config-run-session",
            env=env,
        )

        written = (workspace / "danger.txt").read_text(encoding="utf-8")

    assert result.returncode == 0
    assert "EVENT runtime.approval_resolved" in result.stdout
    assert "decision=allow" in result.stdout
    assert written == "config approved"


def test_config_show_outputs_workspace_effective_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / ".voidcode.json").write_text(
            json.dumps({"approval_mode": "deny", "model": "repo/model"}),
            encoding="utf-8",
        )
        env = with_src_pythonpath(os.environ.copy())

        result = _run_module_cli(
            "config",
            "show",
            "--workspace",
            str(workspace),
            env=env,
        )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "workspace": str(workspace),
        "session_id": None,
        "approval_mode": "deny",
        "model": "repo/model",
        "execution_engine": "deterministic",
        "max_steps": 4,
        "provider_fallback": None,
        "resolved_provider": {
            "active_target": {
                "raw_model": "repo/model",
                "provider": "repo",
                "model": "model",
            },
            "targets": [
                {
                    "raw_model": "repo/model",
                    "provider": "repo",
                    "model": "model",
                }
            ],
        },
    }
    assert "Traceback" not in result.stderr


def test_config_show_uses_opencode_go_environment_without_leaking_key() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        env = with_src_pythonpath(os.environ.copy())
        env.update(
            {
                "VOIDCODE_MODEL": "opencode-go/glm-5",
                "VOIDCODE_EXECUTION_ENGINE": "provider",
                "OPENCODE_API_KEY": "opencode-go-secret",
            }
        )

        result = _run_module_cli(
            "config",
            "show",
            "--workspace",
            str(workspace),
            env=env,
        )

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["model"] == "opencode-go/glm-5"
    assert payload["execution_engine"] == "provider"
    assert payload["resolved_provider"] == {
        "active_target": {
            "raw_model": "opencode-go/glm-5",
            "provider": "opencode-go",
            "model": "glm-5",
        },
        "targets": [
            {
                "raw_model": "opencode-go/glm-5",
                "provider": "opencode-go",
                "model": "glm-5",
            }
        ],
    }
    assert "opencode-go-secret" not in result.stdout
    assert "Traceback" not in result.stderr


def test_config_show_outputs_resumed_session_effective_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / ".voidcode.json").write_text(
            json.dumps({"approval_mode": "deny", "model": "repo/model"}),
            encoding="utf-8",
        )
        (workspace / "sample.txt").write_text("session config\n", encoding="utf-8")
        env = with_src_pythonpath(os.environ.copy())

        setup = _run_module_cli(
            "run",
            "read sample.txt",
            "--workspace",
            str(workspace),
            "--session-id",
            "config-session",
            "--approval-mode",
            "allow",
            env=env,
        )
        result = _run_module_cli(
            "config",
            "show",
            "--workspace",
            str(workspace),
            "--session",
            "config-session",
            env=env,
        )

    assert setup.returncode == 0
    assert result.returncode == 0
    assert json.loads(result.stdout) == {
        "workspace": str(workspace),
        "session_id": "config-session",
        "approval_mode": "allow",
        "model": "repo/model",
        "execution_engine": "deterministic",
        "max_steps": 4,
        "provider_fallback": None,
        "resolved_provider": {
            "active_target": {
                "raw_model": "repo/model",
                "provider": "repo",
                "model": "model",
            },
            "targets": [
                {
                    "raw_model": "repo/model",
                    "provider": "repo",
                    "model": "model",
                }
            ],
        },
    }
    assert "Traceback" not in result.stderr


def test_config_show_delegates_to_runtime_effective_config(capsys: Any) -> None:
    cli = importlib.import_module("voidcode.cli")
    runtime_config = SimpleNamespace(
        approval_mode="allow",
        model="runtime/model",
        execution_engine="deterministic",
        max_steps=9,
        provider_fallback=None,
        resolved_provider={
            "active_target": {
                "raw_model": "runtime/model",
                "provider": "runtime",
                "model": "model",
            },
            "targets": [
                {
                    "raw_model": "runtime/model",
                    "provider": "runtime",
                    "model": "model",
                }
            ],
        },
    )

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.effective_runtime_config.return_value = runtime_config
            result = cli.main(
                [
                    "config",
                    "show",
                    "--workspace",
                    str(workspace),
                    "--session",
                    "config-session",
                ]
            )

    captured = capsys.readouterr()

    assert result == 0
    runtime_class.assert_called_once_with(workspace=workspace)
    runtime_class.return_value.effective_runtime_config.assert_called_once_with(
        session_id="config-session"
    )
    assert captured.out == (
        json.dumps(
            {
                "workspace": str(workspace),
                "session_id": "config-session",
                "approval_mode": "allow",
                "model": "runtime/model",
                "execution_engine": "deterministic",
                "max_steps": 9,
                "provider_fallback": None,
                "resolved_provider": {
                    "active_target": {
                        "raw_model": "runtime/model",
                        "provider": "runtime",
                        "model": "model",
                    },
                    "targets": [
                        {
                            "raw_model": "runtime/model",
                            "provider": "runtime",
                            "model": "model",
                        }
                    ],
                },
            }
        )
        + "\n"
    )


def test_config_show_invalid_workspace_returns_error() -> None:
    result = _run_module_cli(
        "config",
        "show",
        "--workspace",
        "/definitely/missing/workspace",
    )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "error:" in result.stderr


def test_config_show_missing_session_returns_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        env = with_src_pythonpath(os.environ.copy())

        result = _run_module_cli(
            "config",
            "show",
            "--workspace",
            str(workspace),
            "--session",
            "missing-session",
            env=env,
        )

    assert result.returncode != 0
    assert result.stdout == ""
    assert "error:" in result.stderr


def test_config_show_session_workspace_mismatch_returns_error() -> None:
    with tempfile.TemporaryDirectory() as tmp_a, tempfile.TemporaryDirectory() as tmp_b:
        workspace_a = Path(tmp_a)
        workspace_b = Path(tmp_b)
        (workspace_a / "sample.txt").write_text("session config\n", encoding="utf-8")
        env = with_src_pythonpath(os.environ.copy())

        setup = _run_module_cli(
            "run",
            "read sample.txt",
            "--workspace",
            str(workspace_a),
            "--session-id",
            "config-session",
            env=env,
        )
        result = _run_module_cli(
            "config",
            "show",
            "--workspace",
            str(workspace_b),
            "--session",
            "config-session",
            env=env,
        )

    assert setup.returncode == 0
    assert result.returncode != 0
    assert result.stdout == ""
    assert "error:" in result.stderr


def test_config_schema_outputs_json_schema() -> None:
    result = _run_module_cli("config", "schema")

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["$id"] == "https://voidcode.dev/schemas/runtime-config.schema.json"
    assert payload["properties"]["approval_mode"]["enum"] == ["allow", "deny", "ask"]


def test_config_init_prints_starter_config_without_writing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        result = _run_module_cli(
            "config",
            "init",
            "--workspace",
            str(workspace),
            "--approval-mode",
            "deny",
            "--execution-engine",
            "provider",
            "--max-steps",
            "8",
            "--with-examples",
            "--print",
        )

        assert not (workspace / ".voidcode.json").exists()

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload == {
        "$schema": "https://voidcode.dev/schemas/runtime-config.schema.json",
        "approval_mode": "deny",
        "execution_engine": "provider",
        "max_steps": 8,
        "tools": {"builtin": {"enabled": True}},
        "skills": {"enabled": True},
    }
    assert "api_key" not in result.stdout


def test_config_init_writes_starter_config_and_refuses_overwrite() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        first = _run_module_cli("config", "init", "--workspace", str(workspace))
        second = _run_module_cli("config", "init", "--workspace", str(workspace))

        written_payload = json.loads((workspace / ".voidcode.json").read_text(encoding="utf-8"))

    assert first.returncode == 0
    assert json.loads(first.stdout)["config_path"].endswith(".voidcode.json")
    assert written_payload == {
        "$schema": "https://voidcode.dev/schemas/runtime-config.schema.json",
        "approval_mode": "ask",
    }
    assert second.returncode != 0
    assert second.stdout == ""
    assert "already exists" in second.stderr


def test_config_migrate_dry_run_reports_removed_agent_field() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        config_path = workspace / ".voidcode.json"
        config_path.write_text(
            json.dumps({"agent": {"preset": "leader", "leader_mode": "legacy"}}),
            encoding="utf-8",
        )

        result = _run_module_cli("config", "migrate", "--workspace", str(workspace))
        disk_payload = json.loads(config_path.read_text(encoding="utf-8"))

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["dry_run"] is True
    assert payload["migrations"] == [
        {
            "action": "remove",
            "field_path": "agent.leader_mode",
            "reason": (
                "agent.leader_mode has been removed; use the default leader execution flow instead"
            ),
        }
    ]
    assert payload["updated_config"] == {"agent": {"preset": "leader"}}
    assert disk_payload == {"agent": {"preset": "leader", "leader_mode": "legacy"}}


def test_config_migrate_write_updates_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        config_path = workspace / ".voidcode.json"
        config_path.write_text(
            json.dumps({"agent": {"preset": "leader", "leader_mode": "legacy"}}),
            encoding="utf-8",
        )

        result = _run_module_cli(
            "config",
            "migrate",
            "--workspace",
            str(workspace),
            "--write",
        )
        disk_payload = json.loads(config_path.read_text(encoding="utf-8"))

    payload = json.loads(result.stdout)
    assert result.returncode == 0
    assert payload["dry_run"] is False
    assert payload["updated_config"] == {"agent": {"preset": "leader"}}
    assert disk_payload == {"agent": {"preset": "leader"}}


def test_config_migrate_invalid_json_returns_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        (workspace / ".voidcode.json").write_text("{", encoding="utf-8")

        result = _run_module_cli("config", "migrate", "--workspace", str(workspace))

    assert result.returncode != 0
    assert result.stdout == ""
    assert "must contain valid JSON" in result.stderr


def test_provider_models_command_outputs_refreshed_provider_model_list() -> None:
    cli = importlib.import_module("voidcode.cli")
    models = ("alias", "provider/model")

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.refresh_provider_models.return_value = models
            contracts = importlib.import_module("voidcode.runtime.contracts")
            runtime_class.return_value.provider_models_result.return_value = (
                contracts.ProviderModelsResult(
                    provider="litellm",
                    configured=True,
                    models=models,
                    model_metadata={
                        "provider/model": contracts.ProviderModelMetadata(
                            context_window=128_000,
                            max_input_tokens=111_616,
                            max_output_tokens=16_384,
                            supports_tools=True,
                        )
                    },
                    source="remote",
                    last_refresh_status="ok",
                )
            )
            result = cli.main(
                [
                    "provider",
                    "models",
                    "litellm",
                    "--workspace",
                    str(workspace),
                    "--refresh",
                ]
            )

    assert result == 0
    runtime_class.assert_called_once_with(workspace=workspace)
    runtime_class.return_value.refresh_provider_models.assert_called_once_with("litellm")
    runtime_class.return_value.provider_models_result.assert_called_once_with("litellm")


def test_provider_inspect_command_outputs_provider_capabilities() -> None:
    cli = importlib.import_module("voidcode.cli")
    contracts = importlib.import_module("voidcode.runtime.contracts")

    with tempfile.TemporaryDirectory() as tmp:
        workspace = Path(tmp)
        inspect_result = contracts.ProviderInspectResult(
            summary=contracts.ProviderSummary(
                name="openai", label="OpenAI", configured=True, current=True
            ),
            models=contracts.ProviderModelsResult(
                provider="openai",
                configured=True,
                models=("gpt-4o",),
                model_metadata={
                    "gpt-4o": contracts.ProviderModelMetadata(
                        context_window=128_000,
                        max_input_tokens=111_616,
                        max_output_tokens=16_384,
                        supports_tools=True,
                        supports_vision=True,
                    )
                },
                source="remote",
                last_refresh_status="ok",
                discovery_mode="configured_endpoint",
            ),
            validation=contracts.ProviderValidationResult(
                provider="openai",
                configured=True,
                ok=True,
                status="ok",
                message="Remote provider validation succeeded.",
                source="remote",
                discovery_mode="configured_endpoint",
            ),
            current_model="gpt-4o",
            current_model_metadata=contracts.ProviderModelMetadata(
                context_window=128_000,
                max_input_tokens=111_616,
                max_output_tokens=16_384,
                supports_tools=True,
                supports_vision=True,
            ),
        )
        with patch.object(cli, "VoidCodeRuntime", autospec=True) as runtime_class:
            runtime_class.return_value.inspect_provider.return_value = inspect_result
            with patch.object(cli, "print") as print_fn:
                result = cli.main(
                    [
                        "provider",
                        "inspect",
                        "openai",
                        "--workspace",
                        str(workspace),
                    ]
                )

    assert result == 0
    runtime_class.assert_called_once_with(workspace=workspace)
    runtime_class.return_value.inspect_provider.assert_called_once_with("openai")
    payload = json.loads(print_fn.call_args.args[0])
    assert payload["provider"]["name"] == "openai"
    assert payload["current_model"] == "gpt-4o"
    assert payload["current_model_metadata"]["max_input_tokens"] == 111_616
    assert payload["current_model_metadata"]["supports_tools"] is True
