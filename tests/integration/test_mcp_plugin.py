"""Exercise discovery and calls over a real SDK-owned stdio server."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from threading import Thread

import pytest

from tests.provider_loopback import OpenAILoopbackServer, openai_response
from voidcode.core.activation import BUNDLED_PLUGIN_IDS
from voidcode.core.bootstrap import open_core
from voidcode.plugins.mcp import _mcp_config
from voidcode.plugins.mcp.mcp_manager import build_mcp_manager


def _server_source(tmp_path: Path, *, expose_tool: bool = True) -> tuple[Path, Path]:
    source = tmp_path / "server.py"
    pid_file = tmp_path / "server.pid"
    source.write_text(
        "from pathlib import Path\nimport os\n"
        f"Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
        "from mcp.server.mcpserver import MCPServer\n"
        "server = MCPServer('audit-test')\n"
        + ("@server.tool()\ndef echo(text: str) -> str:\n    return 'echo: ' + text\n" if expose_tool else "")
        + "server.run()\n",
        encoding="utf-8",
    )
    return source, pid_file


@pytest.mark.parametrize("expose_tool", [True, False])
def test_stdio_discovery_call_passive_inventory_and_shutdown(tmp_path: Path, expose_tool: bool) -> None:
    source, pid_file = _server_source(tmp_path, expose_tool=expose_tool)
    manager = build_mcp_manager(_mcp_config({"servers": {"demo": {"command": [sys.executable, str(source)]}}}))
    try:
        assert manager.cached_surface(workspace=tmp_path).servers == {}
        tools = manager.list_tools(workspace=tmp_path)
        assert [tool.tool_name for tool in tools] == (["echo"] if expose_tool else [])
        assert manager.cached_surface(workspace=tmp_path).servers == {"demo": tools}
        if expose_tool:
            result = manager.call_tool(server_name="demo", tool_name="echo", arguments={"text": "hello"}, workspace=tmp_path)
            assert result.is_error is False
            assert result.content[0]["text"] == "echo: hello"
    finally:
        manager.shutdown()
    assert manager.current_state().servers["demo"].status == "stopped"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_file.read_text()), 0)


def test_failed_server_start_keeps_an_inspectable_error(tmp_path: Path) -> None:
    manager = build_mcp_manager(_mcp_config({"servers": {"demo": {"command": [str(tmp_path / "missing-executable")]}}}))
    try:
        with pytest.raises(ValueError, match="failed to start server"):
            manager.list_tools(workspace=tmp_path)
        state = manager.current_state().servers["demo"]
        assert state.status == "failed"
        assert "missing-executable" in (state.error or "")
        assert manager.cached_surface(workspace=tmp_path).servers == {}
    finally:
        manager.shutdown()


def test_admitted_mcp_tool_reaches_the_model_and_updates_inventory(tmp_path: Path) -> None:
    source, _ = _server_source(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    server = OpenAILoopbackServer(
        [
            openai_response(
                message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "echo-call",
                            "type": "function",
                            "function": {"name": "mcp", "arguments": json.dumps({"server": "demo", "tool": "echo", "arguments": {"text": "hello"}})},
                        }
                    ],
                },
                finish="tool_calls",
            ),
            openai_response(message={"role": "assistant", "content": "done"}, finish="stop"),
        ]
    )
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        config = {
            "model": {"model": "endpoint/test-model"},
            "policy": {"approval_mode": "yolo"},
            "providers": {"endpoint": {"api_key": "test-key", "base_url": f"http://127.0.0.1:{server.server_port}/v1"}},
            "plugins": {
                **{name: {"enabled": False} for name in BUNDLED_PLUGIN_IDS},
                "mcp": {"config": {"servers": {"demo": {"command": [sys.executable, str(source)]}}}},
            },
        }
        (workspace / "voidcode.json").write_text(json.dumps(config), encoding="utf-8")
        with open_core(workspace, state_dir=tmp_path / "state") as core:
            session_id = core.create_session().session_id
            assert core.run("call echo", session_id=session_id).status == "completed"
            assert "echo: hello" in json.dumps(server.requests[1]["messages"])
            result = core.invoke_command(session_id, "mcp", {}, expected_revision=core.revision_id)
            assert result.status == "ok"
            assert "echo" in (result.output or "")
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


def test_connection_loss_preserves_failure_and_next_call_restarts(tmp_path: Path) -> None:
    source = tmp_path / "crashing_server.py"
    source.write_text(
        "import os\nfrom mcp.server.mcpserver import MCPServer\n"
        "server = MCPServer('crash-test')\n"
        "@server.tool()\ndef crash() -> str:\n    os._exit(7)\n"
        "@server.tool()\ndef echo(text: str) -> str:\n    return text\n"
        "server.run()\n",
        encoding="utf-8",
    )
    manager = build_mcp_manager(_mcp_config({"servers": {"demo": {"command": [sys.executable, str(source)]}}}))
    try:
        with pytest.raises(ValueError):
            manager.call_tool(server_name="demo", tool_name="crash", arguments={}, workspace=tmp_path)
        state = manager.current_state().servers["demo"]
        assert state.status == "failed"
        assert state.error
        result = manager.call_tool(server_name="demo", tool_name="echo", arguments={"text": "restarted"}, workspace=tmp_path)
        assert result.content[0]["text"] == "restarted"
        assert manager.current_state().servers["demo"].status == "running"
        assert manager.current_state().servers["demo"].error is None
    finally:
        manager.shutdown()


def test_recoverable_rpc_error_keeps_connection_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcp import ClientSession
    from mcp.shared.exceptions import MCPError

    source, pid_file = _server_source(tmp_path)
    original = ClientSession.call_tool

    async def fail_bad_call(self, name, *args, **kwargs):
        if name == "bad":
            raise MCPError(-32602, "invalid params")
        return await original(self, name, *args, **kwargs)

    monkeypatch.setattr(ClientSession, "call_tool", fail_bad_call)
    manager = build_mcp_manager(_mcp_config({"servers": {"demo": {"command": [sys.executable, str(source)]}}}))
    try:
        manager.list_tools(workspace=tmp_path)
        pid = pid_file.read_text()
        with pytest.raises(ValueError, match="invalid params"):
            manager.call_tool(server_name="demo", tool_name="bad", arguments={}, workspace=tmp_path)
        assert manager.current_state().servers["demo"].status == "running"
        result = manager.call_tool(server_name="demo", tool_name="echo", arguments={"text": "still alive"}, workspace=tmp_path)
        assert result.content[0]["text"] == "echo: still alive"
        assert pid_file.read_text() == pid
    finally:
        manager.shutdown()


def test_sdk_resource_uris_are_projected_as_wire_json(tmp_path: Path) -> None:
    source, _ = _server_source(tmp_path, expose_tool=False)
    source.write_text(
        source.read_text().replace(
            "server.run()",
            """
from mcp.types import CallToolResult, EmbeddedResource, BlobResourceContents
@server.tool()
def blob() -> CallToolResult:
    return CallToolResult(content=[EmbeddedResource(type="resource", resource=BlobResourceContents(uri="custom://bytes", blob="YQ=="))])
server.run()
""",
        ),
        encoding="utf-8",
    )
    manager = build_mcp_manager(_mcp_config({"servers": {"demo": {"command": [sys.executable, str(source)]}}}))
    try:
        result = manager.call_tool(server_name="demo", tool_name="blob", arguments={}, workspace=tmp_path)
        assert result.is_error is False
        assert result.content == [{"type": "resource", "resource": {"uri": "custom://bytes", "blob": "YQ=="}}]
        assert json.loads(json.dumps(result.content)) == result.content
    finally:
        manager.shutdown()
