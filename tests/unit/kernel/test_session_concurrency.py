"""C2 acceptance: independent sessions execute concurrently in one workspace."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import BLOCK_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "concurrency reasoning", "tool_calls": list(calls)},
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


_CREATED: list = []


@pytest.fixture(autouse=True)
def _close_kernels() -> Iterator[None]:
    yield
    while _CREATED:
        _CREATED.pop().close()


def test_a_blocked_session_does_not_serialize_another_session(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", blocking=True)
    replies = [
        _turn(_call("block-1", BLOCK_TOOL)),
        _turn(output="session B ran while A was blocked"),
        _turn(output="session A released"),
    ]
    with _server(replies) as server:
        # The loopback endpoint must be bound before the kernel opens its provider.
        config = {
            "schema_version": 1,
            "model": {"model": "endpoint/exact-model"},
            "policy": {"approval_mode": "yolo", "read_only": False},
            "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            # The bundled file tools are irrelevant to session scheduling; keep this test
            # hermetic so it exercises the kernel and the fixture only.
            "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
        }
        workspace = tmp_path / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        kernel = open_kernel(workspace, state_dir=tmp_path / "state")
        _CREATED.append(kernel)
        blocked_session = kernel.create_session("sess-blocked")
        other_session = kernel.create_session("sess-other")

        outcome: dict[str, object] = {}

        def _run_blocked() -> None:
            outcome["result"] = kernel.run("hold this session", session_id=blocked_session.session_id)

        blocked = threading.Thread(target=_run_blocked, daemon=True)
        blocked.start()
        marker = workspace / "block.started"
        deadline = time.monotonic() + 20
        while not marker.is_file() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.is_file(), "the blocking tool never started"

        # The other session must make progress while the first one holds its own run.
        other = kernel.run("independent work", session_id=other_session.session_id)
        assert other.status == "completed"
        assert other.output == "session B ran while A was blocked"
        assert blocked.is_alive(), "session A should still be blocked inside its tool"

        # And the blocked session still owns its own cancellation.
        assert kernel.cancel_session(blocked_session.session_id, "concurrency test done") is True
        blocked.join(timeout=20)
        assert not blocked.is_alive()
        blocked_result = outcome["result"]
        events = [event.event_type for event in kernel.events_after(blocked_session.session_id)]
        assert "kernel.run_interrupted" in events
        assert other_session.session_id in {record.session_id for record in kernel.sessions()}
        assert blocked_result is not None
