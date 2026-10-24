"""A coding run needs no admitted plugin, while ordinary governance still applies."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.activation import BUNDLED_PLUGIN_IDS
from voidcode.core.bootstrap import open_core
from voidcode.sdk.codec import decode_tool_result
from voidcode.sdk.tools import ToolSuccess


def _turn(name: str | None = None, arguments: dict[str, object] | None = None) -> dict[str, object]:
    message: dict[str, object] = {"role": "assistant", "content": "done" if name is None else None}
    if name is not None:
        message["tool_calls"] = [{"id": f"call-{name}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]
    return openai_response(message=message, finish="stop" if name is None else "tool_calls")


def _workspace(tmp_path: Path, server: OpenAILoopbackServer, *, mode: str) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "source.txt").write_text("original\n", encoding="utf-8")
    (workspace / "voidcode.json").write_text(
        json.dumps(
            {
                "model": {"model": "endpoint/test-model"},
                "providers": {"endpoint": {"base_url": f"http://127.0.0.1:{server.server_port}/v1", "api_key": "test-key"}},
                "plugins": {name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS},
                "policy": {"approval_mode": mode},
            }
        ),
        encoding="utf-8",
    )
    return workspace


def test_zero_plugins_read_write_execute_and_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Any attempt to load a concrete plugin fails; a hidden default plugin cannot pass.
    from voidcode.core.plugins import PluginRuntime

    def reject_plugin(*_args: object) -> None:
        pytest.fail("zero-plugin core attempted to load a plugin")

    monkeypatch.setattr(PluginRuntime, "_load_plugin", reject_plugin)
    server = OpenAILoopbackServer(
        [
            _turn("read", {"path": "source.txt"}),
            _turn("write", {"path": "result.txt", "content": "created\n"}),
            _turn("shell_exec", {"command": "printf executed"}),
            _turn(),
            _turn(),
        ]
    )
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        workspace = _workspace(tmp_path, server, mode="yolo")
        with open_core(workspace, state_dir=tmp_path / "state", auto_drain=False) as core:
            assert core.plugin_runtime.plugin_ids == ()
            result = core.run("Read, write, and execute.")
            assert result.status == "completed", result.error
            session_id = result.session_id
            completed = [
                decode_tool_result(event.payload["result"]) for event in core.store.events(session_id) if event.event_type == "core.tool_completed"
            ]
            assert [item.tool_name for item in completed] == ["read", "write", "shell_exec"]
            assert all(isinstance(item, ToolSuccess) for item in completed), completed
            assert (workspace / "result.txt").read_text() == "created\n"
        with open_core(workspace, state_dir=tmp_path / "state", auto_drain=False) as core:
            assert core.plugin_runtime.plugin_ids == ()
            result = core.run("Continue", session_id=session_id)
            assert result.status == "completed", result.error
        tool_messages = [item for request in server.requests for item in request["messages"] if item["role"] == "tool"]
        assert any("original" in str(item["content"]) for item in tool_messages)
        assert any("executed" in str(item["content"]) for item in tool_messages)
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_zero_plugins_write_still_requires_approval(tmp_path: Path) -> None:
    server = OpenAILoopbackServer([_turn("write", {"path": "result.txt", "content": "created"})])
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        workspace = _workspace(tmp_path, server, mode="ask")
        with open_core(workspace, state_dir=tmp_path / "state", auto_drain=False) as core:
            result = core.run("Write a file")
            assert result.status == "waiting", result.error
            assert not (workspace / "result.txt").exists()
            assert any(event.event_type == "core.approval_requested" for event in core.store.events(result.session_id))
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_zero_plugin_example_disables_every_bundled_plugin() -> None:
    example = Path(__file__).resolve().parents[2] / "examples/zero-plugins/voidcode.json"
    config = json.loads(example.read_text(encoding="utf-8"))
    assert set(config["plugins"]) == set(BUNDLED_PLUGIN_IDS)
    assert all(entry == {"enabled": False} for entry in config["plugins"].values())
