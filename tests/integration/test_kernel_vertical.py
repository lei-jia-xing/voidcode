from __future__ import annotations

import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.storage.store import SqliteStore
from voidcode.sdk.manifest import PluginCapabilityRequest, PluginFileIdentity, PluginManifest, PluginToolDeclaration
from voidcode.sdk.tools import ToolEffect


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "authentic loopback reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def _configure(workspace: Path, server: OpenAILoopbackServer, **policy: object) -> dict[str, object]:
    config = {
        "schema_version": 1,
        "model": {"model": "loopback/exact-model"},
        "policy": policy,
        "providers": {"loopback": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
    }
    (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
    return config


def _command(workspace: Path, state: Path, *arguments: str, trust: bool = False) -> list[str]:
    return [
        sys.executable,
        "-m",
        "voidcode.kernel",
        "--workspace",
        str(workspace),
        "--state-dir",
        str(state),
        *(["--trust-plugin", "counter"] if trust else []),
        *arguments,
    ]


def _cli(workspace: Path, state: Path, *arguments: str, trust: bool = False, code: int = 0) -> dict:
    result = subprocess.run(_command(workspace, state, *arguments, trust=trust), capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == code, result.stdout + result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_native_batch_prefix_survives_approval_restart(tmp_path: Path, decision: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    (workspace / "input.txt").write_text("original observation\n", encoding="utf-8")
    replies = [
        _turn(_call("read-id", "read", path="input.txt"), _call("write-id", "write", path="derived.txt", content="derived output\n")),
        _turn(_call("read-id", "read", path="input.txt")),  # Native IDs are batch-scoped, not session-global.
        _turn(output="finished after recovery"),
    ]
    with _server(replies) as server:
        config = _configure(workspace, server, approval_mode="ask")
        first = _cli(workspace, state, "run", "read and write", "--session-id", "approval-session")
        assert first["status"] == "waiting"
        assert not (workspace / "derived.txt").exists()
        pending = first["pending_approval"]
        rejected = _cli(workspace, state, "resume", first["session_id"], "--approval-request-id", "wrong-id", "--decision", "allow", code=1)
        assert "identity" in rejected["error"]
        config["policy"] = {"approval_mode": "yolo", "read_only": True}
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        changed = _cli(workspace, state, "resume", first["session_id"], "--approval-request-id", pending["request_id"], "--decision", "allow", code=1)
        assert "revision" in changed["error"]
        config["policy"] = {"approval_mode": "ask"}
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        (workspace / "input.txt").write_text("new observation\n", encoding="utf-8")
        resumed = _cli(workspace, state, "resume", first["session_id"], "--approval-request-id", pending["request_id"], "--decision", decision)
        assert resumed["status"] == "completed"
        assert resumed["output"] == "finished after recovery"
        assert resumed["revision_id"] == first["revision_id"]
        assert resumed["run_id"] != first["run_id"]
        if decision == "allow":
            assert (workspace / "derived.txt").read_text(encoding="utf-8") == "derived output\n"
        else:
            assert not (workspace / "derived.txt").exists()
        messages = server.requests[1]["messages"]
        assistant = next(message for message in messages if message["role"] == "assistant")
        assert assistant["reasoning_content"] == "authentic loopback reasoning"
        reports = [message for message in messages if message["role"] == "tool"]
        assert [message["tool_call_id"] for message in reports] == ["read-id", "write-id"]
        assert "original observation" in reports[0]["content"]
        assert "new observation" not in reports[0]["content"]
        if decision == "deny":
            assert "denied" in reports[1]["content"]
        assert all(request["model"] == "exact-model" and "tool_choice" not in request for request in server.requests)
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        events = store.events(first["session_id"])
        intents = [event for event in events if event.event_type == "kernel.tool_intent"]
        assert [event.payload["call"]["tool_name"] for event in intents] == (["read", "write", "read"] if decision == "allow" else ["read", "read"])
        assert "loopback-secret-not-for-storage" not in json.dumps(dict(store.revision(first["revision_id"])), default=dict)
        assert store.checkpoint(first["session_id"]).kind == "terminal"


def test_read_only_overrides_yolo_without_dispatch(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    with _server([_turn(_call("write", "write", path="forbidden.txt", content="no")), _turn(output="denial observed")]) as server:
        _configure(workspace, server, approval_mode="yolo", read_only=True)
        result = _cli(workspace, state, "run", "attempt a write")
        assert result["status"] == "completed"
        assert not (workspace / "forbidden.txt").exists()
        assert "read-only" in server.requests[1]["messages"][-1]["content"]
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        assert not any(event.event_type == "kernel.tool_intent" for event in store.events(result["session_id"]))


def test_unknown_tool_argument_typo_is_rejected_without_side_effect(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    with _server(
        [
            _turn(_call("typo", "write", path="typo.txt", content="must not land", offest=1)),
            _turn(output="typo observed"),
        ]
    ) as server:
        _configure(workspace, server, approval_mode="yolo")
        result = _cli(workspace, state, "run", "attempt a typo write")
        assert result["status"] == "completed"
        assert not (workspace / "typo.txt").exists()
        assert "admitted JSON schema" in server.requests[1]["messages"][-1]["content"]
    with SqliteStore(state / "kernel-v1.sqlite3") as store:
        assert not any(event.event_type == "kernel.tool_intent" for event in store.events(result["session_id"]))


def _counter_plugin(source: Path, *, safe: bool = False) -> None:
    source.mkdir()
    code = f"""from pathlib import Path
import json
import os
import time
from voidcode.sdk.plugin import Plugin, tool_definition
from voidcode.sdk.manifest import parse_plugin_manifest
from voidcode.sdk.tools import ToolFailure, ToolSuccess, TextOutput

class Counter:
    def __init__(self, definition):
        self.definition = definition
    def invoke(self, call, *, context, services):
        target = context.workspace / call.arguments["path"]
        if {safe!r}:
            with (context.workspace / "read.invocations").open("a") as stream:
                stream.write(context.invocation_id + "\\n")
            (context.workspace / "read.entered").touch()
            while not (context.workspace / "release").exists():
                if context.abort_signal.cancelled:
                    return ToolFailure(call.tool_name, "read cancelled")
                time.sleep(0.01)
            return ToolSuccess(call.tool_name, TextOutput(target.read_text()))
        with target.open("a", encoding="utf-8") as stream:
            stream.write("effect\\n")
            stream.flush()
            os.fsync(stream.fileno())
        (context.workspace / "host.pid").write_text(str(os.getpid()))
        while not context.abort_signal.cancelled:
            time.sleep(0.01)
        return ToolFailure(call.tool_name, "cooperatively stopped after the effect")

def create_plugin(config):
    manifest = parse_plugin_manifest(json.loads(Path(__file__).with_name("voidcode-plugin.json").read_text()))
    return Plugin(tools=(Counter(tool_definition(manifest.tools[0])),))
"""
    path = source / "counter_plugin.py"
    path.write_text(code, encoding="utf-8")
    manifest = PluginManifest(
        plugin_id="counter",
        version="1.0.0",
        entrypoint="counter_plugin:create_plugin",
        files=(PluginFileIdentity(path="counter_plugin.py", sha256=hashlib.sha256(path.read_bytes()).hexdigest()),),
        capabilities=(PluginCapabilityRequest(name="workspace.read" if safe else "workspace.write", reason="exercise a real invocation"),),
        tools=(
            PluginToolDeclaration(
                name="counter",
                description="gated read" if safe else "write once then wait for cancellation",
                input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
                effects=(ToolEffect.READ if safe else ToolEffect.WRITE,),
                replay_policy="safe" if safe else "never",
                capabilities=("workspace.read" if safe else "workspace.write",),
            ),
        ),
    )
    (source / "voidcode-plugin.json").write_text(json.dumps(manifest.as_payload()), encoding="utf-8")


def _await_marker(path: Path, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    while not path.is_file():
        if process.poll() is not None or time.monotonic() >= deadline:
            stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"invocation never reached its side effect: {stdout} {stderr}")
        time.sleep(0.02)


def _await_lease_release(workspace: Path) -> None:
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY)
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    pytest.fail("orphan plugin host retained the workspace lease")
                time.sleep(0.02)
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("stop", ["kill", "signal", "host"])
def test_interrupted_mutation_is_not_replayed(tmp_path: Path, stop: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _counter_plugin(source)
    state = tmp_path / "state"
    (workspace / "input.txt").write_text("completed prefix\n", encoding="utf-8")
    with _server(
        [
            _turn(
                _call("prefix", "read", path="input.txt"), _call("effect", "counter", path="counter.txt"), _call("tail", "counter", path="tail.txt")
            ),
            _turn(output="interruption observed, no replay"),
        ]
    ) as server:
        config = _configure(workspace, server, approval_mode="yolo")
        config["plugins"] = {"counter": {"path": str(source)}}
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        untrusted = _cli(workspace, state, "run", "do not import untrusted code", code=1)
        assert "trust" in untrusted["error"]
        assert not (workspace / "counter.txt").exists()
        process = subprocess.Popen(
            _command(workspace, state, "run", "exercise real effects", "--session-id", "crash-session", trust=True),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _await_marker(workspace / "host.pid", process)
            if stop == "host":
                host_pid = int((workspace / "host.pid").read_text(encoding="utf-8").strip())
                os.kill(host_pid, signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=15)
                assert process.returncode == 130, stdout + stderr
                assert json.loads(stdout)["status"] == "interrupted"
            else:
                if stop == "kill":
                    process.kill()
                else:
                    process.send_signal(signal.SIGINT)
                stdout, stderr = process.communicate(timeout=15)
                assert process.returncode == (-signal.SIGKILL if stop == "kill" else 130), stdout + stderr
                if stop == "signal":
                    assert json.loads(stdout)["status"] == "interrupted"
            _await_lease_release(workspace)
            (workspace / "input.txt").write_text("changed after crash\n", encoding="utf-8")
            if stop == "signal":
                # A deliberate user stop is terminal for that run: the session admits a NEW
                # user action directly and never replays the abandoned batch.
                recovered = _cli(workspace, state, "run", "continue after user stop", "--session-id", "crash-session", trust=True)
            else:
                recovered = _cli(workspace, state, "resume", "crash-session", trust=True)
            assert recovered["status"] == "completed"
            assert (workspace / "counter.txt").read_text(encoding="utf-8") == "effect\n"
            assert not (workspace / "tail.txt").exists()
            reports = [message for message in server.requests[1]["messages"] if message["role"] == "tool"]
            assert [report["tool_call_id"] for report in reports] == ["prefix", "effect", "tail"]
            assert "completed prefix" in reports[0]["content"]
            assert "external effects are unknown" in reports[1]["content"]
            assert "not executed" in reports[2]["content"]
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=15)


def test_safe_read_recovery_reuses_the_durable_invocation_identity(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = tmp_path / "external"
    _counter_plugin(source, safe=True)
    state = tmp_path / "state"
    (workspace / "input.txt").write_text("before crash\n", encoding="utf-8")
    with _server([_turn(_call("read", "counter", path="input.txt")), _turn(output="safe recovery complete")]) as server:
        config = _configure(workspace, server, approval_mode="ask")
        config["plugins"] = {"counter": {"path": str(source)}}
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        process = subprocess.Popen(
            _command(workspace, state, "run", "read input", "--session-id", "safe-session", trust=True),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            _await_marker(workspace / "read.entered", process)
            process.kill()
            process.communicate(timeout=15)
            _await_lease_release(workspace)
            (workspace / "input.txt").write_text("fresh recovery observation\n", encoding="utf-8")
            (workspace / "release").touch()
            recovered = _cli(workspace, state, "resume", "safe-session", trust=True)
            assert recovered["status"] == "completed"
            identities = (workspace / "read.invocations").read_text().splitlines()
            assert len(identities) == 2 and identities[0] == identities[1]
            assert server.requests[1]["messages"][-1]["content"] == "fresh recovery observation\n"
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=15)
