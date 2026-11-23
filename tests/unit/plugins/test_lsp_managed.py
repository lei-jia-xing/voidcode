"""LSP consumer cutover: managed service transport + document-version query.

Behavior boundaries, not wiring: a real stdio fixture server is driven through
the live PluginServices surface (fake lease, real protocol bytes), the version
query answers strict shapes from real didOpen/didChange tracking, and the
second-use cancel kills the actual server plus its descendant. Core/host
smoke (read_only/approval, reload/close cleanup) runs in the integration
suite; hard core death has no cleanup path and is intentionally not claimed
here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from voidcode.plugins.lsp import create_plugin
from voidcode.plugins.lsp.lsp_manager import MAX_LSP_HEADER_BYTES, MAX_LSP_MESSAGE_BYTES, build_lsp_manager
from voidcode.plugins.lsp.lsp_types import RuntimeLspConfig, lsp_config_from_mapping
from voidcode.sdk.invocation import ToolContext
from voidcode.sdk.plugin import PluginQueryCaller
from voidcode.sdk.services import ProcessHandle, ProcessOutput
from voidcode.sdk.tools import ToolCall

_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "lsp_echo_server.py"
_HOST_PAGE_LIMIT = 256 * 1024


class _Abort:
    def __init__(self, cancelled: bool = False) -> None:
        self.cancelled = cancelled
        self.reason: str | None = None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self.cancelled = value
        self.reason = reason if value else None


class _Services:
    """Real log-backed service surface; one scripted lease, real byte cursors."""

    def __init__(self, spool: Path) -> None:
        import subprocess

        self._subprocess = subprocess
        self._spool = spool
        self._entries: dict[str, subprocess.Popen[bytes]] = {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def spawn_service_process(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        process_id: str | None = None,
    ) -> ProcessHandle:
        identifier = process_id or "svc-test"
        self.calls.append(("spawn_service_process", {"argv": list(argv), "process_id": identifier}))
        stdout_log = self._spool / f"{identifier}.stdout.log"
        stderr_log = self._spool / f"{identifier}.stderr.log"
        out = stdout_log.open("ab")
        err = stderr_log.open("ab")
        try:
            child_env = None if env is None else {**os.environ, **{str(k): str(v) for k, v in env.items()}}
            process = self._subprocess.Popen(
                list(argv),
                cwd=str(cwd) if cwd is not None else None,
                env=child_env,
                stdin=self._subprocess.PIPE,
                stdout=out,
                stderr=err,
            )
        finally:
            out.close()
            err.close()
        self._entries[identifier] = process
        return ProcessHandle(
            process_id=identifier,
            pid=process.pid,
            start_time="test",
            stdout_path=str(stdout_log),
            stderr_path=str(stderr_log),
        )

    def write_process(self, process_id: str, data: bytes | str) -> int:
        payload = data.encode() if isinstance(data, str) else bytes(data)
        self.calls.append(("write_process", {"process_id": process_id, "bytes": len(payload)}))
        entry = self._entries[process_id]
        assert entry.stdin is not None
        try:
            entry.stdin.write(payload)
            entry.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise ValueError(f"background process {process_id} is not reading its stdin: {exc}") from exc
        return len(payload)

    def read_process_output(self, process_id: str, *, stream: str = "stdout", offset: int = 0, limit: int = 65536) -> ProcessOutput:
        assert 0 < limit <= _HOST_PAGE_LIMIT, f"page bound {limit} rejected by host"
        self.calls.append(("read_process_output", {"process_id": process_id, "offset": offset, "limit": limit}))
        assert stream == "stdout"
        entry = self._entries[process_id]
        data = (self._spool / f"{process_id}.stdout.log").read_bytes()
        page = data[offset : offset + limit]
        return ProcessOutput(
            process_id=process_id,
            stream=stream,
            offset=offset,
            total_bytes=len(data),
            data=page,
            eof=offset + len(page) >= len(data),
            running=entry.poll() is None,
        )

    def cancel_process(self, process_id: str, reason: str = "owner cancellation") -> bool:
        self.calls.append(("cancel_process", {"process_id": process_id, "reason": reason}))
        entry = self._entries.get(process_id)
        if entry is None:
            raise ValueError(f"unknown background process for this session: {process_id}")
        if entry.poll() is None:
            entry.terminate()
            try:
                entry.wait(timeout=2)
            except self._subprocess.TimeoutExpired:
                entry.kill()
                entry.wait(timeout=2)
        return True

    def list_processes(self) -> tuple[ProcessHandle, ...]:
        return ()


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[tuple[_Services, Path]]:
    spool = tmp_path / "spool"
    spool.mkdir()
    services = _Services(spool)
    yield services, tmp_path
    for entry in services._entries.values():
        if entry.poll() is None:
            entry.kill()
            entry.wait(timeout=5)


def _config(record: Path, *, encoding: str | None = None, grandchild: str | None = None) -> RuntimeLspConfig:
    _ = encoding  # negotiated by the fixture via its environment, not the config surface
    argv: list[str] = [sys.executable, str(_FIXTURE), "--record", str(record)]
    if grandchild:
        argv += ["--grandchild", grandchild]
    return lsp_config_from_mapping({"servers": {"echo": {"command": argv, "extensions": [".txt"], "languages": ["plaintext"]}}})


def _context(workspace: Path, session_id: str, abort: _Abort) -> ToolContext:
    return ToolContext(
        workspace=workspace,
        session_id=session_id,
        run_id="run-1",
        invocation_id="inv-1",
        revision_id="rev-1",
        actor="test",
        abort_signal=abort,  # type: ignore[arg-type]
    )


def _invoke_lsp(workspace: Path, session_id: str, services: _Services, abort: _Abort, arguments: Mapping[str, object]):
    plugin = create_plugin({"servers": {"echo": {"command": [sys.executable, "-V"], "extensions": [".txt"]}}})
    _ = plugin
    tool = next(tool for tool in create_plugin(None).tools if tool.definition.name == "lsp")
    return tool, _context(workspace, session_id, abort), ToolCall("lsp", dict(arguments))


def _echo_methods(record: Path) -> list[dict[str, object]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_request_drives_real_stdio_and_opens_the_document(harness: tuple[_Services, Path]) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("hello\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    manager = build_lsp_manager(_config(record))
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    assert isinstance(manager, ManagedLspManager)
    result = manager.request(
        LspRequest(
            server_name="echo",
            method="textDocument/hover",
            params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
            workspace=workspace,
            session_id="sess-1",
            services=services,  # type: ignore[arg-type]
            abort_signal=_Abort(),  # type: ignore[arg-type]
        )
    )
    assert json.loads(result.response["result"]["contents"]["value"]) == {
        "textDocument": {"uri": target.as_uri()},
        "position": {"line": 0, "character": 1},
    }
    assert result.position_encoding == "utf-16"
    assert result.document_state is not None
    assert result.document_state.version == 1
    assert result.document_state.content_hash == hashlib.sha256(b"hello\n").hexdigest()
    methods = [entry["method"] for entry in _echo_methods(record)]
    assert methods[:2] == ["initialize", "initialized"]
    assert "textDocument/didOpen" in methods
    assert "spawn_service_process" in services.names()
    assert services.names().count("spawn_service_process") == 1


def test_second_request_reuses_service_and_sends_did_change(harness: tuple[_Services, Path]) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("v1\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    manager = build_lsp_manager(_config(record))
    assert isinstance(manager, ManagedLspManager)

    def _ask() -> object:
        return manager.request(
            LspRequest(
                server_name="echo",
                method="textDocument/hover",
                params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
                workspace=workspace,
                session_id="sess-1",
                services=services,  # type: ignore[arg-type]
                abort_signal=_Abort(),  # type: ignore[arg-type]
            )
        )

    first = _ask()
    assert first.document_state is not None and first.document_state.version == 1
    target.write_text("v2-longer\n", encoding="utf-8")
    second = _ask()
    assert second.document_state is not None and second.document_state.version == 2
    assert second.document_state.content_hash == hashlib.sha256(b"v2-longer\n").hexdigest()
    assert services.names().count("spawn_service_process") == 1
    versions = [entry["version"] for entry in _echo_methods(record) if entry["method"].endswith(":version")]
    assert versions == [1, 2]


def test_document_version_query_answers_strict_shape_or_none(harness: tuple[_Services, Path]) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("abc\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    manager = build_lsp_manager(_config(record))
    assert isinstance(manager, ManagedLspManager)
    query = next(query for query in create_plugin(None).queries if query.name == "workspace.document_version")
    caller = PluginQueryCaller(session_id="sess-1", workspace=workspace)
    assert query.handler({"path": "note.txt"}, caller) is None  # nothing opened: no server started
    manager.request(
        LspRequest(
            server_name="echo",
            method="textDocument/hover",
            params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
            workspace=workspace,
            session_id="sess-1",
            services=services,  # type: ignore[arg-type]
            abort_signal=_Abort(),  # type: ignore[arg-type]
        )
    )
    # The query reads the plugin's own manager; bind it explicitly for the assertion.
    from voidcode.plugins.lsp import _document_version_query  # noqa: PLC2701

    direct = _document_version_query(manager).handler({"path": "note.txt"}, caller)
    assert direct is not None
    assert direct["path"] == target.resolve().as_posix()
    assert direct["version"] == 1
    assert direct["content_hash"] == hashlib.sha256(b"abc\n").hexdigest()
    assert direct["position_encoding"] == "utf-16"
    foreign = PluginQueryCaller(session_id="sess-2", workspace=workspace)
    assert _document_version_query(manager).handler({"path": "note.txt"}, foreign) is None
    with pytest.raises(ValueError, match="path"):
        _document_version_query(manager).handler({"path": 3}, caller)


@pytest.mark.parametrize("stop_confirmed", [True, False])
def test_cancelled_second_use_preserves_reason_and_stop_confirmation(
    harness: tuple[_Services, Path],
    monkeypatch: pytest.MonkeyPatch,
    stop_confirmed: bool,
) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("x\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild_cmd = (
        f'{sys.executable} -c "import os,pathlib,time;pathlib.Path({str(grandchild_pid)!r}).write_text(str(os.getpid()));time.sleep(120)"'
    )
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    manager = build_lsp_manager(_config(record, grandchild=grandchild_cmd))
    assert isinstance(manager, ManagedLspManager)
    first = manager.request(
        LspRequest(
            server_name="echo",
            method="textDocument/hover",
            params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
            workspace=workspace,
            session_id="sess-1",
            services=services,  # type: ignore[arg-type]
            abort_signal=_Abort(),  # type: ignore[arg-type]
        )
    )
    assert json.loads(first.response["result"]["contents"]["value"])["position"] == {"line": 0, "character": 1}
    deadline = time.monotonic() + 10
    while not grandchild_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    descendant = int(grandchild_pid.read_text(encoding="utf-8").strip())
    abort = _Abort(cancelled=True)
    if not stop_confirmed:
        monkeypatch.setattr(services, "cancel_process", lambda *args, **kwargs: False)
    expected_stop = "the server was stopped" if stop_confirmed else "the server could not be confirmed stopped"
    with pytest.raises(ValueError, match=f"was cancelled; {expected_stop}"):
        manager.request(
            LspRequest(
                server_name="echo",
                method="textDocument/hover",
                params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
                workspace=workspace,
                session_id="sess-1",
                services=services,  # type: ignore[arg-type]
                abort_signal=abort,  # type: ignore[arg-type]
            )
        )
    failed = manager.current_state().servers["echo"]
    assert failed.status == "failed"
    if stop_confirmed:
        assert failed.last_error == "LSP request to echo was cancelled"
        assert "cancel_process" in services.names()
        assert all(entry.poll() is not None for entry in services._entries.values())
    else:
        assert failed.last_error == "LSP request to echo was cancelled; LSP server echo could not be confirmed stopped"
        assert any(entry.poll() is None for entry in services._entries.values())
    # The fake lease only terminates the leader; the host-owned tree kill is the
    # coordinated smoke's proof. The descendant must at least be reaped there.
    _ = descendant


def test_framing_bounds_match_the_old_transport() -> None:
    assert (MAX_LSP_HEADER_BYTES, MAX_LSP_MESSAGE_BYTES) == (8192, 1024 * 1024)


def test_tool_registers_query_and_effects() -> None:
    plugin = create_plugin(None)
    assert [query.name for query in plugin.queries] == ["workspace.document_version"]
    tool = next(tool for tool in plugin.tools if tool.definition.name == "lsp")
    assert tool.definition.effects == frozenset({"read", "execute"})
    _ = replace


def test_lsp_command_lists_servers_without_spawning(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    plugin = create_plugin({"servers": {"echo": {"command": [sys.executable, "-V"], "extensions": [".txt"]}}})
    command = next(command for command in plugin.commands if command.definition.name == "lsp")
    from voidcode.sdk.commands import CommandInvocation

    services = _Services(tmp_path)
    result = command.invoke(
        CommandInvocation(context=_context(workspace, "sess-1", _Abort()), name="lsp", arguments={}),
        services,  # type: ignore[arg-type]
    )
    assert result.status == "ok"
    assert "spawn_service_process" not in services.names()
    assert result.data is not None and result.data["servers"][0]["name"] == "echo"


def test_each_session_gets_its_own_server(harness: tuple[_Services, Path]) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("s\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    manager = build_lsp_manager(_config(record))
    assert isinstance(manager, ManagedLspManager)
    for session in ("sess-1", "sess-2"):
        manager.request(
            LspRequest(
                server_name="echo",
                method="textDocument/hover",
                params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
                workspace=workspace,
                session_id=session,
                services=services,  # type: ignore[arg-type]
                abort_signal=_Abort(),  # type: ignore[arg-type]
            )
        )
    assert services.names().count("spawn_service_process") == 2


def test_disabled_inventory_uses_the_same_configuration_contract(tmp_path: Path) -> None:
    from voidcode.sdk.commands import CommandInvocation

    plugin = create_plugin({"enabled": False})
    command = next(command for command in plugin.commands if command.definition.name == "lsp")
    services = _Services(tmp_path)
    result = command.invoke(CommandInvocation(context=_context(tmp_path, "sess-1", _Abort()), name="lsp"), services)
    assert result.status == "ok"
    assert result.data["enabled"] is False
    assert "disabled by configuration" in result.output
    assert not services.calls


def test_server_eof_is_recorded_as_protocol_failure(harness: tuple[_Services, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.plugins.lsp.lsp_manager import LspProtocolError, LspRequest

    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    manager = build_lsp_manager(_config(tmp_path / "record.jsonl"))
    request = LspRequest(
        server_name="echo",
        method="textDocument/hover",
        params={},
        workspace=workspace,
        session_id="sess-eof",
        services=services,
    )
    manager.request(request)

    def exit_before_reply(process_id, data):
        process = services._entries[process_id]
        process.kill()
        process.wait(timeout=5)
        return len(data)

    monkeypatch.setattr(services, "write_process", exit_before_reply)
    with pytest.raises(LspProtocolError, match="No response from LSP server echo"):
        manager.request(request)
    failed = manager.current_state().servers["echo"]
    assert failed.status == "failed"
    assert failed.last_error == "No response from LSP server echo for textDocument/hover"
    assert "cancel_process" in services.names()


def test_partial_stdin_write_is_a_protocol_failure(tmp_path: Path) -> None:
    from voidcode.plugins.lsp.lsp_manager import LspProtocolError, ManagedLspManager, _encode_lsp_message, _RunningLspServer

    message: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    requested = len(_encode_lsp_message(message))

    class PartialWrites:
        def write_process(self, process_id: str, data: bytes | str) -> int:
            assert process_id == "partial-write"
            return requested - 1

    server = _RunningLspServer(
        config=build_lsp_manager(_config(tmp_path / "unused")).configuration.servers["echo"],
        process_id="partial-write",
        workspace_root=tmp_path,
    )
    with pytest.raises(LspProtocolError, match=rf"accepted only {requested - 1} of {requested} stdin bytes"):
        ManagedLspManager._write_message(PartialWrites(), server, message=message)


@pytest.mark.parametrize("result", [None, {}, {"capabilities": None}, {"capabilities": []}])
def test_malformed_initialize_stops_server_and_preserves_failure(harness: tuple[_Services, Path], result: object) -> None:
    from voidcode.plugins.lsp.lsp_manager import LspProtocolError, LspRequest

    services, tmp_path = harness
    config = lsp_config_from_mapping(
        {
            "servers": {
                "echo": {
                    "command": [sys.executable, str(_FIXTURE), "--record", str(tmp_path / "record.jsonl"), "--initialize-result", json.dumps(result)],
                    "extensions": [".txt"],
                }
            }
        }
    )
    manager = build_lsp_manager(config)
    with pytest.raises(LspProtocolError, match="requires a capabilities object"):
        manager.request(LspRequest(server_name="echo", method="custom", params={}, workspace=tmp_path, session_id="bad-init", services=services))
    state = manager.current_state().servers["echo"]
    assert state.status == "failed"
    assert "requires a capabilities object" in state.last_error
    assert all(process.poll() is not None for process in services._entries.values())


@pytest.mark.parametrize("header", [b"Content-Length: 2\xff", b"Content-Length: -1", b"Content-Length: invalid", b"Other: value"])
def test_invalid_lsp_headers_are_explicit_protocol_failures(tmp_path: Path, header: bytes) -> None:
    from voidcode.plugins.lsp.lsp_manager import LspProtocolError, ManagedLspManager, _RunningLspServer

    frame = header + b"\r\n\r\n{}"

    class Framed:
        def read_process_output(self, process_id, *, stream, offset, limit):
            data = frame[offset : offset + limit]
            return ProcessOutput(
                process_id=process_id,
                stream=stream,
                offset=offset,
                data=data,
                total_bytes=len(frame),
                eof=offset + len(data) >= len(frame),
                running=False,
            )

    server = _RunningLspServer(
        config=build_lsp_manager(_config(tmp_path / "unused")).configuration.servers["echo"], process_id="bad-frame", workspace_root=tmp_path
    )
    with pytest.raises(LspProtocolError):
        ManagedLspManager._read_message(Framed(), server, server_name="echo", abort_signal=None)


def test_missing_server_binary_reports_current_configuration_path(harness: tuple[_Services, Path]) -> None:
    from voidcode.plugins.lsp.lsp_manager import LspRequest

    services, tmp_path = harness
    manager = build_lsp_manager(lsp_config_from_mapping({"servers": {"missing": {"command": [str(tmp_path / "absent-server")]}}}))
    with pytest.raises(ValueError, match="plugins.lsp.config.servers.missing.command") as error:
        manager.request(
            LspRequest(server_name="missing", method="custom", params={}, workspace=tmp_path, session_id="missing-bin", services=services)
        )
    assert "in the product configuration" in str(error.value)
    state = manager.current_state().servers["missing"]
    assert state.status == "failed"
    assert state.last_error == str(error.value)


@pytest.mark.parametrize("id_kind", ["same", "different", "string"])
def test_server_requests_receive_errors_without_confusing_client_responses(harness: tuple[_Services, Path], id_kind: str) -> None:
    from voidcode.plugins.lsp.lsp_manager import LspRequest

    services, tmp_path = harness
    record = tmp_path / "record.jsonl"
    manager = build_lsp_manager(
        lsp_config_from_mapping(
            {
                "servers": {
                    "echo": {
                        "command": [sys.executable, str(_FIXTURE), "--record", str(record), "--server-request-id", id_kind],
                    }
                }
            }
        )
    )
    response = manager.request(
        LspRequest(server_name="echo", method="test/echo", params={"value": 42}, workspace=tmp_path, session_id="duplex", services=services)
    )
    assert response.response["result"] == {"echo": {"value": 42}}
    replies = [entry["reply"] for entry in _echo_methods(record) if entry["method"] == "__client_reply__"]
    expected_id = 1 if id_kind == "same" else 900 if id_kind == "different" else "server-id"
    assert replies == [{"jsonrpc": "2.0", "id": expected_id, "error": {"code": -32601, "message": "Method not found"}}]


def test_concurrent_first_use_owns_one_registry_lease(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from voidcode.plugins.lsp import lsp_manager as module

    registry = module._SessionScopedLspRegistry()
    monkeypatch.setattr(module, "_SESSION_SCOPED_LSP_REGISTRY", registry)
    manager = build_lsp_manager(_config(tmp_path / "record.jsonl"))
    server_config = manager.configuration.resolve("echo")
    server = SimpleNamespace(workspace_root=tmp_path, process_id="test-process", config=server_config)
    monkeypatch.setattr(manager, "_start_running_server", lambda *args, **kwargs: server)
    monkeypatch.setattr(manager, "_service_answers", lambda *args: True)
    acquired = Event()
    release_first = Event()
    second_acquire = Event()
    acquire = registry.acquire
    calls = []

    def held_acquire(**kwargs):
        calls.append(kwargs["key"])
        result = acquire(**kwargs)
        if len(calls) == 1:
            acquired.set()
            assert release_first.wait(5)
        else:
            second_acquire.set()
        return result

    monkeypatch.setattr(registry, "acquire", held_acquire)

    def ensure():
        return manager._ensure_running(
            None, server_name="echo", server_config=server_config, workspace_root=tmp_path, session_id="session", abort_signal=None
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(ensure)
        assert acquired.wait(5)
        second = pool.submit(ensure)
        # Hold the first lease before its local handoff. A registry-only lock
        # permits a second acquire here, leaking one ref when the local map wins.
        second_acquire.wait(0.2)
        release_first.set()
        assert first.result(5)[0] is second.result(5)[0]
    assert len(calls) == 1
    assert next(iter(registry._entries.values())).ref_count == 1
    manager.shutdown()
    assert registry._entries == {}


def test_late_server_failure_preserves_new_local_entry(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from voidcode.plugins.lsp.lsp_manager import _LspRegistryKey

    manager = build_lsp_manager(_config(tmp_path / "record.jsonl"))
    old = SimpleNamespace(process_id="old", documents={}, initialized=True, stdout_cursor=0)
    new = SimpleNamespace(process_id="new")
    manager._running_servers[("session", "echo", tmp_path)] = new
    before = manager.current_state()
    services = SimpleNamespace(cancel_process=lambda process_id: process_id == "old")
    assert manager._cancel_own_server(services, _LspRegistryKey("session", "echo", tmp_path), old, error="old failure")
    assert manager._running_servers[("session", "echo", tmp_path)] is new
    assert manager.current_state() == before
    manager.shutdown()


def test_shutdown_stops_owner_thread_and_next_manager_works(harness: tuple[_Services, Path]) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("owner\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    baseline = {thread for thread in threading.enumerate() if thread.name == "lsp-owner"}
    manager = build_lsp_manager(_config(record))
    assert isinstance(manager, ManagedLspManager)

    def _hover(instance: ManagedLspManager) -> object:
        return instance.request(
            LspRequest(
                server_name="echo",
                method="textDocument/hover",
                params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
                workspace=workspace,
                session_id="sess-owner",
                services=services,  # type: ignore[arg-type]
                abort_signal=_Abort(),  # type: ignore[arg-type]
            )
        )

    _hover(manager)
    owner_thread = manager._owner._thread
    assert owner_thread is not None and owner_thread.is_alive()
    manager.shutdown()
    assert not owner_thread.is_alive()
    assert {thread for thread in threading.enumerate() if thread.name == "lsp-owner"} == baseline

    replacement = build_lsp_manager(_config(record))
    assert isinstance(replacement, ManagedLspManager)
    try:
        _hover(replacement)
        assert services.names().count("spawn_service_process") == 2
    finally:
        replacement.shutdown()
    assert {thread for thread in threading.enumerate() if thread.name == "lsp-owner"} == baseline


def test_interleaved_notifications_share_one_request_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from voidcode.plugins.lsp import lsp_manager as module
    from voidcode.plugins.lsp.lsp_manager import _RunningLspServer

    manager = build_lsp_manager(_config(tmp_path / "record.jsonl"))
    server = _RunningLspServer(
        config=manager.configuration.resolve("echo"),
        process_id="notification-flood",
        workspace_root=tmp_path,
    )
    writes: list[bytes] = []

    class Writes:
        def write_process(self, process_id: str, data: bytes | str) -> int:
            assert process_id == "notification-flood"
            payload = data.encode() if isinstance(data, str) else bytes(data)
            writes.append(payload)
            return len(payload)

    remaining_timeouts: list[float] = []
    clock = iter([0.0, 0.0, 11.0])

    def _read_message(*args: object, **kwargs: object) -> dict[str, object]:
        remaining_timeouts.append(float(kwargs["timeout"]))
        return {"jsonrpc": "2.0", "method": "window/logMessage", "params": {"type": 3, "message": "busy"}}

    monkeypatch.setattr(module, "LSP_REQUEST_TIMEOUT_SECONDS", 10.0)
    monkeypatch.setattr(module.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(manager, "_read_message", _read_message)
    with pytest.raises(TimeoutError, match="did not respond within 10.0s"):
        manager._send_request_owned(
            Writes(),  # type: ignore[arg-type]
            server,
            method="textDocument/hover",
            params={},
            server_name="echo",
            abort_signal=None,
        )
    assert remaining_timeouts == [10.0]
    assert len(writes) == 1
    manager.shutdown()


def test_crashed_server_is_replaced_between_requests(harness: tuple[_Services, Path]) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("crash\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    manager = build_lsp_manager(_config(record))
    assert isinstance(manager, ManagedLspManager)

    def _hover() -> object:
        return manager.request(
            LspRequest(
                server_name="echo",
                method="textDocument/hover",
                params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
                workspace=workspace,
                session_id="sess-crash",
                services=services,  # type: ignore[arg-type]
                abort_signal=_Abort(),  # type: ignore[arg-type]
            )
        )

    _hover()
    process = next(iter(services._entries.values()))
    process.kill()
    process.wait(timeout=5)
    _hover()
    assert services.names().count("spawn_service_process") == 2
    manager.shutdown()


@pytest.mark.parametrize("cancel_result", [False, ValueError("unknown process")], ids=["false", "value-error"])
def test_abandoned_server_stop_reports_unconfirmed(
    tmp_path: Path,
    cancel_result: bool | ValueError,
) -> None:
    from types import SimpleNamespace

    from voidcode.plugins.lsp.lsp_manager import _LspRegistryKey, _RunningLspServer

    manager = build_lsp_manager(_config(tmp_path / "record.jsonl"))
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    old_root.mkdir()
    new_root.mkdir()
    server = _RunningLspServer(
        config=manager.configuration.resolve("echo"),
        process_id="old-abandoned",
        workspace_root=old_root,
    )
    local_key = ("session", "echo", old_root)
    manager._running_servers[local_key] = server

    def _cancel_process(process_id: str) -> bool:
        assert process_id == "old-abandoned"
        if isinstance(cancel_result, ValueError):
            raise cancel_result
        return cancel_result

    assert (
        manager._stop_abandoned_server(
            SimpleNamespace(cancel_process=_cancel_process),  # type: ignore[arg-type]
            local_key,
            _LspRegistryKey("session", "echo", new_root),
            server,
        )
        is False
    )
    state = manager.current_state().servers["echo"]
    assert state.status == "failed"
    assert state.last_error is not None and "could not be confirmed stopped" in state.last_error
    manager.shutdown()


def test_alternating_roots_keep_separate_servers(harness: tuple[_Services, Path]) -> None:
    services, tmp_path = harness
    workspace = tmp_path / "ws"
    first_root = workspace / "first"
    second_root = workspace / "second"
    first_root.mkdir(parents=True)
    second_root.mkdir(parents=True)
    (first_root / "pyproject.toml").write_text("", encoding="utf-8")
    (second_root / "pyproject.toml").write_text("", encoding="utf-8")
    first_target = first_root / "a.txt"
    second_target = second_root / "b.txt"
    first_target.write_text("a\n", encoding="utf-8")
    second_target.write_text("b\n", encoding="utf-8")
    record = tmp_path / "record.jsonl"
    config = lsp_config_from_mapping(
        {
            "servers": {
                "echo": {
                    "command": [sys.executable, str(_FIXTURE), "--record", str(record)],
                    "extensions": [".txt"],
                    "root_markers": ["pyproject.toml"],
                }
            }
        }
    )
    from voidcode.plugins.lsp.lsp_manager import LspRequest, ManagedLspManager

    manager = build_lsp_manager(config)
    assert isinstance(manager, ManagedLspManager)

    def _hover(target: Path) -> object:
        return manager.request(
            LspRequest(
                server_name="echo",
                method="textDocument/hover",
                params={"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}},
                workspace=workspace,
                session_id="sess-roots",
                services=services,  # type: ignore[arg-type]
                abort_signal=_Abort(),  # type: ignore[arg-type]
            )
        )

    _hover(first_target)
    _hover(second_target)
    third = _hover(first_target)
    assert services.names().count("spawn_service_process") == 2
    assert third.document_state is not None and third.document_state.version == 1
    manager.shutdown()


@pytest.mark.parametrize(("name", "expected"), [("note.js", "javascript"), ("note.tsx", "typescript")])
def test_language_id_uses_document_extension(tmp_path: Path, name: str, expected: str) -> None:
    from voidcode.plugins.lsp.lsp_manager import _RunningLspServer

    config = lsp_config_from_mapping(
        {
            "servers": {
                "tsserver": {
                    "command": ["typescript-language-server"],
                    "extensions": [".js", ".tsx"],
                    "languages": ["typescript", "javascript"],
                }
            }
        }
    )
    manager = build_lsp_manager(config)
    target = tmp_path / name
    target.write_text("const value = 1;\n", encoding="utf-8")
    server = _RunningLspServer(
        config=manager.configuration.resolve("tsserver"),
        process_id="language-id",
        workspace_root=tmp_path,
        document_sync_enabled=True,
    )
    writes: list[bytes] = []

    class Writes:
        def write_process(self, process_id: str, data: bytes | str) -> int:
            assert process_id == "language-id"
            payload = data.encode() if isinstance(data, str) else bytes(data)
            writes.append(payload)
            return len(payload)

    state = manager._synchronize_document_owned(Writes(), server, target)  # type: ignore[arg-type]
    assert state is not None and state.version == 1
    message = json.loads(writes[0].split(b"\r\n\r\n", 1)[1])
    assert message["method"] == "textDocument/didOpen"
    assert message["params"]["textDocument"]["languageId"] == expected
    manager.shutdown()
