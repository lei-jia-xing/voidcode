"""LSP managed-consumer smoke on the real kernel + plugin host + loopback provider.

End-to-end proofs the unit suite cannot give: the read+execute lease fix lets a
yolo `lsp` call spawn a real fixture server; the `workspace.document_version`
query drives genuine `apply_workspace_edit` match/stale/None guards through the
kernel; a second-use invocation cancelled mid-wait kills the actual server plus
its TERM-ignoring descendant; hang-timeout stops the server; graceful close and
PID-only host SIGKILL leave no service or descendant behind; read_only refuses
the tool while the inventory command stays passive.
"""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ToolFailure, ToolSuccess

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lsp_echo_server.py"
_CREATED_KERNELS: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_created_kernels() -> Iterator[None]:
    yield
    while _CREATED_KERNELS:
        _CREATED_KERNELS.pop().close()


@contextmanager
def _loopback_server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _kernel(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    record: Path,
    *,
    read_only: bool = False,
    approval_mode: str = "yolo",
    grandchild: str | None = None,
    hang_after: int | None = None,
    files_enabled: bool = True,
) -> tuple[Kernel, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    command: list[str] = [sys.executable, str(_FIXTURE), "--record", str(record)]
    if grandchild:
        command += ["--grandchild", grandchild]
    if hang_after is not None:
        command += ["--hang-after", str(hang_after)]
    config = {
        "schema_version": 1,
        "model": {"model": "endpoint/test-model"},
        "policy": {"approval_mode": approval_mode, "read_only": read_only},
        "providers": {
            "endpoint": {
                "api_key": "loopback-test-key",
                "base_url": f"http://127.0.0.1:{server.server_port}/v1",
            }
        },
        "plugins": {
            "files": {"enabled": files_enabled},
            "lsp": {
                "config": {
                    "servers": {
                        "echo": {
                            "command": command,
                            "extensions": [".txt"],
                            "languages": ["plaintext"],
                        }
                    }
                }
            },
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    kernel = open_kernel(workspace, state_dir=state_dir)
    _CREATED_KERNELS.append(kernel)
    return kernel, workspace


def _completed(kernel: Kernel, session_id: str) -> list[object]:
    return [decode_tool_result(event.payload["result"]) for event in kernel.store.events(session_id) if event.event_type == "kernel.tool_completed"]


def _record_methods(record: Path) -> list[dict[str, object]]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pid_gone(pid: int, *, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        stat = Path(f"/proc/{pid}/stat")
        if not stat.exists():
            return True
        try:
            if stat.read_text(encoding="utf-8").split()[2] == "Z":
                return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


def _wait_file(path: Path, *, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        time.sleep(0.02)
    raise AssertionError(f"{path} was never written")


class _TestAbort:
    """Local abort handle satisfying the provider protocol for direct host.invoke calls."""

    def __init__(self) -> None:
        self.cancelled = False
        self.reason: str | None = None

    def set_cancelled(self, value: bool, *, reason: str | None = None) -> None:
        self.cancelled = value
        self.reason = reason if value else None


def test_yolo_lsp_spawns_fixture_and_opens_document(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="smoke done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record)
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
        session_id = kernel.create_session().session_id

        result = kernel.run("check hover", session_id=session_id, max_steps=8)
        assert result.status == "completed", result.error

        (completed,) = _completed(kernel, session_id)
        assert isinstance(completed, ToolSuccess), completed
        body = dict(completed.body.structured_content) if completed.body is not None else {}
        assert body["operation"] == "textDocument/hover"
        assert body["position_encoding"] == "utf-16"
        assert body["document_state"] is not None
        assert body["document_state"]["version"] == 1
        assert body["document_state"]["content_hash"] == hashlib.sha256(b"hello\n").hexdigest()
        methods = [entry["method"] for entry in _record_methods(record)]
        assert methods[:2] == ["initialize", "initialized"]
        assert "textDocument/didOpen" in methods


def test_apply_workspace_edit_version_match_and_stale_refusal(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    content = "hello\n"
    content_hash = hashlib.sha256(content.encode()).hexdigest()
    replies = [
        _turn(_call("call-read-1", "read", path="note.txt")),
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(
            _call(
                "call-edit-1",
                "apply_workspace_edit",
                edits=[
                    {
                        "path": "note.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "bye",
                        "expectedHash": content_hash,
                        "expectedVersion": 1,
                    }
                ],
            ),
            output=None,
        ),
        _turn(_call("call-read-2", "read", path="note.txt")),
        _turn(
            _call(
                "call-edit-2",
                "apply_workspace_edit",
                edits=[
                    {
                        "path": "note.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 4,
                        "newText": "zzz",
                        "expectedHash": hashlib.sha256(b"bye\n").hexdigest(),
                        "expectedVersion": 99,
                    }
                ],
            ),
            output=None,
        ),
        _turn(output="edit smoke done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record)
        (workspace / "note.txt").write_text(content, encoding="utf-8")
        session_id = kernel.create_session().session_id
        result = kernel.run("hover then edit twice", session_id=session_id, max_steps=12)
        assert result.status == "completed", result.error

        completed = _completed(kernel, session_id)
        assert len(completed) == 5, [(type(item).__name__, getattr(item, "error", None)) for item in completed]
        assert isinstance(completed[0], ToolSuccess), completed[0]
        assert isinstance(completed[1], ToolSuccess), completed[1]
        assert isinstance(completed[2], ToolSuccess), completed[2]
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "bye\n"
        assert isinstance(completed[3], ToolSuccess), completed[3]
        assert isinstance(completed[4], ToolFailure), completed[4]
        assert "not requested version 99" in completed[4].error or "version" in completed[4].error.lower()


def test_second_use_cancel_kills_server_and_term_ignoring_descendant(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild_cmd = (
        f'{sys.executable} -c "import os,pathlib,signal,time;'
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f'pathlib.Path({str(grandchild_pid)!r}).write_text(str(os.getpid()));time.sleep(120)"'
    )
    # hang-after 2: initialize + first hover answered, second hover blocks mid-wait.
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="first done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record, grandchild=grandchild_cmd, hang_after=2)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = kernel.create_session().session_id
        assert kernel.run("first hover", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        first_pids = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        assert first_pids, "fixture never spawned the grandchild on first use"
        server_pid = int(first_pids[0]["pid"])

        # Direct second invocation reuses the live server and blocks; the host cancel
        # RPC (the same path _wait_for_future takes) must stop server + descendant.
        from uuid import uuid4

        from voidcode.kernel.host import InvocationCancelled
        from voidcode.sdk.invocation import ToolContext
        from voidcode.sdk.tools import ToolCall

        host = kernel.host
        abort = _TestAbort()
        context = ToolContext(
            workspace=workspace,
            session_id=session_id,
            run_id="run-cancel",
            invocation_id=uuid4().hex,
            revision_id=kernel.command_catalog().revision_id,
            actor="model",
            capabilities=frozenset({"workspace.read"}),
            abort_signal=abort,  # type: ignore[arg-type]
        )
        call = ToolCall("lsp", {"operation": "hover", "path": "note.txt", "line": 1, "character": 1})
        outcome: dict[str, object] = {}

        def _second_use() -> None:
            try:
                outcome["result"] = host.invoke(call, context=context, timeout_seconds=60)
            except BaseException as exc:  # noqa: BLE001 - the cancel path is the assertion
                outcome["error"] = exc

        worker = Thread(target=_second_use, daemon=True)
        worker.start()
        time.sleep(2.0)  # let the second hover reach the blocked read
        abort.set_cancelled(True, reason="smoke cancel")
        worker.join(timeout=30)
        assert not worker.is_alive(), "second use never returned after cancel"
        assert isinstance(outcome.get("error"), InvocationCancelled), outcome
        assert _pid_gone(server_pid), f"server pid {server_pid} survived second-use cancel"
        assert _pid_gone(descendant), f"descendant pid {descendant} survived second-use cancel"


def test_second_use_timeout_stops_hung_server(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild_cmd = (
        f'{sys.executable} -c "import os,pathlib,signal,time;'
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f'pathlib.Path({str(grandchild_pid)!r}).write_text(str(os.getpid()));time.sleep(120)"'
    )
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="first done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record, hang_after=2, grandchild=grandchild_cmd)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = kernel.create_session().session_id
        assert kernel.run("first hover", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        markers = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        assert markers, "fixture never spawned the grandchild on first use"
        server_pid = int(markers[0]["pid"])
        from uuid import uuid4

        from voidcode.kernel.host import HostTimeout
        from voidcode.sdk.invocation import ToolContext
        from voidcode.sdk.tools import ToolCall

        host = kernel.host
        context = ToolContext(
            workspace=workspace,
            session_id=session_id,
            run_id="run-timeout",
            invocation_id=uuid4().hex,
            revision_id=kernel.command_catalog().revision_id,
            actor="model",
            capabilities=frozenset({"workspace.read"}),
        )
        call = ToolCall("lsp", {"operation": "hover", "path": "note.txt", "line": 1, "character": 1})
        started = time.monotonic()
        with pytest.raises(HostTimeout):
            host.invoke(call, context=context, timeout_seconds=3)
        assert time.monotonic() - started < 30, "timeout must come from the host window, not the LSP read"
        # Proven BEFORE kernel.close: the timeout killed the hung server + descendant.
        assert _pid_gone(server_pid, timeout=20), f"server pid {server_pid} survived second-use timeout"
        assert _pid_gone(descendant, timeout=20), f"descendant pid {descendant} survived second-use timeout"


def test_ask_parks_before_spawn_and_allow_spawns_successfully(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="ask done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record, approval_mode="ask")
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
        session_id = kernel.create_session().session_id

        parked = kernel.run("hover with approval", session_id=session_id, max_steps=8)
        assert parked.status == "waiting", parked
        assert parked.pending_approval is not None
        assert not record.exists(), "parked run must not spawn the server before approval"

        request_id = str(parked.pending_approval["request_id"])
        resumed = kernel.resume(session_id, approval_request_id=request_id, decision="allow", max_steps=8)
        assert resumed.status == "completed", resumed.error
        (completed,) = _completed(kernel, session_id)
        assert isinstance(completed, ToolSuccess), completed
        methods = [entry["method"] for entry in _record_methods(record)]
        assert methods[:2] == ["initialize", "initialized"]
        assert "textDocument/didOpen" in methods


def test_read_only_refuses_tool_but_inventory_stays_passive(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="ro done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record, read_only=True)
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
        session_id = kernel.create_session().session_id

        result = kernel.run("try hover", session_id=session_id, max_steps=8)
        assert result.status == "completed", result.error
        (completed,) = _completed(kernel, session_id)
        assert isinstance(completed, ToolFailure), completed
        assert "read-only" in completed.error.lower() or "read_only" in completed.error.lower()
        assert not record.exists(), "read-only run must not spawn the server binary"

        inventory = kernel.invoke_command(session_id, "lsp", {}, expected_revision=kernel.command_catalog().revision_id)
        assert inventory.status == "ok"
        assert inventory.data is not None and inventory.data["servers"][0]["name"] == "echo"
        assert not record.exists(), "inventory must never spawn a server"


def test_live_graceful_close_kills_server_and_term_ignoring_descendant(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild_cmd = (
        f'{sys.executable} -c "import os,pathlib,signal,time;'
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f'pathlib.Path({str(grandchild_pid)!r}).write_text(str(os.getpid()));time.sleep(120)"'
    )
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="close done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record, grandchild=grandchild_cmd)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = kernel.create_session().session_id
        assert kernel.run("hover once", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        markers = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        server_pid = int(markers[0]["pid"])
        assert not _pid_gone(server_pid, timeout=0.1), "server must be alive before close"
        assert not _pid_gone(descendant, timeout=0.1), "descendant must be alive before close"

        # Live graceful close: the kernel stops the owned tree without any kill -9.
        _CREATED_KERNELS.remove(kernel)
        kernel.close()
        assert _pid_gone(server_pid, timeout=20), f"server pid {server_pid} survived graceful close"
        assert _pid_gone(descendant, timeout=20), f"descendant pid {descendant} survived graceful close"


def test_reload_retires_old_host_and_its_service_tree(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild_cmd = (
        f'{sys.executable} -c "import os,pathlib,signal,time;'
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f'pathlib.Path({str(grandchild_pid)!r}).write_text(str(os.getpid()));time.sleep(120)"'
    )
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(
            _call(
                "call-bg-1",
                "background_process",
                op="start",
                command=(
                    f'{sys.executable} -c "import os,pathlib,time;'
                    f"pathlib.Path({str(tmp_path / 'bg.pid')!r}).write_text(str(os.getpid()));"
                    "print('bg-ready', flush=True);time.sleep(120)\""
                ),
                ready_pattern="bg-ready",
            )
        ),
        _turn(output="reload done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record, grandchild=grandchild_cmd)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = kernel.create_session().session_id
        assert kernel.run("hover once", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        bg_pid = int(_wait_file(tmp_path / "bg.pid"))
        markers = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        server_pid = int(markers[0]["pid"])
        old_host_pid = kernel.host.pid

        # A no-op reload refuses; change the workspace config so the swap is real.
        config_path = workspace / "voidcode.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["policy"]["approval_mode"] = "ask"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        reloaded = kernel.invoke_command(session_id, "kernel.reload", {}, expected_revision=kernel.command_catalog().revision_id)
        assert reloaded.status == "ok", reloaded
        assert kernel.host.pid != old_host_pid, "reload must swap the host process"
        assert _pid_gone(server_pid, timeout=20), f"server pid {server_pid} survived reload"
        assert _pid_gone(descendant, timeout=20), f"descendant pid {descendant} survived reload"
        assert _pid_gone(old_host_pid, timeout=20), f"old host pid {old_host_pid} survived reload"
        assert not _pid_gone(bg_pid, timeout=0.1), f"detached background job {bg_pid} must survive reload"


def test_pid_kill_of_live_host_leaves_no_service_behind(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"

    grandchild_pid = tmp_path / "grandchild.pid"
    grandchild_cmd = (
        f'{sys.executable} -c "import os,pathlib,signal,time;'
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f'pathlib.Path({str(grandchild_pid)!r}).write_text(str(os.getpid()));time.sleep(120)"'
    )
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="close done"),
    ]
    with _loopback_server(replies) as server:
        kernel, workspace = _kernel(tmp_path, server, record, grandchild=grandchild_cmd)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = kernel.create_session().session_id
        assert kernel.run("hover once", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        markers = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        server_pid = int(markers[0]["pid"])

        # PID-only host kill: the kernel's tracked-pgid sweep must take the group.
        host_pid = kernel.host.pid
        assert isinstance(host_pid, int) and host_pid > 1
        os.kill(host_pid, signal.SIGKILL)
        assert _pid_gone(host_pid), "host pid survived its own SIGKILL"
        assert _pid_gone(server_pid, timeout=20), f"server pid {server_pid} survived host SIGKILL"
        assert _pid_gone(descendant, timeout=20), f"descendant pid {descendant} survived host SIGKILL"
