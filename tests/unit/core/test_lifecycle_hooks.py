"""Lifecycle acceptance: session_start/session_end fire once per run and are recorded."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from tests.fixtures.core_factory import register_core
from tests.fixtures.reload_plugin import PROBE_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.bootstrap import open_core
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


def _open(tmp_path: Path) -> Core:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    core = open_core(workspace, state_dir=tmp_path / "state")
    register_core(core)
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


def _lifecycle_records(workspace: Path) -> list[str]:
    log_file = workspace / "lifecycle.log"
    if not log_file.exists():
        return []
    return [line.strip() for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_lifecycle_hooks_fire_once_per_run_with_durable_notes(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="done"), _turn(output="second run")]
    with _server(replies) as server:
        workspace = tmp_path / "workspace"
        _configure(workspace, server, source)
        core = _open(tmp_path)
        core.create_session("sess-life")
        assert core.run("first", session_id="sess-life").status == "completed"

        records = _lifecycle_records(workspace)
        starts = [r for r in records if r.startswith("start:")]
        ends = [r for r in records if r.startswith("end:")]
        assert len(starts) == 1 and len(ends) == 1
        assert ends[0] == "end:completed"
        # A second run fires the pair again, once each, for its own run identity.
        assert core.run("second", session_id="sess-life").status == "completed"
        records_again = _lifecycle_records(workspace)
        starts_again = [r for r in records_again if r.startswith("start:")]
        ends_again = [r for r in records_again if r.startswith("end:")]
        assert len(starts_again) == 2 and len(ends_again) == 2
        assert starts_again[0] != starts_again[1]


def test_lifecycle_hooks_do_not_duplicate_on_resume(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", lifecycle_hooks=True)
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="stopped"), _turn(output="next answer")]
    with _server(replies) as server:
        workspace = tmp_path / "workspace"
        _configure(workspace, server, source)
        core = _open(tmp_path)
        core.create_session("sess-resume")
        assert core.run("first", session_id="sess-resume").status == "completed"
        # Resuming the same run's session must not fire session_start a second time for that run.
        assert core.run("next", session_id="sess-resume").status == "completed"
        starts = [r for r in _lifecycle_records(workspace) if r.startswith("start:")]
        assert len(starts) == len(set(starts))
