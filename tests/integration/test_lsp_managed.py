"""LSP managed-consumer smoke on the real core + loopback provider.

End-to-end proofs the unit suite cannot give: the read+execute lease fix lets a
yolo `lsp` call spawn a real fixture server; the `workspace.document_version`
query drives genuine `edit` range-mode match/stale/None guards through the
core; a session cancel during a blocked second use kills the actual server plus
its TERM-ignoring descendant; graceful close and reload leave no service or
descendant behind (detached background jobs survive); read_only refuses
the tool while the inventory command stays passive.
"""

from __future__ import annotations

import hashlib
import json
import shlex
import sys
import time
from pathlib import Path
from threading import Event, Thread
from types import ModuleType

import pytest

from tests.fixtures.core_factory import discard_core, loopback_server, register_core
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.json_values import json_wire_object
from voidcode.sdk.tools import TextOutput, ToolFailure, ToolSuccess

_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "lsp_echo_server.py"


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
    external_plugins: dict[str, dict[str, object]] | None = None,
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
            **(external_plugins or {}),
        },
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    core = open_core(workspace, state_dir=state_dir)
    register_core(core)
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
    with loopback_server(replies) as server:
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


def test_edit_range_version_match_and_stale_refusal(tmp_path: Path) -> None:
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
                "edit",
                mode="range",
                edits=[
                    {
                        "path": "note.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 6,
                        "newText": "bye",
                        "expectedHash": content_hash,
                        "version": 1,
                    }
                ],
            ),
            output=None,
        ),
        _turn(_call("call-read-2", "read", path="note.txt")),
        _turn(
            _call(
                "call-edit-2",
                "edit",
                mode="range",
                edits=[
                    {
                        "path": "note.txt",
                        "startLine": 1,
                        "startCharacter": 1,
                        "endLine": 1,
                        "endCharacter": 4,
                        "newText": "zzz",
                        "expectedHash": hashlib.sha256(b"bye\n").hexdigest(),
                        "version": 99,
                    }
                ],
            ),
            output=None,
        ),
        _turn(output="edit smoke done"),
    ]
    with loopback_server(replies) as server:
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
    with loopback_server(replies) as server:
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
    with loopback_server(replies) as server:
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
    with loopback_server(replies) as server:
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
    with loopback_server(replies) as server:
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
        discard_core(core)
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
    with loopback_server(replies) as server:
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


