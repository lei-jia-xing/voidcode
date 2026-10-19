"""C8 acceptance: the dispatch carries a measured request window, not an estimate."""

from __future__ import annotations

import hashlib
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
from voidcode.sdk.hooks import RequestWindow

PLUGIN_ID = "reload-fixture"
PROMPT = "measure this exact prompt"


def _turn(*calls: dict[str, object], output: str | None = None) -> dict[str, object]:
    return openai_response(
        message={"role": "assistant", "content": output, "reasoning_content": "window reasoning", "tool_calls": list(calls)},
        finish="tool_calls" if calls else "stop",
    )


def _call(identity: str, name: str, **arguments: object) -> dict[str, object]:
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


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
                "providers": {"endpoint": {"api_key": "loopback-secret-not-for-storage", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
                "plugins": {"files": {"enabled": False}, PLUGIN_ID: {"path": str(source), "enabled": True}},
            }
        ),
        encoding="utf-8",
    )


def _windows(kernel: Kernel, session_id: str) -> list[Mapping[str, object]]:
    return [
        window
        for event in kernel.events_after(session_id)
        if event.event_type == "kernel.provider_intent"
        for window in [dict(event.payload).get("request_window")]
        if isinstance(window, Mapping)
    ]


def test_window_measures_the_literal_messages_of_each_dispatch(tmp_path: Path) -> None:
    source = tmp_path / "plugin"
    write_reload_plugin(source, tool_output="v1")
    replies = [_turn(_call("probe-1", PROBE_TOOL)), _turn(output="done")]
    with _server(replies) as server:
        _configure(tmp_path / "workspace", server, source)
        kernel = open_kernel(tmp_path / "workspace", state_dir=tmp_path / "state", trusted_plugins=frozenset({PLUGIN_ID}))
        _CREATED.append(kernel)
        kernel.create_session("sess-window")
        assert kernel.run(PROMPT, session_id="sess-window").status == "completed"

        windows = _windows(kernel, "sess-window")
        assert len(windows) == 2, "one window per provider dispatch"
        first = windows[0]
        sent = server.requests[0].get("messages")
        assert isinstance(sent, list)

        # The measurement is the exact length and digest of every string the kernel sent.
        segments = first["segments"]
        assert isinstance(segments, (list, tuple)) and len(segments) == len(sent)
        measured = 0
        for segment, message in zip(segments, sent, strict=True):
            assert isinstance(segment, Mapping) and isinstance(message, Mapping)
            content = str(message.get("content") or "")
            assert segment["role"] == message.get("role")
            assert segment["characters"] == len(content)
            assert segment["sha256"] == hashlib.sha256(content.encode("utf-8")).hexdigest()
            measured += len(content)
        assert first["message_characters"] == measured
        assert PROMPT in json.dumps(sent)
        tool_schema_characters = int(str(first["tool_schema_characters"]))
        assert tool_schema_characters > 0
        assert int(str(first["tool_count"])) > 0

        # Decodable by clients with the shared typed shape, and free of token estimates.
        # The decoder reads the payload exactly as a plugin receives it (arrays as tuples),
        # with no re-encoding through the wire codec.
        window = RequestWindow.from_payload(dict(first))
        assert len(window.segments) == len(sent)
        assert window.message_characters == measured
        assert window.total_characters == window.message_characters + window.tool_schema_characters
        assert window.tool_count == int(str(first["tool_count"]))
        serialized = json.dumps(dict(first), default=str)
        assert "estimate" not in serialized
        assert "tokens" not in serialized

        # The second dispatch re-measures the grown conversation, not a cached estimate.
        assert int(str(windows[1]["message_characters"])) > int(str(windows[0]["message_characters"]))
