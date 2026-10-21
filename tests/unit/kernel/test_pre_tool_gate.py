"""C5 acceptance: the pre-tool gate can only refuse, and it fails closed."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "gate reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


@contextmanager
def _server(replies: list[dict[str, object]]) -> Iterator[OpenAILoopbackServer]:
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


_CREATED: list[Kernel] = []


@pytest.fixture(autouse=True)
def _close_kernels() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _run_with_gate(tmp_path: Path, gate: str) -> tuple[Kernel, list[Mapping[str, object]]]:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", pre_tool_gate=gate)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="done")]
    server = OpenAILoopbackServer(replies)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "model": {"model": "endpoint/exact-model"},
                "policy": {"approval_mode": "yolo"},
                "providers": {
                    "endpoint": {
                        "api_key": "loopback-secret-not-for-storage",
                        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                    }
                },
                "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
            }
        ),
        encoding="utf-8",
    )
    try:
        kernel = open_kernel(workspace, state_dir=tmp_path / "state")
        _CREATED.append(kernel)
        kernel.create_session("sess-gate")
        assert kernel.run("call the probe", session_id="sess-gate").status == "completed"
        results = [
            payload
            for event in kernel.events_after("sess-gate")
            if event.event_type == "kernel.tool_completed"
            for payload in [dict(event.payload).get("result")]
            if isinstance(payload, Mapping)
        ]
        return kernel, results
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_gate_denies_before_the_invocation_runs(tmp_path: Path) -> None:
    kernel, results = _run_with_gate(tmp_path, "no writes today")
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "pre-tool gate" in str(results[0]["error"]) and "no writes today" in str(results[0]["error"])
    # Denied before effects: the probe never wrote its state note, and no intent was recorded.
    state = {row.owner: row for row in kernel.store.plugin_state_rows("session", "sess-gate")}
    assert PLUGIN_ID not in state
    assert not [event for event in kernel.events_after("sess-gate") if event.event_type == "kernel.tool_intent"]


def test_gate_failure_denies_the_call(tmp_path: Path) -> None:
    kernel, results = _run_with_gate(tmp_path, "explode")
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "pre-tool gate" in str(results[0]["error"])
    state = {row.owner: row for row in kernel.store.plugin_state_rows("session", "sess-gate")}
    assert PLUGIN_ID not in state


def test_allowing_gate_is_transparent(tmp_path: Path) -> None:
    # None means allow: the call runs normally under an admitted gate.
    kernel, results = _run_with_gate(tmp_path, "allow")
    assert len(results) >= 1
    assert results[0]["status"] == "ok", results[0]
    state = {row.owner: row for row in kernel.store.plugin_state_rows("session", "sess-gate")}
    assert state[PLUGIN_ID].payload.get("note") == "v1"
