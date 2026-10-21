"""Projection acceptance: a declared, kernel-verified window substitution (no event rewrite)."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import pytest

from tests.fixtures.reload_plugin import OVERLAY_COMMAND, SERVICES_TOOL, write_reload_plugin
from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.kernel.bootstrap import open_kernel
from voidcode.kernel.engine import Kernel

PLUGIN_ID = "reload-fixture"


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "projection reasoning", "tool_calls": list(calls)},
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


def _open(tmp_path: Path) -> Kernel:
    kernel = open_kernel(tmp_path / "workspace", state_dir=tmp_path / "state")
    _CREATED.append(kernel)
    return kernel


def _messages(request: Mapping[str, object]) -> list[Mapping[str, object]]:
    messages = request.get("messages")
    assert isinstance(messages, list)
    return [message for message in messages if isinstance(message, Mapping)]


def test_projection_replaces_a_committed_prefix_and_keeps_history(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [
        _turn(_call("turn-1", SERVICES_TOOL, action="project")),
        _turn(output="summarised"),
        _turn(output="second answer"),
    ]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-project")
        first_prompt = "first prompt " + "detail " * 40
        assert kernel.run(first_prompt, session_id="sess-project").status == "completed"

        # The tool declared the projection; the kernel verified it and measured the accounting.
        projection = kernel.store.window_projection("sess-project")
        assert projection is not None
        assert projection.owner == PLUGIN_ID
        assert projection.covered_bytes > len(projection.replacement.encode("utf-8"))
        assert projection.dropped_bytes == projection.covered_bytes - len(projection.replacement.encode("utf-8"))

        # History is untouched: every original event is still there, in order.
        sequences = [event.sequence for event in kernel.events_after("sess-project")]
        assert sequences == sorted(sequences) and len(sequences) >= 4

        # The next dispatch substitutes the replacement for the covered prefix.
        assert kernel.run("second prompt", session_id="sess-project").status == "completed"
        sent = _messages(server.requests[-1])
        assert any(str(message.get("content", "")).startswith("[window summary by") for message in sent)
        assert first_prompt not in json.dumps([dict(message) for message in sent], default=str)


def test_projection_is_refused_when_it_would_not_shrink_the_window(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True)
    replies = [_turn(_call("turn-1", SERVICES_TOOL, action="project_huge")), _turn(output="refused")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-refuse")
        assert kernel.run("first prompt " + "detail " * 40, session_id="sess-refuse").status == "completed"
        assert kernel.store.window_projection("sess-refuse") is None
        results = [dict(event.payload).get("result") for event in kernel.events_after("sess-refuse") if event.event_type == "kernel.tool_completed"]
        assert results and isinstance(results[0], Mapping)
        assert results[0]["status"] == "error"


def test_read_only_owner_cannot_project(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1", services_probe=True, overlay_command=True)
    replies = [_turn(output="entered"), _turn(_call("turn-1", SERVICES_TOOL, action="project")), _turn(output="denied")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = _open(tmp_path)
        kernel.create_session("sess-ro")
        entered = kernel.invoke_command("sess-ro", OVERLAY_COMMAND, {"action": "on"}, expected_revision=kernel.revision_id)
        assert entered.status == "ok", entered.error
        assert kernel.run("project under a restriction", session_id="sess-ro").status == "completed"
        assert kernel.store.window_projection("sess-ro") is None
