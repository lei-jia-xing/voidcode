"""Behavior tests for the exec plugin's background process tool on the core process service.

The host owns the machine, so these tests drive a scripted service: it records every call the tool
made and returns the handles, log pages, and refusals the host would have produced. What is under
test is the tool's contract — the argv/cwd/log paths it asks for, the identity it persists in
workspace state, the ready-pattern wait, line paged logs, stdin writes, and the honest
"externally managed" answer for an id this host no longer supervises.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import jsonschema
import pytest

from voidcode.capabilities.exec.background_process import (
    MAX_PROCESS_ROWS,
    MAX_SEND_BYTES,
    BackgroundProcessExternalBody,
    BackgroundProcessListBody,
    BackgroundProcessLogsBody,
    BackgroundProcessSendBody,
    BackgroundProcessStartBody,
    BackgroundProcessStopBody,
)
from voidcode.capabilities.exec.process_reader import read_process_resource
from voidcode.capabilities.resource_pages import MAX_RESOURCE_PAGE_BYTES
from voidcode.plugins.exec import create_plugin
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.resources import ResourceReadCaller, ResourceReadRequest
from voidcode.sdk.services import ProcessHandle, ProcessOutput
from voidcode.sdk.tools import TOOL_OUTPUT_BYTES, ConfirmedStop, ToolCall, ToolEffect, ToolFailure, ToolSuccess, UnconfirmedStop

_SHELL_WRAPPER = ["cmd.exe", "/c"] if os.name == "nt" else ["/bin/sh", "-c"]
_LOG_PAGE_BYTES = 64 * 1024
_HOST_PAGE_LIMIT = 256 * 1024
_EXTERNAL = "externally managed"


class _FakeServices:
    """A scripted core service: records every call, returns what the test scripted."""

    def __init__(
        self,
        *,
        state: Mapping[str, object] | None = None,
        version: int = 0,
        handles: Sequence[ProcessHandle] = (),
        logs: Mapping[tuple[str, str] | str, bytes] | None = None,
        spawned_polls: Sequence[bytes] = (),
        spawn_error: Exception | None = None,
        write_error: Exception | None = None,
        cancel_result: bool = True,
        spawn_running: bool = True,
        spawn_exit_code: int | None = None,
    ) -> None:
        self.state = dict(state or {})
        self.version = version
        self.handles = list(handles)
        self.logs = dict(logs or {})
        self.polls = list(spawned_polls)
        self.spawn_error = spawn_error
        self.write_error = write_error
        self.cancel_result = cancel_result
        self.spawn_running = spawn_running
        self.spawn_exit_code = spawn_exit_code
        self.calls: list[tuple[str, dict[str, object]]] = []

    # ---- recorded calls ---------------------------------------------------------------------

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def call(self, name: str) -> dict[str, object]:
        return next(args for call_name, args in self.calls if call_name == name)

    def spawns_of(self, name: str) -> list[dict[str, object]]:
        return [args for call_name, args in self.calls if call_name == name]

    @property
    def spawn_handle(self) -> ProcessHandle:
        return self.handles[-1]

    # ---- the seven service calls ------------------------------------------------------------

    def get_state(self, scope: str = "session") -> tuple[Mapping[str, object] | None, int]:
        self.calls.append(("get_state", {"scope": scope}))
        return (dict(self.state) or None, self.version)

    def replace_state(self, state: Mapping[str, object], expected_version: int, scope: str = "session") -> int:
        self.calls.append(("replace_state", {"state": dict(state), "expected_version": expected_version, "scope": scope}))
        self.state = dict(state)
        self.version = expected_version + 1
        return self.version

    def spawn_process(
        self,
        argv: Sequence[str],
        *,
        cwd: object = None,
        env: object = None,
        stdin: bool = False,
        stdout_path: object = None,
        stderr_path: object = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        self.calls.append(
            (
                "spawn_process",
                {
                    "argv": list(argv),
                    "cwd": cwd,
                    "env": env,
                    "stdin": stdin,
                    "stdout_path": stdout_path,
                    "stderr_path": stderr_path,
                    "process_id": process_id,
                },
            )
        )
        if self.spawn_error is not None:
            raise self.spawn_error
        handle = ProcessHandle(
            process_id=str(process_id),
            pid=100 + len(self.handles),
            start_time=f"start-{process_id}",
            stdout_path=str(stdout_path or f"/host-spool/{process_id}.stdout.log"),
            stderr_path=str(stderr_path or f"/host-spool/{process_id}.stderr.log"),
            running=self.spawn_running,
            exit_code=self.spawn_exit_code,
        )
        self.handles.append(handle)
        return handle

    def write_process(self, process_id: str, data: bytes | str) -> int:
        payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
        self.calls.append(("write_process", {"process_id": process_id, "data": payload}))
        if self.write_error is not None:
            raise self.write_error
        return len(payload)

    def read_process_output(
        self,
        process_id: str,
        *,
        stream: str = "stdout",
        offset: int = 0,
        limit: int = 65536,
    ) -> ProcessOutput:
        assert 0 < limit <= _HOST_PAGE_LIMIT, f"the host refuses a page bound of {limit}"
        self.calls.append(("read_process_output", {"process_id": process_id, "stream": stream, "offset": offset, "limit": limit}))
        if self.polls:
            data = self.polls.pop(0)
        else:
            data = self.logs.get((process_id, stream), self.logs.get(stream, b""))
        page = data[offset : offset + limit]
        handle = next((handle for handle in self.handles if handle.process_id == process_id), None)
        return ProcessOutput(
            process_id=process_id,
            stream=stream,
            offset=offset,
            total_bytes=len(data),
            data=page,
            eof=offset + len(page) >= len(data),
            running=handle.running if handle is not None else False,
        )

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        self.calls.append(("cancel_process", {"process_id": process_id, "reason": reason}))
        if not self.cancel_result:
            return False
        self.handles = [replace(handle, running=False, exit_code=-15) if handle.process_id == process_id else handle for handle in self.handles]
        return True

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        self.calls.append(("list_processes", {}))
        return tuple(self.handles)


def _handle(process_id: str, *, pid: int, running: bool = True, exit_code: int | None = None) -> ProcessHandle:
    log_dir = f"/workspace/.voidcode/background-processes/{process_id}"
    return ProcessHandle(
        process_id=process_id,
        pid=pid,
        start_time=f"start-{pid}",
        stdout_path=f"{log_dir}.stdout.log",
        stderr_path=f"{log_dir}.stderr.log",
        running=running,
        exit_code=exit_code,
    )


def _persisted(
    handle: ProcessHandle,
    *,
    description: str = "",
    command: str = "sleep 60",
    state: str = "running",
    origin_session_id: str | None = "session-test",
) -> dict[str, object]:
    """The record `start` writes: identity, where it logs, and what the caller called it."""
    record: dict[str, object] = {
        "process_id": handle.process_id,
        "pid": handle.pid,
        "start_time": handle.start_time,
        "description": description,
        "command": command,
        "stdout_path": handle.stdout_path,
        "stderr_path": handle.stderr_path,
        "started_at": "2026-01-01T00:00:00+00:00",
        "state": state,
    }
    if origin_session_id is not None:
        record["origin_session_id"] = origin_session_id
    return record


def _state(*records: Mapping[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "processes": {str(record["process_id"]): dict(record) for record in records}}


def _tool():
    return next(tool for tool in create_plugin({}).tools if tool.definition.name == "background_process")


class _AbortSignal:
    """The core's cancel state as the host swaps it into a live invocation."""

    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.reason: str | None = "cancelled by test" if cancelled else None

    def cancel(self) -> None:
        self.cancelled = True
        self.reason = "cancelled by test"