def test_exec_proc_resources_are_scoped_and_preserve_process_bytes(tmp_path: Path) -> None:
    record = tmp_path / "lsp-record.jsonl"
    process_script = tmp_path / "resource-child.py"
    process_script.write_text(
        "import os, sys\n"
        "os.write(1, b'abc\\xc3\\xa9Z\\nREADY\\n')\n"
        "os.write(2, b'ERR\\xff\\n')\n"
        "sys.stdin.buffer.readline()\n"
        "os.write(1, b'tail\\n')\n"
        "os.write(2, b'err-tail\\n')\n",
        encoding="utf-8",
    )
    with loopback_server([]) as server:
        core, workspace = _core(tmp_path, server, record)
        (workspace / "note.txt").write_text("hello\n", encoding="utf-8")

        def run_tool(current_core: Core, session_id: str, name: str, **arguments: object) -> object:
            call_id = f"call-{name}-{len(server.requests)}"
            server.replies.extend(
                [
                    _turn(_call(call_id, name, **arguments)),
                    _turn(output=f"{call_id} done"),
                ]
            )
            result = current_core.run(f"run {name}", session_id=session_id, max_steps=8)
            assert result.status == "completed", result.error
            return _completed(current_core, session_id)[-1]

        session_id = core.create_session().session_id
        lsp_result = run_tool(
            core,
            session_id,
            "lsp",
            operation="hover",
            path="note.txt",
            line=1,
            character=2,
        )
        assert isinstance(lsp_result, ToolSuccess)
        assert "textDocument/hover" in [entry.get("method") for entry in _record_methods(record)]

        command = shlex.join([sys.executable, str(process_script)])
        start_result = run_tool(
            core,
            session_id,
            "background_process",
            op="start",
            command=command,
            ready_pattern="READY",
            ready_timeout=5,
        )
        assert isinstance(start_result, ToolSuccess)
        assert start_result.body is not None
        start_body = dict(start_result.body.structured_content)
        process_id = str(start_body["process_id"])
        resource_uri = str(start_body["resource_uri"])
        pid = int(start_body["pid"])
        assert resource_uri == f"proc://{process_id}"

        inventory_result = run_tool(core, session_id, "read", path="proc://")
        assert isinstance(inventory_result, ToolSuccess)
        inventory = dict(inventory_result.body.structured_content)
        (row,) = inventory["processes"]
        assert row["process_id"] == process_id
        assert row["running"] is True
        assert "pid" not in row and "command" not in row

        first_page_result = run_tool(
            core,
            session_id,
            "read",
            path=resource_uri,
            byte_offset=0,
            byte_limit=4,
        )
        assert isinstance(first_page_result, ToolSuccess)
        first_page = dict(first_page_result.body.structured_content)
        assert first_page["running"] is True
        assert first_page["page_eof"] is False
        assert first_page["raw_content"] == "abc"
        assert first_page["pending_byte_offset"] == 3
        assert first_page["next_byte_offset"] == 3

        second_page_result = run_tool(
            core,
            session_id,
            "read",
            path=resource_uri,
            byte_offset=3,
            byte_limit=4,
        )
        assert isinstance(second_page_result, ToolSuccess)
        second_page = dict(second_page_result.body.structured_content)
        assert second_page["raw_content"] == "éZ\n"
        assert second_page["offset"] == 3

        stderr_result = run_tool(
            core,
            session_id,
            "read",
            path=f"{resource_uri}/stderr",
            byte_offset=0,
            byte_limit=4,
        )
        assert isinstance(stderr_result, ToolSuccess)
        stderr_page = dict(stderr_result.body.structured_content)
        assert stderr_page["running"] is True
        assert stderr_page["encoding"] == "hex"
        assert stderr_page["hex_content"] == "455252ff"
        assert stderr_page["next_byte_offset"] == 4

        initial_stdout = b"abc\xc3\xa9Z\nREADY\n"
        at_end_result = run_tool(
            core,
            session_id,
            "read",
            path=resource_uri,
            byte_offset=len(initial_stdout),
            byte_limit=4,
        )
        assert isinstance(at_end_result, ToolSuccess)
        at_end = dict(at_end_result.body.structured_content)
        assert at_end["running"] is True
        assert at_end["page_eof"] is True
        assert at_end["next_byte_offset"] is None
        assert at_end["current_end_byte_offset"] == len(initial_stdout)

        bad_alias = run_tool(
            core,
            session_id,
            "read",
            path=f"{resource_uri}/stdout",
            byte_offset=0,
            byte_limit=4,
        )
        assert isinstance(bad_alias, ToolFailure)

        foreign_session_id = core.create_session().session_id
        foreign_read = run_tool(core, foreign_session_id, "read", path=resource_uri)
        assert isinstance(foreign_read, ToolFailure)
        assert "no such resource" in foreign_read.error.lower()

        send_result = run_tool(
            core,
            session_id,
            "background_process",
            op="send",
            process_id=process_id,
            input="finish",
        )
        assert isinstance(send_result, ToolSuccess)
        assert _pid_gone(pid, timeout=15), "the real background process did not exit after stdin"

        final_stdout_result = run_tool(
            core,
            session_id,
            "read",
            path=resource_uri,
            byte_offset=0,
            byte_limit=128,
        )
        assert isinstance(final_stdout_result, ToolSuccess)
        final_stdout = dict(final_stdout_result.body.structured_content)
        assert final_stdout["running"] is False
        assert final_stdout["exit_code"] == 0
        assert final_stdout["page_eof"] is True
        assert final_stdout["raw_content"] == "abcéZ\nREADY\ntail\n"

        final_stderr_result = run_tool(
            core,
            session_id,
            "read",
            path=f"{resource_uri}/stderr",
            byte_offset=0,
            byte_limit=128,
        )
        assert isinstance(final_stderr_result, ToolSuccess)
        final_stderr = dict(final_stderr_result.body.structured_content)
        assert final_stderr["running"] is False
        assert final_stderr["encoding"] == "hex"
        assert bytes.fromhex(str(final_stderr["hex_content"])) == b"ERR\xff\nerr-tail\n"

        resource_bodies = (inventory, first_page, second_page, stderr_page, at_end, final_stdout, final_stderr)
        for body in resource_bodies:
            encoded = json.dumps(json_wire_object(body), ensure_ascii=False)
            assert str(tmp_path) not in encoded
            assert command not in encoded
            assert "stdout_path" not in encoded and "stderr_path" not in encoded

        discard_core(core)
        core.close()
        core = open_core(workspace, state_dir=tmp_path / "state")
        register_core(core)
        stale_result = run_tool(core, session_id, "read", path=resource_uri)
        assert isinstance(stale_result, ToolSuccess)
        stale = dict(stale_result.body.structured_content)
        assert stale["status"] == "unknown_not_supervised"
        assert stale["running"] is None
        assert stale["logs_available"] is False
        assert "raw_content" not in stale
        assert "logs are unavailable" in stale_result.output.text.lower()

        config_path = workspace / "voidcode.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["plugins"]["exec"] = {"enabled": False}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        activation = core.activate(session_id=session_id, source="client")
        assert activation["activated"] is True
        disabled_read = run_tool(core, session_id, "read", path=resource_uri)
        assert isinstance(disabled_read, ToolFailure)
        assert "unsupported resource uri" in disabled_read.error.lower()


