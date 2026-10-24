"""LSP managed-consumer smoke on the real core + loopback provider.

End-to-end proofs the unit suite cannot give: the read+execute lease fix lets a
yolo `lsp` call spawn a real fixture server; the `workspace.document_version`
query drives genuine `apply_workspace_edit` match/stale/None guards through the
core; a session cancel during a blocked second use kills the actual server plus
its TERM-ignoring descendant; graceful close and reload leave no service or
descendant behind (detached background jobs survive); read_only refuses
the tool while the inventory command stays passive.
"""

from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ToolFailure, ToolSuccess

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lsp_echo_server.py"
_CREATED_CORES: list[Core] = []


@pytest.fixture(autouse=True)
def _close_created_cores() -> Iterator[None]:
    yield
    while _CREATED_CORES:
        _CREATED_CORES.pop().close()


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


def _core(
    tmp_path: Path,
    server: OpenAILoopbackServer,
    record: Path,
    *,
    read_only: bool = False,
    approval_mode: str = "yolo",
    grandchild: str | None = None,
    hang_after: int | None = None,
    files_enabled: bool = True,
) -> tuple[Core, Path]:
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
    core = open_core(workspace, state_dir=state_dir)
    _CREATED_CORES.append(core)
    return core, workspace


def _completed(core: Core, session_id: str) -> list[object]:
    return [decode_tool_result(event.payload["result"]) for event in core.store.events(session_id) if event.event_type == "core.tool_completed"]


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