def _context(workspace: Path, abort_signal: object | None = None) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id="session-test",
        run_id="run-test",
        invocation_id="invocation-test",
        revision_id="rev-test",
        actor="model",
        abort_signal=abort_signal,  # type: ignore[arg-type]
    )


def _invoke(arguments: Mapping[str, object], tmp_path: Path, services: _FakeServices, *, abort_signal: object | None = None):
    return _tool().invoke(
        ToolCall("background_process", arguments),
        context=_context(tmp_path, abort_signal),
        services=services,  # type: ignore[arg-type]
    )


def test_the_plugin_registers_both_tools() -> None:
    definitions = {tool.definition.name: tool.definition for tool in create_plugin({}).tools}

    assert set(definitions) == {"shell_exec", "background_process"}
    definition = definitions["background_process"]
    assert definition.effects == frozenset({ToolEffect.EXECUTE})
    assert definitions["shell_exec"].effects == frozenset({ToolEffect.EXECUTE})
    # The core validates the schema at admission and again on every call.
    jsonschema.Draft202012Validator.check_schema(json_wire_object(definition.input_schema))


@pytest.mark.parametrize(
    ("arguments", "accepted"),
    [
        ({"op": "start", "command": "serve"}, True),
        ({"op": "start", "command": "serve", "ready_pattern": "up", "ready_timeout": 30}, True),
        ({"op": "start", "command": "serve", "ready_timeout": 31}, False),
        ({"op": "start"}, False),
        ({"op": "ps"}, True),
        ({"op": "ps", "process_id": "proc-1"}, False),
        ({"op": "logs", "process_id": "proc-1"}, True),
        ({"op": "logs", "process_id": "proc-1", "stream": "stderr", "offset": 10, "limit": 500}, True),
        ({"op": "logs", "process_id": "proc-1", "stream": "sideways"}, False),
        ({"op": "logs", "process_id": "proc-1", "limit": 501}, False),
        ({"op": "send", "process_id": "proc-1", "input": "hi"}, True),
        ({"op": "send", "process_id": "proc-1", "input": "hi", "newline": False}, True),
        ({"op": "send", "process_id": "proc-1"}, False),
        ({"op": "stop", "process_id": "proc-1"}, True),
        ({"op": "restart", "process_id": "proc-1"}, False),
        ({}, False),
    ],
)
def test_the_declared_schema_validates_operations(arguments: Mapping[str, object], accepted: bool) -> None:
    tool = next(tool for tool in create_plugin({}).tools if tool.definition.name == "background_process")
    schema = json_wire_object(tool.definition.input_schema)

    assert (not list(jsonschema.Draft202012Validator(schema).iter_errors(dict(arguments)))) is accepted