def test_cancelling_inflight_resource_log_read_preserves_its_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate_name = "voidcode_test_resource_read_cancel_gate"
    gate = ModuleType(gate_name)
    gate.read_entered = Event()
    gate.read_page_ready = Event()
    gate.release_read = Event()
    monkeypatch.setitem(sys.modules, gate_name, gate)

    plugin_path = Path(__file__).resolve().parents[1] / "fixtures" / "resource_read_cancel_plugin"
    with loopback_server([]) as server:
        core, workspace = _core(
            tmp_path,
            server,
            tmp_path / "unused-lsp-record.jsonl",
            external_plugins={"resource-read-fixture": {"path": str(plugin_path)}},
        )
        session_id = core.create_session().session_id

        def run_tool(name: str, **arguments: object) -> object:
            call_id = f"call-{name}-{len(server.requests)}"
            server.replies.extend(
                [
                    _turn(_call(call_id, name, **arguments)),
                    _turn(output=f"{call_id} done"),
                ]
            )
            result = core.run(f"run {name}", session_id=session_id, max_steps=8)
            assert result.status == "completed", result.error
            return _completed(core, session_id)[-1]

        started = run_tool("resource_service_start")
        assert isinstance(started, ToolSuccess), started
        assert isinstance(started.output, TextOutput), started.output
        start_payload = json.loads(started.output.text)
        process_id = str(start_payload["process_id"])
        pid = int(start_payload["pid"])

        read_call_id = f"call-read-service-{len(server.requests)}"
        server.replies.extend(
            [
                _turn(_call(read_call_id, "read", path=f"safetyproc://{process_id}", byte_offset=0, byte_limit=4)),
                _turn(output="resource read completed"),
            ]
        )
        outcome: dict[str, object] = {}

        def _read_service_log() -> None:
            outcome["result"] = core.run("read the managed service log", session_id=session_id, max_steps=8)

        worker = Thread(target=_read_service_log, daemon=True)
        worker.start()
        try:
            assert gate.read_entered.wait(timeout=20), "resource reader never entered"
            assert gate.read_page_ready.wait(timeout=20), "real registry process-log read never completed"
            assert core.cancel_session(session_id, reason="cancel in-flight resource process read") is True
            assert not _pid_gone(pid, timeout=0.5), f"resource-read cancellation stopped managed service {pid}"
        finally:
            if worker.is_alive():
                core.cancel_session(session_id, reason="resource-read test cleanup")
            gate.release_read.set()
            worker.join(timeout=30)

        assert not worker.is_alive(), "cancelled resource read did not stop"
        result = outcome.get("result")
        assert result is not None and result.status == "interrupted", result
        assert core.session(session_id).status == "interrupted"

        finished = core.invoke_command(
            session_id,
            "resource_service_finish",
            {"process_id": process_id},
            expected_revision=core.command_catalog().revision_id,
        )
        assert finished.status == "ok", finished.error
        assert finished.data is not None
        assert finished.data["hover_response"] is True
        assert finished.data["shutdown_response"] is True
        assert finished.data["running"] is False
        assert finished.data["exit_code"] == 0
        assert _pid_gone(pid, timeout=10), f"managed service {pid} did not exit normally"

        methods = [entry.get("method") for entry in _record_methods(workspace / "resource-reader-lsp.jsonl")]
        assert "textDocument/hover" in methods
        assert "shutdown" in methods
        assert "exit" in methods