def _wait_second_hover_started(record: Path, *, timeout: float = 30.0) -> bool:
    """Wait until the fixture records the hung second hover plus its barrier.

    The fixture answers `hang_after` requests, then records the next real
    request and `__hanging__` before sleeping: both lines prove hover #2 is
    genuinely in flight and the client genuinely blocked awaiting its response.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        methods = [entry.get("method") for entry in _record_methods(record)]
        if "__hanging__" in methods and methods.count("textDocument/hover") >= 2:
            return True
        time.sleep(0.05)
    return False


def test_yolo_lsp_spawns_fixture_and_opens_document(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="smoke done"),
    ]
    with _loopback_server(replies) as server:
        core, workspace = _core(tmp_path, server, record)
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
        session_id = core.create_session().session_id

        result = core.run("check hover", session_id=session_id, max_steps=8)
        assert result.status == "completed", result.error

        (completed,) = _completed(core, session_id)
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
        core, workspace = _core(tmp_path, server, record)
        (workspace / "note.txt").write_text(content, encoding="utf-8")
        session_id = core.create_session().session_id
        result = core.run("hover then edit twice", session_id=session_id, max_steps=12)
        assert result.status == "completed", result.error

        completed = _completed(core, session_id)
        assert len(completed) == 5, [(type(item).__name__, getattr(item, "error", None)) for item in completed]
        assert isinstance(completed[0], ToolSuccess), completed[0]
        assert isinstance(completed[1], ToolSuccess), completed[1]
        assert isinstance(completed[2], ToolSuccess), completed[2]
        assert (workspace / "note.txt").read_text(encoding="utf-8") == "bye\n"
        assert isinstance(completed[3], ToolSuccess), completed[3]
        assert isinstance(completed[4], ToolFailure), completed[4]
        assert "not requested version 99" in completed[4].error or "version" in completed[4].error.lower()


def test_session_cancel_during_blocked_use_kills_server_and_term_ignoring_descendant(tmp_path: Path) -> None:
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
        _turn(_call("call-lsp-2", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="second done"),
    ]
    with _loopback_server(replies) as server:
        core, workspace = _core(tmp_path, server, record, grandchild=grandchild_cmd, hang_after=2)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = core.create_session().session_id
        assert core.run("first hover", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        first_pids = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        assert first_pids, "fixture never spawned the grandchild on first use"
        server_pid = int(first_pids[0]["pid"])

        # Second run blocks inside the hung server; cancelling the session must
        # cooperatively stop the invocation and kill the server + descendant.
        outcome: dict[str, object] = {}

        def _second_run() -> None:
            outcome["result"] = core.run("second hover", session_id=session_id, max_steps=8)

        worker = Thread(target=_second_run, daemon=True)
        worker.start()
        assert _wait_second_hover_started(record)
        assert core.cancel_session(session_id, reason="smoke cancel") is True
        worker.join(timeout=60)
        assert not worker.is_alive(), "second run never returned after cancel"
        assert _pid_gone(server_pid), f"server pid {server_pid} survived session cancel"
        assert _pid_gone(descendant), f"descendant pid {descendant} survived session cancel"


def test_ask_parks_before_spawn_and_allow_spawns_successfully(tmp_path: Path) -> None:
    record = tmp_path / "record.jsonl"
    replies = [
        _turn(_call("call-lsp-1", "lsp", operation="hover", path="note.txt", line=1, character=2)),
        _turn(output="ask done"),
    ]
    with _loopback_server(replies) as server:
        core, workspace = _core(tmp_path, server, record, approval_mode="ask")
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
        session_id = core.create_session().session_id

        parked = core.run("hover with approval", session_id=session_id, max_steps=8)
        assert parked.status == "waiting", parked
        assert parked.pending_approval is not None
        assert not record.exists(), "parked run must not spawn the server before approval"

        request_id = str(parked.pending_approval["request_id"])
        resumed = core.resume(session_id, approval_request_id=request_id, decision="allow", max_steps=8)
        assert resumed.status == "completed", resumed.error
        (completed,) = _completed(core, session_id)
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
        core, workspace = _core(tmp_path, server, record, read_only=True)
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8")
        session_id = core.create_session().session_id

        result = core.run("try hover", session_id=session_id, max_steps=8)
        assert result.status == "completed", result.error
        (completed,) = _completed(core, session_id)
        assert isinstance(completed, ToolFailure), completed
        assert "read-only" in completed.error.lower() or "read_only" in completed.error.lower()
        assert not record.exists(), "read-only run must not spawn the server binary"

        inventory = core.invoke_command(session_id, "lsp", {}, expected_revision=core.command_catalog().revision_id)
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
        core, workspace = _core(tmp_path, server, record, grandchild=grandchild_cmd)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = core.create_session().session_id
        assert core.run("hover once", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        markers = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        server_pid = int(markers[0]["pid"])
        assert not _pid_gone(server_pid, timeout=0.1), "server must be alive before close"
        assert not _pid_gone(descendant, timeout=0.1), "descendant must be alive before close"

        # Live graceful close: the core stops the owned tree without any kill -9.
        _CREATED_CORES.remove(core)
        core.close()
        assert _pid_gone(server_pid, timeout=20), f"server pid {server_pid} survived graceful close"
        assert _pid_gone(descendant, timeout=20), f"descendant pid {descendant} survived graceful close"


def test_reload_retires_old_runtime_and_its_service_tree(tmp_path: Path) -> None:
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
        core, workspace = _core(tmp_path, server, record, grandchild=grandchild_cmd)
        (workspace / "note.txt").write_text("x\n", encoding="utf-8")
        session_id = core.create_session().session_id
        assert core.run("hover once", session_id=session_id, max_steps=8).status == "completed"
        descendant = int(_wait_file(grandchild_pid))
        bg_pid = int(_wait_file(tmp_path / "bg.pid"))
        markers = [entry for entry in _record_methods(record) if entry.get("method") == "__grandchild__"]
        server_pid = int(markers[0]["pid"])
        old_runtime = core.plugin_runtime

        # A no-op reload refuses; change the workspace config so the swap is real.
        config_path = workspace / "voidcode.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["policy"]["approval_mode"] = "ask"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        reloaded = core.invoke_command(session_id, "core.reload", {}, expected_revision=core.command_catalog().revision_id)
        assert reloaded.status == "ok", reloaded
        assert core.plugin_runtime is not old_runtime, "reload must swap the plugin runtime"
        assert _pid_gone(server_pid, timeout=20), f"server pid {server_pid} survived reload"
        assert _pid_gone(descendant, timeout=20), f"descendant pid {descendant} survived reload"
        assert not _pid_gone(bg_pid, timeout=0.1), f"detached background job {bg_pid} must survive reload"