def test_start_launches_the_workspace_shell_and_persists_the_identity(tmp_path: Path) -> None:
    services = _FakeServices()

    result = _invoke({"op": "start", "command": "python -m http.server", "description": "dev server"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    spawn = services.call("spawn_process")
    handle = services.spawn_handle
    assert str(spawn["process_id"]).startswith("proc-")
    assert spawn["argv"] == [*_SHELL_WRAPPER, "python -m http.server"]
    assert spawn["cwd"] == tmp_path.resolve()
    assert spawn["stdin"] is True
    assert spawn["env"] is None
    assert spawn["stdout_path"] is None
    assert spawn["stderr_path"] is None
    assert not (tmp_path / ".voidcode").exists()
    body = result.body
    assert isinstance(body, BackgroundProcessStartBody)
    assert body.process_id == handle.process_id
    assert body.resource_uri == f"proc://{handle.process_id}"
    assert body.pid == handle.pid
    assert body.state == "running"
    assert body.description == "dev server"
    assert body.stdout_path == handle.stdout_path
    assert body.stderr_path == handle.stderr_path
    assert body.ready is None
    assert body.ready_pattern is None
    assert body.exit_code is None
    guidance = (
        f"Read proc://{handle.process_id} for status and stdout, or proc://{handle.process_id}/stderr for stderr; "
        "use background_process with op=logs, send, or stop for line paging and process control."
    )
    assert body.guidance == guidance
    started = f"Started background process {handle.process_id} (pid={handle.pid}) for command: python -m http.server."
    assert result.output.text == f"{started} Guidance: {guidance}"
    # The identity is persisted in workspace state, with optimistic concurrency.
    assert services.call("get_state")["scope"] == "workspace"
    replace_call = services.call("replace_state")
    assert replace_call["scope"] == "workspace"
    assert replace_call["expected_version"] == 0
    record = services.state["processes"][handle.process_id]
    assert record == {
        **_persisted(handle, description="dev server", command="python -m http.server"),
        "origin_session_id": "session-test",
        "started_at": record["started_at"],
    }
    assert isinstance(record["started_at"], str) and record["started_at"]
    assert replace_call["state"] == {"schema_version": 1, "processes": {handle.process_id: record}}
    # A process still running after the start is written once: no wait can cost the identity.
    assert len(services.spawns_of("replace_state")) == 1


def test_a_later_start_keeps_the_identities_already_persisted(tmp_path: Path) -> None:
    first = _handle("proc-first", pid=11)
    services = _FakeServices(state=_state(_persisted(first, description="first", origin_session_id=None)), version=4)

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    records = services.state["processes"]
    assert set(records) == {"proc-first", services.spawn_handle.process_id}
    assert records["proc-first"]["description"] == "first"
    assert "origin_session_id" not in records["proc-first"]
    assert records[services.spawn_handle.process_id]["origin_session_id"] == "session-test"
    assert services.call("replace_state")["expected_version"] == 4


def test_start_waits_for_the_ready_pattern(tmp_path: Path) -> None:
    # The first poll is empty: the pattern only appears on the next read of the same byte window.
    services = _FakeServices(spawned_polls=[b"", b"booting\nlistening on 8080\n"])

    result = _invoke({"op": "start", "command": "serve", "ready_pattern": "listening"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessStartBody)
    assert body.ready is True
    assert body.ready_pattern == "listening"
    assert "Readiness pattern" not in result.output.text
    polls = services.spawns_of("read_process_output")
    assert len(polls) >= 2
    assert {poll["limit"] for poll in polls} == {_LOG_PAGE_BYTES}


def test_start_reports_a_ready_timeout_without_failing(tmp_path: Path) -> None:
    services = _FakeServices()

    result = _invoke({"op": "start", "command": "serve", "ready_pattern": "listening", "ready_timeout": 1}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessStartBody)
    assert body.ready is False
    assert body.state == "running"
    assert "Readiness pattern 'listening' was not observed within 1s; process is still running." in result.output.text
    # A timeout is not a failure: the identity is still the caller's handle on the process.
    assert services.spawn_handle.process_id in services.state["processes"]


def test_start_reports_a_process_that_exited_before_readiness(tmp_path: Path) -> None:
    services = _FakeServices(spawn_running=False, spawn_exit_code=3)

    result = _invoke({"op": "start", "command": "serve", "ready_pattern": "listening", "ready_timeout": 5}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessStartBody)
    assert body.state == "exited"
    assert body.running is False
    assert body.exit_code == 3
    assert "Process exited with code 3 before readiness pattern 'listening' appeared." in result.output.text
    record = services.state["processes"][services.spawn_handle.process_id]
    assert record["state"] == "exited"
    assert record["exit_code"] == 3
    # The identity was written before the wait, and the confirmed exit written after it.
    assert [call["expected_version"] for call in services.spawns_of("replace_state")] == [0, 1]


def test_start_of_a_cancelled_call_launches_nothing(tmp_path: Path) -> None:
    services = _FakeServices()

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services, abort_signal=_AbortSignal(cancelled=True))

    assert result.status == "error"
    assert "aborted before execution" in (result.error or "")
    assert result.execution is not None and result.execution.cancellation_signalled is True
    assert result.execution.side_effect_state == "settled"
    # The refusal is pre-flight: no process was started and no identity was written.
    assert services.names() == []
    assert services.state == {}


def test_start_converts_the_core_duplicate_refusal(tmp_path: Path) -> None:
    services = _FakeServices(spawn_error=ValueError("background process proc-abc is already running"))

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert "already running" in result.error
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_execution_error"
    assert result.diagnostics.details["reason"] == "duplicate_spawn"
    assert services.names() == ["get_state", "spawn_process"]
    assert services.state == {}


def test_start_converts_a_failed_process_start(tmp_path: Path) -> None:
    services = _FakeServices(spawn_error=ValueError("failed to start background process: permission denied"))

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "failed_process_start"
    assert "could not start the process" in result.error
    assert services.names() == ["get_state", "spawn_process"]


def test_start_surfaces_a_read_only_lease_permission_error(tmp_path: Path) -> None:
    refusal = PermissionError("this lease may not run background processes: read-only and hook leases do not touch the machine")
    services = _FakeServices(spawn_error=refusal)

    with pytest.raises(PermissionError, match="read-only and hook leases do not touch the machine"):
        _invoke({"op": "start", "command": "serve"}, tmp_path, services)


def test_start_cancelled_right_after_spawn_stops_the_process(tmp_path: Path) -> None:
    signal = _AbortSignal()

    class CancelOnSpawn(_FakeServices):
        def spawn_process(self, *args: object, **kwargs: object) -> ProcessHandle:
            handle = super().spawn_process(*args, **kwargs)  # type: ignore[arg-type]
            signal.cancel()
            return handle

    services = CancelOnSpawn()

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert "cancelled after starting" in result.error
    assert "Started background process" not in result.output.text
    assert result.execution is not None
    assert isinstance(result.execution, ConfirmedStop)
    assert result.execution.cancellation_signalled is True
    assert services.call("cancel_process")["reason"] == "background_process start cancelled"
    # The identity was never persisted, so the cancelled start leaves no success record.
    assert "replace_state" not in services.names()
    assert services.state == {}
    assert result.body is None


def test_start_cancelled_after_persisting_the_identity_marks_it_exited(tmp_path: Path) -> None:
    signal = _AbortSignal()

    class CancelOnStateWrite(_FakeServices):
        def __init__(self) -> None:
            super().__init__()
            self.writes = 0

        def replace_state(self, state: Mapping[str, object], expected_version: int, scope: str = "session") -> int:
            version = super().replace_state(state, expected_version, scope)
            if self.writes == 0:
                signal.cancel()
            self.writes += 1
            return version

    services = CancelOnStateWrite()

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert "the process and its group were stopped" in result.error
    assert result.execution is not None and result.execution.side_effect_state == "settled"
    assert services.names().count("cancel_process") == 1
    process_id = services.spawn_handle.process_id
    assert services.state["processes"][process_id]["state"] == "exited"
    assert services.state["processes"][process_id]["exit_code"] == -15
    assert [call["expected_version"] for call in services.spawns_of("replace_state")] == [0, 1]
    assert isinstance(result.body, BackgroundProcessStartBody)
    assert result.body.state == "exited"
    assert result.body.running is False


def test_start_cancelled_during_the_readiness_wait_cleans_up_instead_of_succeeding(tmp_path: Path) -> None:
    signal = _AbortSignal()

    class CancelOnPoll(_FakeServices):
        def read_process_output(self, process_id: str, *, stream: str = "stdout", offset: int = 0, limit: int = _HOST_PAGE_LIMIT) -> ProcessOutput:
            page = super().read_process_output(process_id, stream=stream, offset=offset, limit=limit)
            signal.cancel()
            return page

    services = CancelOnPoll()

    result = _invoke(
        {"op": "start", "command": "serve", "ready_pattern": "listening", "ready_timeout": 5},
        tmp_path,
        services,
        abort_signal=signal,
    )

    assert isinstance(result, ToolFailure)
    assert "Readiness wait was cancelled; the process was started" not in result.output.text
    assert "the process and its group were stopped" in result.error
    assert services.names().count("cancel_process") == 1
    process_id = services.spawn_handle.process_id
    assert services.state["processes"][process_id]["state"] == "exited"


def test_start_cancelled_with_an_unconfirmed_stop_reports_that_honestly(tmp_path: Path) -> None:
    signal = _AbortSignal()

    class UnconfirmedStopServices(_FakeServices):
        def __init__(self) -> None:
            super().__init__(cancel_result=False)
            self.writes = 0

        def replace_state(self, state: Mapping[str, object], expected_version: int, scope: str = "session") -> int:
            version = super().replace_state(state, expected_version, scope)
            if self.writes == 0:
                signal.cancel()
            self.writes += 1
            return version

    services = UnconfirmedStopServices()

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert "could not confirm it stopped" in result.error
    assert result.execution is not None
    assert isinstance(result.execution, UnconfirmedStop)
    assert result.execution.side_effect_state == "unknown"
    process_id = services.spawn_handle.process_id
    # An unconfirmed stop must not fabricate an exited record.
    assert services.state["processes"][process_id]["state"] == "running"


def test_start_cancelled_with_an_already_exited_leader_is_still_unconfirmed(tmp_path: Path) -> None:
    signal = _AbortSignal()

    class ExitedBeforeCancel(_FakeServices):
        def __init__(self) -> None:
            super().__init__(cancel_result=False)
            self.writes = 0

        def replace_state(self, state: Mapping[str, object], expected_version: int, scope: str = "session") -> int:
            version = super().replace_state(state, expected_version, scope)
            if self.writes == 0:
                signal.cancel()
                self.handles = [replace(handle, running=False, exit_code=0) for handle in self.handles]
            self.writes += 1
            return version

    services = ExitedBeforeCancel()

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert "the process leader exited, but the process group could not be confirmed stopped" in result.error
    assert result.execution is not None
    assert isinstance(result.execution, UnconfirmedStop)
    assert result.execution.side_effect_state == "unknown"
    assert result.execution.cancellation_signalled is True
    process_id = services.spawn_handle.process_id
    # A dead leader does not prove the recorded group is gone; keep the identity running.
    assert services.state["processes"][process_id]["state"] == "running"
    assert "exit_code" not in services.state["processes"][process_id]
    assert [call["expected_version"] for call in services.spawns_of("replace_state")] == [0]
    assert isinstance(result.body, BackgroundProcessStartBody)
    assert result.body.state == "exited"
    assert result.body.running is False
    assert result.body.exit_code == 0


def test_ps_merges_live_handles_with_the_persisted_identities(tmp_path: Path) -> None:
    live = _handle("proc-live", pid=101)
    done = _handle("proc-done", pid=102, running=False, exit_code=0)
    gone = _handle("proc-gone", pid=103)
    services = _FakeServices(
        handles=[live, done],
        state=_state(
            _persisted(live, description="dev server", command="python -m http.server"),
            _persisted(done, description="one shot", command="ls", state="exited"),
            _persisted(gone, description="watcher", command="watch"),
        ),
    )

    result = _invoke({"op": "ps"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessListBody)
    assert [(row.process_id, row.state) for row in body.processes] == [
        ("proc-done", "exited"),
        ("proc-gone", _EXTERNAL),
        ("proc-live", "running"),
    ]
    assert body.count == 3
    assert body.limit == MAX_PROCESS_ROWS
    text = result.output.text
    assert text.startswith("Background process list: 3 process(es).\n")
    assert "- proc-live (proc://proc-live): running; description='dev server'; command='python -m http.server'" in text
    assert "- proc-done (proc://proc-done): exited, exit code 0; description='one shot'; command='ls'" in text
    assert "- proc-gone (proc://proc-gone): externally managed; description='watcher'; command='watch'" in text
    # The model-visible listing never exposes private spool paths.
    assert live.stdout_path not in text
    assert gone.stderr_path not in text
    rows = {row.process_id: row for row in body.processes}
    assert rows["proc-live"].description == "dev server"
    assert rows["proc-live"].command == "python -m http.server"
    assert rows["proc-live"].pid == 101
    assert rows["proc-live"].exit_code is None
    assert rows["proc-done"].exit_code == 0
    # A persisted id this host does not supervise keeps its identity but is never called live.
    assert rows["proc-gone"].pid == 103
    assert rows["proc-gone"].stdout_path == gone.stdout_path
    assert rows["proc-gone"].started_at == "2026-01-01T00:00:00+00:00"
    assert services.names() == ["get_state", "list_processes"]


def test_ps_keeps_host_identity_without_a_persisted_record(tmp_path: Path) -> None:
    handle = _handle("proc-host-only", pid=104)
    services = _FakeServices(handles=[handle])

    result = _invoke({"op": "ps"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    assert isinstance(result.body, BackgroundProcessListBody)
    (row,) = result.body.processes
    assert row.process_id == handle.process_id
    assert row.pid == handle.pid
    assert row.state == "running"
    assert row.stdout_path == handle.stdout_path
    assert row.stderr_path == handle.stderr_path
    assert row.command == row.description == ""
    assert row.started_at is None
    assert services.names() == ["get_state", "list_processes"]


def test_ps_of_an_empty_workspace_lists_nothing(tmp_path: Path) -> None:
    services = _FakeServices()

    result = _invoke({"op": "ps"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessListBody)
    assert body.processes == ()
    assert body.count == 0
    assert result.output.text == "Background process list: 0 process(es)."


def test_ps_never_shows_another_sessions_records(tmp_path: Path) -> None:
    foreign = _handle("proc-foreign", pid=201)
    mine = _handle("proc-mine", pid=202, running=False, exit_code=0)
    services = _FakeServices(
        state=_state(
            _persisted(foreign, description="someone else", origin_session_id="other-session"),
            _persisted(mine, description="mine", state="exited"),
        ),
    )

    result = _invoke({"op": "ps"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessListBody)
    assert [row.process_id for row in body.processes] == ["proc-mine"]
    assert "proc-foreign" not in result.output.text
    assert "someone else" not in result.output.text


@pytest.mark.parametrize("op", ["logs", "send", "stop"])
def test_a_foreign_process_id_is_indistinguishable_from_unknown(tmp_path: Path, op: str) -> None:
    foreign = _handle("proc-foreign", pid=203)
    services = _FakeServices(state=_state(_persisted(foreign, origin_session_id="other-session")))
    arguments: dict[str, object] = {"op": op, "process_id": "proc-foreign"}
    if op == "send":
        arguments["input"] = "hi"

    result = _invoke(arguments, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert result.error == "unknown background process: proc-foreign"
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "unknown_process_id"
    assert "externally managed" not in result.error


def test_ps_text_is_line_and_budget_bounded(tmp_path: Path) -> None:
    from voidcode.capabilities.exec import background_process

    command = "x" * 5000
    description = "d" * 5000
    handles = [_handle(f"proc-long-{index:02d}", pid=400 + index) for index in range(MAX_PROCESS_ROWS)]
    services = _FakeServices(
        handles=handles,
        state=_state(*(_persisted(handle, description=description, command=command) for handle in handles)),
    )

    result = _invoke({"op": "ps"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    text = result.output.text
    assert len(text.encode("utf-8")) <= background_process._LOG_RENDER_BYTES
    assert "... (line truncated to 2000 chars)" in text
    assert "omitted from the model text by the render budget; the typed body retains them" in text
    body = result.body
    assert isinstance(body, BackgroundProcessListBody)
    assert body.count == MAX_PROCESS_ROWS
    assert body.processes[0].command == command
    assert body.processes[0].description == description
    assert result.output.bounds.truncated is True


def test_logs_pages_lines_by_offset_and_reports_the_next_offset(tmp_path: Path) -> None:
    handle = _handle("proc-logs", pid=7)
    services = _FakeServices(
        handles=[handle],
        logs={("proc-logs", "stdout"): b"one\ntwo\nthree\nfour\nfive\n"},
        state=_state(_persisted(handle)),
    )

    first = _invoke({"op": "logs", "process_id": "proc-logs", "stream": "stdout", "offset": 1, "limit": 2}, tmp_path, services)

    assert isinstance(first, ToolSuccess)
    body = first.body
    assert isinstance(body, BackgroundProcessLogsBody)
    assert body.stream == "stdout"
    assert body.offset == 1
    assert body.limit == 2
    assert body.stdout == "two\nthree\n"
    assert body.stdout_lines == 2
    assert body.stderr == ""
    assert body.stderr_lines == 0
    assert body.state == "running"
    assert body.running is True
    assert body.exit_code is None
    assert body.next_offset == 3
    assert body.next_offsets == {"stdout": 3}
    assert body.truncated is True
    assert body.guidance == "This is a bounded page from the current log snapshot. Continue with the next offset(s) shown above."
    assert first.output.text.endswith(f"Guidance: {body.guidance}")
    assert first.output.text.startswith("[stdout]\ntwo\nthree\n")
    assert "Next offset: 3." in first.output.text
    # The model-visible page never names the private spool path.
    assert handle.stdout_path not in first.output.text
    assert handle.stderr_path not in first.output.text

    second = _invoke({"op": "logs", "process_id": "proc-logs", "stream": "stdout", "offset": body.next_offset, "limit": 2}, tmp_path, services)

    assert isinstance(second, ToolSuccess)
    tail = second.body
    assert isinstance(tail, BackgroundProcessLogsBody)
    assert tail.stdout == "four\nfive\n"
    assert tail.next_offset is None
    assert tail.next_offsets == {}
    # Offset > 0 alone is not truncation; this page reaches the captured end.
    assert tail.truncated is False
    assert "Next offset:" not in second.output.text
    # The tool reads the host's bounded byte windows, never a whole log in one call.
    reads = services.spawns_of("read_process_output")
    assert reads and all(read["limit"] == _LOG_PAGE_BYTES and read["offset"] % _LOG_PAGE_BYTES == 0 for read in reads)


def test_logs_reads_both_streams_and_keeps_their_offsets_apart(tmp_path: Path) -> None:
    handle = _handle("proc-both", pid=8)
    services = _FakeServices(
        handles=[handle],
        logs={("proc-both", "stdout"): b"out-1\nout-2\n", ("proc-both", "stderr"): b"err-1\n"},
        state=_state(_persisted(handle)),
    )

    result = _invoke({"op": "logs", "process_id": "proc-both", "limit": 1}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessLogsBody)
    assert body.stream == "both"
    assert body.stdout == "out-1\n"
    assert body.stderr == "err-1\n"
    assert body.stdout_lines == 1
    assert body.stderr_lines == 1
    # `both` has no single next offset: each stream carries its own.
    assert body.next_offset is None
    assert body.next_offsets == {"stdout": 1}
    assert "[stdout]\nout-1\n" in result.output.text
    assert "[stderr]\nerr-1\n" in result.output.text
    assert "Next offsets: stdout=1." in result.output.text


def test_logs_of_an_exited_process_reads_the_final_snapshot(tmp_path: Path) -> None:
    handle = _handle("proc-done", pid=9, running=False, exit_code=0)
    services = _FakeServices(handles=[handle], logs={("proc-done", "stdout"): b"bye\n"}, state=_state(_persisted(handle, state="exited")))

    result = _invoke({"op": "logs", "process_id": "proc-done", "stream": "stdout"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessLogsBody)
    assert body.state == "exited"
    assert body.running is False
    assert body.exit_code == 0
    assert body.stdout == "bye\n"
    assert body.next_offset is None
    assert body.guidance == "This is a bounded page from the final log snapshot."
    assert body.truncated is False


def test_logs_scan_cap_guides_proc_byte_paging_without_spool_paths(tmp_path, monkeypatch):
    from voidcode.capabilities.exec import background_process

    monkeypatch.setattr(background_process, "_LOG_SCAN_BYTES", 4)
    handle = _handle("proc-cap", pid=10, running=False, exit_code=0)
    services = _FakeServices(handles=[handle], logs={("proc-cap", "stdout"): b"one\ntwo\nthree\n"}, state=_state(_persisted(handle, state="exited")))

    result = _invoke({"op": "logs", "process_id": "proc-cap", "stream": "stdout"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessLogsBody)
    assert body.truncated is True
    assert "Only the first 4 bytes of stdout were scanned" in result.output.text
    assert "read(proc://proc-cap, byte_offset=4)" in result.output.text
    assert handle.stdout_path not in result.output.text


def test_logs_render_budget_advances_the_cursor_instead_of_overflowing_projection(tmp_path, monkeypatch):
    from voidcode.capabilities.exec import background_process

    monkeypatch.setattr(background_process, "_LOG_RENDER_BYTES", 10)
    handle = _handle("proc-budget", pid=11, running=False, exit_code=0)
    services = _FakeServices(
        handles=[handle],
        logs={("proc-budget", "stdout"): b"aaaa\nbbbb\ncccc\n"},
        state=_state(_persisted(handle, state="exited")),
    )

    result = _invoke({"op": "logs", "process_id": "proc-budget", "stream": "stdout", "limit": 3}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessLogsBody)
    assert body.stdout == "aaaa\nbbbb\n"
    assert body.stdout_lines == 2
    assert body.next_offset == 2
    assert body.next_offsets == {"stdout": 2}
    assert body.truncated is True
    assert "Next offset: 2." in result.output.text
    assert len(result.output.text.encode("utf-8")) < TOOL_OUTPUT_BYTES


def test_logs_caps_one_oversized_line_and_discloses_the_clip(tmp_path):
    handle = _handle("proc-line", pid=12, running=False, exit_code=0)
    services = _FakeServices(
        handles=[handle],
        logs={("proc-line", "stdout"): ("x" * 3000 + "\n").encode()},
        state=_state(_persisted(handle, state="exited")),
    )

    result = _invoke({"op": "logs", "process_id": "proc-line", "stream": "stdout"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessLogsBody)
    assert body.truncated is True
    assert "... (line truncated to 2000 chars)" in result.output.text
    assert len(body.stdout) < 2300


def test_send_writes_the_input_and_reports_the_accepted_bytes(tmp_path: Path) -> None:
    handle = _handle("proc-send", pid=21)
    services = _FakeServices(handles=[handle], state=_state(_persisted(handle)))

    result = _invoke({"op": "send", "process_id": "proc-send", "input": "status"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    assert services.call("write_process")["data"] == b"status\n"
    body = result.body
    assert isinstance(body, BackgroundProcessSendBody)
    assert body.requested_bytes == 7
    assert body.written_bytes == 7
    assert body.newline is True
    assert body.error is None
    assert result.output.text == "Wrote 7 of 7 UTF-8 bytes to background process proc-send."

    without_newline = _invoke({"op": "send", "process_id": "proc-send", "input": "quit", "newline": False}, tmp_path, services)

    assert isinstance(without_newline, ToolSuccess)
    assert services.spawns_of("write_process")[-1]["data"] == b"quit"
    assert without_newline.output.text == "Wrote 4 of 4 UTF-8 bytes to background process proc-send."


def test_send_reports_a_closed_stdin_as_an_input_failure(tmp_path: Path) -> None:
    handle = _handle("proc-send", pid=22, running=False, exit_code=0)
    services = _FakeServices(
        handles=[handle],
        state=_state(_persisted(handle)),
        write_error=ValueError("background process proc-send was started without a stdin pipe"),
    )

    result = _invoke({"op": "send", "process_id": "proc-send", "input": "status"}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert "is not accepting input" in result.error
    assert "was started without a stdin pipe" in result.error
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "stdin_unavailable"
    assert result.diagnostics.details["requested_bytes"] == 7
    assert result.body is None
    assert services.names() == ["get_state", "list_processes", "write_process"]


def test_send_refuses_oversized_input_before_touching_services(tmp_path: Path) -> None:
    services = _FakeServices()
    # Half the byte limit in characters, doubled by multi-byte UTF-8, plus the newline.
    text = "é" * (MAX_SEND_BYTES // 2)

    result = _invoke({"op": "send", "process_id": "proc-send", "input": text}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_input_validation"
    assert result.diagnostics.details["reason"] == "input_too_large"
    assert result.diagnostics.details["requested_bytes"] == MAX_SEND_BYTES + 1
    assert services.names() == []


def test_send_refuses_input_past_the_schema_character_limit(tmp_path: Path) -> None:
    services = _FakeServices()

    result = _invoke({"op": "send", "process_id": "proc-send", "input": "x" * (MAX_SEND_BYTES + 1)}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_input_validation"
    assert services.names() == []


def test_send_reports_a_partial_write_as_a_failure(tmp_path: Path) -> None:
    handle = _handle("proc-send", pid=23)
    services = _FakeServices(handles=[handle], state=_state(_persisted(handle)))
    services.write_process = lambda process_id, data: 2  # type: ignore[method-assign]

    result = _invoke({"op": "send", "process_id": "proc-send", "input": "status"}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert "accepted only part of the input" in result.error
    body = result.body
    assert isinstance(body, BackgroundProcessSendBody)
    assert body.written_bytes == 2
    assert body.requested_bytes == 7
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "partial_stdin_write"
    assert result.execution is not None
    assert isinstance(result.execution, ConfirmedStop)
    assert result.execution.cancellation_signalled is False


def test_send_cancelled_before_the_write_writes_nothing(tmp_path: Path) -> None:
    signal = _AbortSignal()
    handle = _handle("proc-send", pid=24)

    class CancelOnList(_FakeServices):
        def list_processes(self) -> tuple[ProcessHandle, ...]:
            handles = super().list_processes()
            signal.cancel()
            return handles

    services = CancelOnList(handles=[handle], state=_state(_persisted(handle)))

    result = _invoke({"op": "send", "process_id": "proc-send", "input": "status"}, tmp_path, services, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert "cancelled before writing" in result.error
    assert "write_process" not in services.names()
    body = result.body
    assert isinstance(body, BackgroundProcessSendBody)
    assert body.written_bytes == 0
    assert result.execution is not None
    assert isinstance(result.execution, ConfirmedStop)
    assert result.execution.cancellation_signalled is True


def test_send_cancelled_after_the_write_reports_the_accepted_bytes(tmp_path: Path) -> None:
    signal = _AbortSignal()
    handle = _handle("proc-send", pid=25)

    class CancelOnWrite(_FakeServices):
        def write_process(self, process_id: str, data: bytes | str) -> int:
            written = super().write_process(process_id, data)
            signal.cancel()
            return written

    services = CancelOnWrite(handles=[handle], state=_state(_persisted(handle)))

    result = _invoke({"op": "send", "process_id": "proc-send", "input": "status"}, tmp_path, services, abort_signal=signal)

    assert isinstance(result, ToolFailure)
    assert "cancelled after writing 7 of 7 bytes" in result.error
    body = result.body
    assert isinstance(body, BackgroundProcessSendBody)
    assert body.written_bytes == 7
    assert result.execution is not None
    assert isinstance(result.execution, ConfirmedStop)
    assert result.execution.cancellation_signalled is True


def test_stop_cancels_the_process_group_and_records_the_exit(tmp_path: Path) -> None:
    handle = _handle("proc-stop", pid=31)
    services = _FakeServices(handles=[handle], state=_state(_persisted(handle, description="dev server", command="serve")))

    result = _invoke({"op": "stop", "process_id": "proc-stop"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    assert services.call("cancel_process") == {"process_id": "proc-stop", "reason": "background_process stop"}
    body = result.body
    assert isinstance(body, BackgroundProcessStopBody)
    assert body.state == "exited"
    assert body.running is False
    assert body.exit_code == -15
    assert result.output.text == "Stopped background process proc-stop."
    record = services.state["processes"]["proc-stop"]
    assert record["state"] == "exited"
    assert record["exit_code"] == -15
    assert record["description"] == "dev server"
    assert record["command"] == "serve"
    assert services.call("replace_state")["expected_version"] == 0


def test_stop_of_an_id_this_host_does_not_supervise_is_externally_managed(tmp_path: Path) -> None:
    gone = _handle("proc-gone", pid=41)
    services = _FakeServices(state=_state(_persisted(gone, description="watcher", command="watch")))

    result = _invoke({"op": "stop", "process_id": "proc-gone"}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert "externally managed" in result.error
    assert "survived a host restart" in result.error
    body = result.body
    assert isinstance(body, BackgroundProcessExternalBody)
    assert body.process_id == "proc-gone"
    assert body.pid == 41
    assert body.start_time == gone.start_time
    assert body.state == _EXTERNAL
    assert body.reason == result.error
    assert body.stdout_path == gone.stdout_path
    # The identity is all there is: this tool never guesses at another session's process group.
    assert services.names() == ["get_state", "list_processes"]


def test_stop_reports_an_unconfirmed_stop_as_a_failure(tmp_path: Path) -> None:
    handle = _handle("proc-stubborn", pid=51)
    services = _FakeServices(handles=[handle], state=_state(_persisted(handle)), cancel_result=False)

    result = _invoke({"op": "stop", "process_id": "proc-stubborn"}, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert result.error == "could not confirm background process proc-stubborn stopped; process group remains or termination is unconfirmed"
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "stop_unconfirmed"
    # A stop that was not confirmed leaves the persisted state alone.
    assert "replace_state" not in services.names()


@pytest.mark.parametrize(
    "arguments",
    [
        {"op": "logs", "process_id": "proc-missing"},
        {"op": "send", "process_id": "proc-missing", "input": "hi"},
        {"op": "stop", "process_id": "proc-missing"},
    ],
)
def test_an_unknown_process_id_fails_clearly(tmp_path: Path, arguments: Mapping[str, object]) -> None:
    services = _FakeServices()

    result = _invoke(arguments, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert result.error == "unknown background process: proc-missing"
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "unknown_process_id"
    assert services.names() == ["get_state", "list_processes"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"op": "restart"},
        {"op": "start", "command": "serve", "ready_timeout": True},
        {"op": "start", "command": "serve", "ready_timeout": "5"},
        {"op": "logs", "process_id": "proc-1", "offset": 1.0},
        {"op": "logs", "process_id": "proc-1", "limit": "5"},
        {"op": "send", "process_id": "proc-1", "input": "quit", "newline": "false"},
        {"op": "start"},
        {"op": "start", "command": "   "},
        {"op": "ps", "process_id": "proc-1"},
        {"op": "logs"},
        {"op": "send", "process_id": "proc-1"},
        {"op": "send", "process_id": "proc-1", "input": ""},
        {"op": "stop"},
    ],
)
def test_invalid_arguments_fail_before_touching_services(tmp_path: Path, arguments: Mapping[str, object]) -> None:
    services = _FakeServices()

    result = _invoke(arguments, tmp_path, services)

    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.kind == "tool_input_validation"
    assert services.names() == []


@pytest.mark.parametrize(
    "state",
    [
        {},
        {"schema_version": 1, "processes": []},
        {"schema_version": True, "processes": {}},
        {"schema_version": 2, "processes": {}},
        {"schema_version": 1, "processes": {"broken": None}},
        _state({**_persisted(_handle("proc-bad", pid=123)), "pid": 0}),
        _state({**_persisted(_handle("proc-bad", pid=123)), "pid": True}),
        _state({**_persisted(_handle("proc-bad", pid=123)), "command": ""}),
        _state({key: value for key, value in _persisted(_handle("proc-bad", pid=123)).items() if key != "start_time"}),
        {"schema_version": 1, "processes": {"wrong-key": _persisted(_handle("proc-bad", pid=123))}},
    ],
)
def test_corrupt_process_state_is_not_an_empty_or_fabricated_inventory(tmp_path: Path, state: Mapping[str, object]) -> None:
    services = _FakeServices(state=state)
    if not state:

        def empty_state(scope="workspace"):
            services.calls.append(("get_state", {"scope": scope}))
            return {}, 0

        services.get_state = empty_state
    result = _invoke({"op": "ps"}, tmp_path, services)
    assert isinstance(result, ToolFailure)
    assert result.diagnostics is not None
    assert result.diagnostics.details["reason"] == "corrupt_process_state"
    assert services.names() == ["get_state"]


def test_log_pages_preserve_utf8_and_crlf_across_byte_boundaries(tmp_path, monkeypatch):
    from voidcode.capabilities.exec import background_process

    monkeypatch.setattr(background_process, "_LOG_PAGE_BYTES", 2)
    handle = _handle("proc-split", pid=7, running=False, exit_code=0)
    services = _FakeServices(
        handles=[handle], logs={("proc-split", "stdout"): "甲\r\n乙\r\n".encode()}, state=_state(_persisted(handle, state="exited"))
    )
    result = _invoke({"op": "logs", "process_id": "proc-split", "stream": "stdout"}, tmp_path, services)
    assert result.body.stdout == "甲\n乙\n"
    assert result.body.stdout_lines == 2
    assert result.body.next_offset is None
    assert result.body.truncated is False


@pytest.mark.parametrize("running", [False, True])
def test_readiness_finds_utf8_pattern_after_multiple_byte_pages(tmp_path, monkeypatch, running):
    from voidcode.capabilities.exec import background_process

    monkeypatch.setattr(background_process, "_LOG_PAGE_BYTES", 2)
    monkeypatch.setattr(background_process, "_READY_POLL_SECONDS", 0)
    services = _FakeServices(logs={"stdout": "boot\n就绪\n".encode()}, spawn_running=running, spawn_exit_code=None if running else 0)
    result = _invoke({"op": "start", "command": "serve", "ready_pattern": "就绪", "ready_timeout": 1}, tmp_path, services)
    assert result.body.ready is True
    assert result.body.running is running


def test_readiness_rechecks_stdout_when_process_exits_between_stream_reads(tmp_path):
    class Services(_FakeServices):
        transitioned = False

        def read_process_output(self, process_id, *, stream="stdout", offset=0, limit=_HOST_PAGE_LIMIT):
            page = super().read_process_output(process_id, stream=stream, offset=offset, limit=limit)
            if stream == "stdout" and not self.transitioned:
                self.transitioned = True
                self.logs["stdout"] = b"listening\n"
                self.handles = [replace(handle, running=False, exit_code=0) for handle in self.handles]
            return page

    result = _invoke({"op": "start", "command": "serve", "ready_pattern": "listening", "ready_timeout": 1}, tmp_path, Services())
    assert result.body.ready is True
    assert result.body.state == "exited"


def test_proc_output_default_page_is_not_projection_clipped(tmp_path: Path) -> None:
    handle = _handle("proc-page", pid=61)
    services = _FakeServices(
        handles=[handle],
        logs={("proc-page", "stdout"): b"x" * (MAX_RESOURCE_PAGE_BYTES + 4096)},
    )
    caller = ResourceReadCaller(session_id="session-test", workspace=tmp_path, revision_id="rev-test", run_id="run-test")

    page = read_process_resource(ResourceReadRequest(uri="proc://proc-page"), caller, services)  # type: ignore[arg-type]

    assert page is not None
    reads = services.spawns_of("read_process_output")
    assert reads[-1]["limit"] == MAX_RESOURCE_PAGE_BYTES
    assert page.body["byte_count"] == MAX_RESOURCE_PAGE_BYTES
    raw_content = page.body["raw_content"]
    assert isinstance(raw_content, str) and len(raw_content) == MAX_RESOURCE_PAGE_BYTES
    assert len(page.output.text.encode("utf-8")) <= TOOL_OUTPUT_BYTES
