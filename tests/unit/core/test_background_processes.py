"""S5 background-process acceptance: supervised processes outlive their invocation, not their owner.

`run_process` dies with the lease; a background process is supervised instead: it keeps running
after the tool call returns, its logs are real files the caller named, another session can neither
read nor stop it, a retried start cannot create a second process, and the core's own shutdown
ends it by group. One core owns a workspace, so each scenario drives several runs (and sessions)
of the same core — which is also what keeps a supervised process alive to be inspected.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.core_factory import loopback_server, open_core_for_test, write_loopback_config
from tests.fixtures.reload_plugin import SERVICES_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "background reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _payload(server: OpenAILoopbackServer, request_index: int) -> dict[str, object]:
    """The last tool result the model of that dispatch read."""
    messages = server.requests[request_index].get("messages")
    assert isinstance(messages, list)
    payloads: list[dict[str, object]] = []
    for message in messages:
        if isinstance(message, Mapping) and message.get("role") == "tool":
            envelope = json.loads(str(message.get("content", "")))
            payloads.append(json.loads(envelope["text"]))
    assert payloads, f"dispatch {request_index} carried no tool result"
    return payloads[-1]


def _tool_text(server: OpenAILoopbackServer, request_index: int) -> str:
    messages = server.requests[request_index].get("messages")
    assert isinstance(messages, list)
    return "\n".join(str(message.get("content", "")) for message in messages if isinstance(message, Mapping) and message.get("role") == "tool")


def _python(*lines: str) -> list[str]:
    return [sys.executable, "-c", "\n".join(lines)]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_gone(pid: int, *, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid) or not Path(f"/proc/{pid}").exists():
            return True
        state = Path(f"/proc/{pid}/stat")
        if state.exists() and state.read_text(encoding="utf-8").split()[2] == "Z":
            return True
        time.sleep(0.05)
    return False


def _wait_content(path: Path, needle: str, *, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        if needle in text:
            return text
        time.sleep(0.05)
    raise AssertionError(f"{path} never contained {needle!r}")


def _wait_file(path: Path, *, timeout: float = 10.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        time.sleep(0.05)
    raise AssertionError(f"{path} was never written")


def test_a_supervised_process_outlives_the_call_and_lists_itself(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(
            _call(
                "bg-1",
                SERVICES_TOOL,
                action="bg_spawn",
                argv=_python("import time", "print('daemon up', flush=True)", "time.sleep(120)"),
                stdout_suffix="daemon",
            )
        ),
        _turn(output="done"),
        _turn(_call("bg-2", SERVICES_TOOL, action="bg_list")),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        write_loopback_config(tmp_path / "workspace", server, source, plugin_id=PLUGIN_ID)
        core = open_core_for_test(tmp_path)
        core.create_session("sess-bg")
        assert core.run("start it", session_id="sess-bg").status == "completed"

        handle = _payload(server, 1)["handle"]
        assert handle["running"] is True
        assert handle["pid"] > 0
        assert handle["start_time"] != ""

        # The call has returned; the process is still alive and its log is a real file.
        _wait_content(Path(handle["stdout_path"]), "daemon up")
        assert _pid_alive(handle["pid"])

        assert core.run("list them", session_id="sess-bg").status == "completed"
        listed = _payload(server, 3)["handles"]
        assert [entry["process_id"] for entry in listed] == [handle["process_id"]]
        assert listed[0]["running"] is True


def test_pages_read_the_log_by_offset_and_report_liveness(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(
            _call(
                "bg-1",
                SERVICES_TOOL,
                action="bg_spawn",
                argv=_python("import time", "print('0123456789', flush=True)", "time.sleep(120)"),
                stdout_suffix="pages",
            )
        ),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        write_loopback_config(tmp_path / "workspace", server, source, plugin_id=PLUGIN_ID)
        core = open_core_for_test(tmp_path)
        core.create_session("sess-bg-pages")
        assert core.run("start it", session_id="sess-bg-pages").status == "completed"
        handle = _payload(server, 1)["handle"]
        _wait_content(Path(handle["stdout_path"]), "0123456789")

        # A later run of the same core reads the log by the identity it persisted, page by page.
        server.replies.extend(
            [
                _turn(_call("p1", SERVICES_TOOL, action="bg_output", process_id=handle["process_id"], offset=0, limit=4)),
                _turn(_call("p2", SERVICES_TOOL, action="bg_output", process_id=handle["process_id"], offset=4, limit=64)),
                _turn(output="done"),
            ]
        )
        assert core.run("read it", session_id="sess-bg-pages").status == "completed"
        first = _payload(server, -2)
        second = _payload(server, -1)
        assert (first["data"], first["offset"], first["eof"], first["running"]) == ("0123", 0, False, True)
        assert second["data"] == "456789\n"
        assert second["eof"] is True
        assert second["running"] is True


def test_written_stdin_reaches_the_process(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(
            _call(
                "bg-1",
                SERVICES_TOOL,
                action="bg_spawn",
                argv=_python(
                    "import sys, time",
                    "line = sys.stdin.readline()",
                    "print('echo:' + line.strip(), flush=True)",
                    "time.sleep(120)",
                ),
                stdin=True,
                stdout_suffix="echo",
                process_id="echo-service",
            )
        ),
        _turn(output="done"),
        _turn(_call("w1", SERVICES_TOOL, action="bg_write", process_id="echo-service", text="hello\n")),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        write_loopback_config(tmp_path / "workspace", server, source, plugin_id=PLUGIN_ID)
        core = open_core_for_test(tmp_path)
        core.create_session("sess-bg-stdin")
        assert core.run("start it", session_id="sess-bg-stdin").status == "completed"
        handle = _payload(server, 1)["handle"]
        assert handle["process_id"] == "echo-service"

        assert core.run("write to it", session_id="sess-bg-stdin").status == "completed"
        assert _payload(server, 3)["written"] == len("hello\n")
        _wait_content(Path(handle["stdout_path"]), "echo:hello")


def test_a_live_id_cannot_be_started_twice_and_cancel_kills_the_group(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(
            _call(
                "bg-1",
                SERVICES_TOOL,
                action="bg_spawn",
                argv=_python("import time", "time.sleep(120)"),
                stdout_suffix="cancel",
                process_id="fixed-id",
            )
        ),
        _turn(output="done"),
        _turn(_call("bg-2", SERVICES_TOOL, action="bg_spawn", argv=_python("print('again')"), process_id="fixed-id", stdout_suffix="cancel")),
        _turn(output="done"),
        _turn(_call("c1", SERVICES_TOOL, action="bg_cancel", process_id="fixed-id")),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        write_loopback_config(tmp_path / "workspace", server, source, plugin_id=PLUGIN_ID)
        core = open_core_for_test(tmp_path)
        core.create_session("sess-bg-cancel")
        assert core.run("start it", session_id="sess-bg-cancel").status == "completed"
        handle = _payload(server, 1)["handle"]
        assert handle["process_id"] == "fixed-id"
        assert _pid_alive(handle["pid"])

        # A retried start must never silently create a second process.
        assert core.run("start it again", session_id="sess-bg-cancel").status == "completed"
        assert _payload(server, 3)["error"].startswith("ValueError: background process fixed-id is already running")

        assert core.run("stop it", session_id="sess-bg-cancel").status == "completed"
        assert _payload(server, 5)["stopped"] is True
        assert _wait_gone(handle["pid"])


def test_another_session_cannot_see_or_stop_a_supervised_process(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(
            _call(
                "bg-1",
                SERVICES_TOOL,
                action="bg_spawn",
                argv=_python("import time", "print('private', flush=True)", "time.sleep(120)"),
                stdout_suffix="private",
                process_id="session-bound",
            )
        ),
        _turn(output="done"),
        _turn(_call("l1", SERVICES_TOOL, action="bg_list")),
        _turn(_call("o1", SERVICES_TOOL, action="bg_output", process_id="session-bound", offset=0, limit=64)),
        _turn(_call("c1", SERVICES_TOOL, action="bg_cancel", process_id="session-bound")),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        write_loopback_config(tmp_path / "workspace", server, source, plugin_id=PLUGIN_ID)
        core = open_core_for_test(tmp_path)
        core.create_session("sess-owner")
        core.create_session("sess-intruder")
        assert core.run("start it", session_id="sess-owner").status == "completed"
        handle = _payload(server, 1)["handle"]

        assert core.run("poke it", session_id="sess-intruder").status == "completed"
        assert _payload(server, -3)["handles"] == []
        assert _payload(server, -2)["error"].startswith("ValueError:")
        assert _payload(server, -1)["error"].startswith("ValueError:")
        assert _pid_alive(handle["pid"])


def test_core_shutdown_stops_supervised_processes_by_group(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    leader_file = workspace / "leader.pid"
    grandchild_file = workspace / "grandchild.pid"
    grandchild_script = f"import os, pathlib, time;pathlib.Path({str(grandchild_file)!r}).write_text(str(os.getpid()));time.sleep(120)"
    replies = [
        _turn(
            _call(
                "bg-1",
                SERVICES_TOOL,
                action="bg_spawn",
                argv=_python(
                    "import os, subprocess, sys, time",
                    f"subprocess.Popen([sys.executable, '-c', {grandchild_script!r}])",
                    f"from pathlib import Path; Path({str(leader_file)!r}).write_text(str(os.getpid()))",
                    "time.sleep(120)",
                ),
                stdout_suffix="shutdown",
            )
        ),
        _turn(output="done"),
    ]
    with loopback_server(replies) as server:
        write_loopback_config(workspace, server, source, plugin_id=PLUGIN_ID)
        core = open_core_for_test(tmp_path)
        core.create_session("sess-bg-shutdown")
        assert core.run("start it", session_id="sess-bg-shutdown").status == "completed"
        assert _pid_alive(int(_payload(server, 1)["handle"]["pid"]))

        leader = int(_wait_file(leader_file))
        grandchild = int(_wait_file(grandchild_file))

        core.close()

        # The owner going away stops the whole group, not just the supervised leader.
        assert _wait_gone(leader)
        assert _wait_gone(grandchild)
