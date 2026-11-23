"""C5 acceptance: the pre-tool gate can only refuse, and it fails closed."""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path

from tests.fixtures.core_factory import register_core
from tests.fixtures.reload_plugin import DEACTIVATE_TOOL, PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
from voidcode.core.engine import Core
from voidcode.core.events import EventType

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "gate reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _run_with_gate(
    tmp_path: Path,
    gate: str,
    *,
    read_only: bool = False,
    mutating: bool = False,
    tool_name: str = PROBE_TOOL,
) -> tuple[Core, list[Mapping[str, object]]]:
    source = tmp_path / "plugin"
    write_reload_plugin(
        source,
        tool_output="v1",
        pre_tool_gate=gate,
        pre_tool_match=(tool_name,),
        pre_tool_mutating=mutating,
    )
    replies = [_turn(_call("probe-1", tool_name)), _turn(output="done")]
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
                "policy": {"approval_mode": "yolo", "read_only": read_only},
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
        core = open_core(workspace, state_dir=tmp_path / "state")
        register_core(core)
        core.create_session("sess-gate")
        assert core.run("call the probe", session_id="sess-gate").status == "completed"
        results = [
            payload
            for event in core.events_after("sess-gate")
            if event.event_type == "core.tool_completed"
            for payload in [dict(event.payload).get("result")]
            if isinstance(payload, Mapping)
        ]
        return core, results
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_gate_denies_before_the_invocation_runs(tmp_path: Path) -> None:
    core, results = _run_with_gate(tmp_path, "no writes today")
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "pre-tool gate" in str(results[0]["error"]) and "no writes today" in str(results[0]["error"])
    # Denied before effects: the probe never wrote its state note, and no intent was recorded.
    state = {row.owner: row for row in core.store.plugin_state_rows("session", "sess-gate")}
    assert PLUGIN_ID not in state
    assert not [event for event in core.events_after("sess-gate") if event.event_type == "core.tool_intent"]


def test_gate_failure_denies_the_call(tmp_path: Path) -> None:
    core, results = _run_with_gate(tmp_path, "explode")
    assert len(results) == 1
    assert results[0]["status"] == "error"
    assert "pre-tool gate" in str(results[0]["error"])
    state = {row.owner: row for row in core.store.plugin_state_rows("session", "sess-gate")}
    assert PLUGIN_ID not in state


def test_allowing_gate_is_transparent(tmp_path: Path) -> None:
    # None means allow: the call runs normally under an admitted gate.
    core, results = _run_with_gate(tmp_path, "allow")
    assert len(results) >= 1
    assert results[0]["status"] == "ok", results[0]
    state = {row.owner: row for row in core.store.plugin_state_rows("session", "sess-gate")}
    assert state[PLUGIN_ID].payload.get("note") == "v1"


def test_mutating_gate_skip_is_visible_without_denying_under_read_only(tmp_path: Path) -> None:
    # "explode" would deny if asked: proving the skipped gate was never invoked.
    core, results = _run_with_gate(
        tmp_path,
        "explode",
        read_only=True,
        mutating=True,
        tool_name=DEACTIVATE_TOOL,
    )
    assert len(results) == 1 and results[0]["status"] == "ok", results
    skips = [event for event in core.events_after("sess-gate") if event.event_type == EventType.HOOK_SKIPPED]
    assert len(skips) == 1
    payload = skips[0].payload
    assert payload["kind"] == "pre_tool"
    assert payload["owner"] == PLUGIN_ID
    assert payload["reason"] == "read_only"
    assert payload["tool_name"] == DEACTIVATE_TOOL
    assert payload["tool_call_id"] == "probe-1"
    assert isinstance(payload["logical_turn"], str)
