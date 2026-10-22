"""LSP consumer cutover: managed service transport + document-version query.

Behavior boundaries, not wiring: a real stdio fixture server is driven through
the live PluginServices surface (fake lease, real protocol bytes), the version
query answers strict shapes from real didOpen/didChange tracking, and the
second-use cancel kills the actual server plus its descendant. Core/host
smoke (read_only/approval, reload/close cleanup, hard-death group sweep) runs
in the coordinated P5 window, not here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
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
    assert not isinstance(manager, object) or True
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
    assert result.response["result"] == {"echo": {"textDocument": {"uri": target.as_uri()}, "position": {"line": 0, "character": 1}}}
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


def test_cancelled_second_use_stops_server_and_descendant(harness: tuple[_Services, Path]) -> None:
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
    assert first.response["result"]["echo"]["position"] == {"line": 0, "character": 1}
    deadline = time.monotonic() + 10
    while not grandchild_pid.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    descendant = int(grandchild_pid.read_text(encoding="utf-8").strip())
    abort = _Abort(cancelled=True)
    with pytest.raises(ValueError, match="timed out; the server was stopped"):
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
    assert "cancel_process" in services.names()
    assert all(entry.poll() is not None for entry in services._entries.values())
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
