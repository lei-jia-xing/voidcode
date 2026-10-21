"""Lifecycle acceptance: session_start/session_end fire once per run and are recorded."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.bootstrap import open_core
from voidcode.core.engine import Core

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "lifecycle reasoning", "tool_calls": list(calls)},
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


_CREATED: list[Core] = []


@pytest.fixture(autouse=True)
def _close_cores() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def _open(tmp_path: Path) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    core = open_core(workspace, state_dir=tmp_path / "state")
    _CREATED.append(core)
    return core


def _configure(workspace: Path, server: OpenAILoopbackServer, source: Path) -> None:
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


def _hook_events(core: Core, session_id: str, kind: str) -> list[dict[str, object]]:
    # Every owner with lifecycle hooks fires once per run, including the bundled `hooks`
    # plugin: this fixture asserts the reload plugin's pair only.
    return [
        dict(event.payload)
        for event in core.events_after(session_id)
        if event.event_type == "kernel.hook_invoked"
        and dict(event.payload).get("hook_kind") == kind
        and dict(event.payload).get("owner") == PLUGIN_ID
    ]


def test_lifecycle_hooks_fire_once_per_run_with_durable_notes(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="done"), _turn(output="second run")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-life")
        assert core.run("first", session_id="sess-life").status == "completed"

        started = _hook_events(core, "sess-life", "session_start")
        ended = _hook_events(core, "sess-life", "session_end")
        assert len(started) == 1 and len(ended) == 1
        assert started[0]["owner"] == PLUGIN_ID and started[0]["note"] == f"started {started[0]['run_id']}"
        assert ended[0]["note"] == "ended completed"
        # A second run fires the pair again, once each, for its own run identity.
        assert core.run("second", session_id="sess-life").status == "completed"
        started_again = _hook_events(core, "sess-life", "session_start")
        ended_again = _hook_events(core, "sess-life", "session_end")
        assert len(started_again) == 2 and len(ended_again) == 2
        assert started_again[0]["run_id"] != started_again[1]["run_id"]


def test_lifecycle_hooks_do_not_duplicate_on_resume(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="stopped"), _turn(output="next answer")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        core = _open(tmp_path)
        core.create_session("sess-resume")
        assert core.run("first", session_id="sess-resume").status == "completed"
        # Resuming the same run's session must not fire session_start a second time for that run.
        assert core.run("next", session_id="sess-resume").status == "completed"
        starts = _hook_events(core, "sess-resume", "session_start")
        assert len(starts) == len({str(entry["run_id"]) for entry in starts})
