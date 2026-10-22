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
    BackgroundProcessExternalBody,
    BackgroundProcessListBody,
    BackgroundProcessLogsBody,
    BackgroundProcessSendBody,
    BackgroundProcessStartBody,
    BackgroundProcessStopBody,
)
from voidcode.plugins.exec import create_plugin
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.services import ProcessHandle, ProcessOutput
from voidcode.sdk.tools import ToolCall, ToolEffect, ToolFailure, ToolSuccess

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
            stdout_path=str(stdout_path),
            stderr_path=str(stderr_path),
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


def _persisted(handle: ProcessHandle, *, description: str = "", command: str = "sleep 60", state: str = "running") -> dict[str, object]:
    """The record `start` writes: identity, where it logs, and what the caller called it."""
    return {
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


def _state(*records: Mapping[str, object]) -> dict[str, object]:
    return {"schema_version": 1, "processes": {str(record["process_id"]): dict(record) for record in records}}


def _tool():
    return next(tool for tool in create_plugin({}).tools if tool.definition.name == "background_process")


class _AbortSignal:
    """The core's cancel state as the host swaps it into a live invocation."""

    def __init__(self, *, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.reason: str | None = "cancelled by test" if cancelled else None


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
    assert definition.effects == frozenset({ToolEffect.EXECUTE, ToolEffect.SPAWN})
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
def test_the_declared_schema_is_the_legacy_op_contract(arguments: Mapping[str, object], accepted: bool) -> None:
    tool = next(tool for tool in create_plugin({}).tools if tool.definition.name == "background_process")
    schema = json_wire_object(tool.definition.input_schema)

    assert (not list(jsonschema.Draft202012Validator(schema).iter_errors(dict(arguments)))) is accepted


def test_start_launches_the_workspace_shell_and_persists_the_identity(tmp_path: Path) -> None:
    services = _FakeServices()

    result = _invoke({"op": "start", "command": "python -m http.server", "description": "dev server"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    spawn = services.call("spawn_process")
    handle = services.spawn_handle
    log_dir = tmp_path.resolve() / ".voidcode" / "background-processes"
    assert str(spawn["process_id"]).startswith("proc-")
    assert spawn["argv"] == [*_SHELL_WRAPPER, "python -m http.server"]
    assert spawn["cwd"] == tmp_path.resolve()
    assert spawn["stdin"] is True
    assert spawn["env"] is None
    assert spawn["stdout_path"] == log_dir / f"{handle.process_id}.stdout.log"
    assert spawn["stderr_path"] == log_dir / f"{handle.process_id}.stderr.log"
    # The legacy layout: the log directory exists in the workspace before the process runs.
    assert log_dir.is_dir()
    body = result.body
    assert isinstance(body, BackgroundProcessStartBody)
    assert body.process_id == handle.process_id
    assert body.pid == handle.pid
    assert body.state == "running"
    assert body.description == "dev server"
    assert body.stdout_path == str(log_dir / f"{handle.process_id}.stdout.log")
    assert body.ready is None
    assert body.ready_pattern is None
    assert body.exit_code is None
    guidance = f"Track this process by process_id '{handle.process_id}'. Use background_process with op=logs, send, or stop."
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
        "started_at": record["started_at"],
    }
    assert isinstance(record["started_at"], str) and record["started_at"]
    assert replace_call["state"] == {"schema_version": 1, "processes": {handle.process_id: record}}
    # A process still running after the start is written once: no wait can cost the identity.
    assert len(services.spawns_of("replace_state")) == 1


def test_a_later_start_keeps_the_identities_already_persisted(tmp_path: Path) -> None:
    first = _handle("proc-first", pid=11)
    services = _FakeServices(state=_state(_persisted(first, description="first")), version=4)

    result = _invoke({"op": "start", "command": "serve"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    records = services.state["processes"]
    assert set(records) == {"proc-first", services.spawn_handle.process_id}
    assert records["proc-first"]["description"] == "first"
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
    assert "aborted before launching the process" in (result.error or "")
    assert result.execution is not None and result.execution.cancellation_signalled is True
    assert result.execution.side_effect_state == "settled"
    # The refusal is pre-flight: no process was started and no identity was written.
    assert services.names() == []
    assert services.state == {}


def test_start_propagates_the_core_duplicate_refusal(tmp_path: Path) -> None:
    services = _FakeServices(spawn_error=ValueError("background process proc-abc is already running"))

    with pytest.raises(ValueError, match="already running"):
        _invoke({"op": "start", "command": "serve"}, tmp_path, services)

    assert services.names() == ["get_state", "spawn_process"]
    assert services.state == {}


def test_start_surfaces_a_read_only_lease_permission_error(tmp_path: Path) -> None:
    refusal = PermissionError("this lease may not run background processes: read-only and hook leases do not touch the machine")
    services = _FakeServices(spawn_error=refusal)

    with pytest.raises(PermissionError, match="read-only and hook leases do not touch the machine"):
        _invoke({"op": "start", "command": "serve"}, tmp_path, services)


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
    assert result.output.text == "Background process list: 3 process(es)."
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


def test_ps_of_an_empty_workspace_lists_nothing(tmp_path: Path) -> None:
    services = _FakeServices()

    result = _invoke({"op": "ps"}, tmp_path, services)

    assert isinstance(result, ToolSuccess)
    body = result.body
    assert isinstance(body, BackgroundProcessListBody)
    assert body.processes == ()
    assert body.count == 0
    assert result.output.text == "Background process list: 0 process(es)."


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
    assert body.guidance == "This is a bounded page from the current log snapshot; use offset to inspect another page or continue other work."
    assert first.output.text.endswith(f"Guidance: {body.guidance}")
    assert first.output.text.startswith("[stdout]\ntwo\nthree\n")

    second = _invoke({"op": "logs", "process_id": "proc-logs", "stream": "stdout", "offset": body.next_offset, "limit": 2}, tmp_path, services)

    assert isinstance(second, ToolSuccess)
    tail = second.body
    assert isinstance(tail, BackgroundProcessLogsBody)
    assert tail.stdout == "four\nfive\n"
    assert tail.next_offset is None
    assert tail.next_offsets == {}
    # Paging past the start of the log is the only reason a completed page is still partial.
    assert tail.truncated is True
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
    assert body.guidance == "This is a bounded page from the final log snapshot; use offset to inspect another page if needed."


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
    body = result.body
    assert isinstance(body, BackgroundProcessSendBody)
    assert body.requested_bytes == 7
    assert body.written_bytes == 0
    assert body.error == result.error
    assert result.output.text == "Wrote 0 of 7 UTF-8 bytes to background process proc-send."


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
    assert "survived a host restart or was started by another session" in result.error
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
    assert result.output.text == result.error
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

    with pytest.raises(ValueError, match="unknown background process: proc-missing"):
        _invoke(arguments, tmp_path, services)

    assert services.names() == ["get_state", "list_processes"]


@pytest.mark.parametrize(
    "arguments",
    [
        {"op": "restart"},
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

    with pytest.raises(ValueError):
        _invoke(arguments, tmp_path, services)

    assert services.names() == []


def test_a_handler_without_the_process_service_fails_visibly(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="background_process requires the core background process service"):
        _tool().invoke(ToolCall("background_process", {"op": "ps"}), context=_context(tmp_path), services=None)  # type: ignore[arg-type]

    class _StateOnly:
        def get_state(self, scope: str = "session") -> tuple[Mapping[str, object] | None, int]:
            return None, 0

        def replace_state(self, state: Mapping[str, object], expected_version: int, scope: str = "session") -> int:
            return expected_version + 1

    with pytest.raises(ValueError, match="background_process requires the core background process service"):
        _tool().invoke(ToolCall("background_process", {"op": "start", "command": "serve"}), context=_context(tmp_path), services=_StateOnly())  # type: ignore[arg-type]
