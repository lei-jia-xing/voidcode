from __future__ import annotations

import sys
from pathlib import Path

from voidcode.runtime.config import RuntimeMcpConfig, RuntimeMcpServerConfig
from voidcode.runtime.mcp import McpConfigState, McpManagerState, build_mcp_manager

_MCP_SERVER_SCRIPT = r"""
from __future__ import annotations

import json
import sys


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo-mcp", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo the text argument.",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"text": {"type": "string"}},
                                "required": ["text"],
                            },
                        }
                    ]
                },
            }
        )
        continue
    if method == "tools/call":
        params = message.get("params", {})
        arguments = params.get("arguments", {}) if isinstance(params, dict) else {}
        text = arguments.get("text", "") if isinstance(arguments, dict) else ""
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [{"type": "text", "text": f"echo:{text}"}],
                    "isError": False,
                },
            }
        )
        continue
"""


def test_mcp_manager_discovers_stdio_tools_and_invokes_calls(tmp_path: Path) -> None:
    server_script = tmp_path / "echo_mcp_server.py"
    server_script.write_text(_MCP_SERVER_SCRIPT, encoding="utf-8")

    config = RuntimeMcpConfig(
        enabled=True,
        servers={
            "echo": RuntimeMcpServerConfig(
                transport="stdio",
                command=(sys.executable, str(server_script)),
            )
        },
    )

    manager = build_mcp_manager(config)
    assert manager.configuration == McpConfigState.from_runtime_config(config)
    assert manager.current_state().mode == "managed"

    discovered = manager.list_tools(workspace=tmp_path)

    assert len(discovered) == 1
    assert discovered[0].server_name == "echo"
    assert discovered[0].tool_name == "echo"
    assert discovered[0].input_schema == {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    result = manager.call_tool(
        server_name="echo",
        tool_name="echo",
        arguments={"text": "hello"},
        workspace=tmp_path,
    )

    assert result.is_error is False
    assert result.content == [{"type": "text", "text": "echo:hello"}]

    shutdown_events = manager.shutdown()
    assert shutdown_events
    assert any(event.event_type == "runtime.mcp_server_stopped" for event in shutdown_events)


def test_disabled_mcp_manager_rejects_tool_listing(tmp_path: Path) -> None:
    manager = build_mcp_manager(RuntimeMcpConfig(enabled=False))

    assert manager.current_state() == McpManagerState(
        mode="disabled",
        configuration=McpConfigState.from_runtime_config(RuntimeMcpConfig(enabled=False)),
    )

    try:
        manager.list_tools(workspace=tmp_path)
    except ValueError as exc:
        assert str(exc) == "MCP runtime support is disabled"
    else:
        raise AssertionError("expected MCP manager to reject listing while disabled")


def test_mcp_manager_matches_jsonrpc_response_ids_when_notifications_interleave(
    tmp_path: Path,
) -> None:
    server_script = tmp_path / "interleaved_mcp_server.py"
    server_script.write_text(
        r"""
from __future__ import annotations

import json
import sys


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"text": "warming up"},
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo-mcp", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"text": "listing"},
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "Echo the text argument.",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        )
        continue
    if method == "tools/call":
        send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/message",
                "params": {"text": "calling"},
            }
        )
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [{"type": "text", "text": "echo:ok"}],
                    "isError": False,
                },
            }
        )
        continue
""",
        encoding="utf-8",
    )

    config = RuntimeMcpConfig(
        enabled=True,
        servers={
            "echo": RuntimeMcpServerConfig(
                transport="stdio",
                command=(sys.executable, str(server_script)),
            )
        },
    )

    manager = build_mcp_manager(config)

    discovered = manager.list_tools(workspace=tmp_path)

    assert [tool.tool_name for tool in discovered] == ["echo"]

    result = manager.call_tool(
        server_name="echo",
        tool_name="echo",
        arguments={},
        workspace=tmp_path,
    )

    assert result.content == [{"type": "text", "text": "echo:ok"}]


def test_mcp_manager_inherits_parent_environment_for_server_process(tmp_path: Path) -> None:
    server_script = tmp_path / "env_mcp_server.py"
    server_script.write_text(
        r"""
from __future__ import annotations

import json
import os
import sys


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "env-mcp", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "tools": [
                        {
                            "name": "inspect_env",
                            "description": "Inspect inherited environment.",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        )
        continue
    if method == "tools/call":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "content": [
                        {
                            "type": "text",
                            "text": os.environ.get("VOIDCODE_TEST_PARENT_ENV", "missing"),
                        }
                    ],
                    "isError": False,
                },
            }
        )
        continue
""",
        encoding="utf-8",
    )

    config = RuntimeMcpConfig(
        enabled=True,
        servers={
            "env": RuntimeMcpServerConfig(
                transport="stdio",
                command=(sys.executable, str(server_script)),
            )
        },
    )

    manager = build_mcp_manager(config)

    import os

    previous = os.environ.get("VOIDCODE_TEST_PARENT_ENV")
    os.environ["VOIDCODE_TEST_PARENT_ENV"] = "inherited"
    try:
        _ = manager.list_tools(workspace=tmp_path)
        result = manager.call_tool(
            server_name="env",
            tool_name="inspect_env",
            arguments={},
            workspace=tmp_path,
        )
    finally:
        if previous is None:
            del os.environ["VOIDCODE_TEST_PARENT_ENV"]
        else:
            os.environ["VOIDCODE_TEST_PARENT_ENV"] = previous

    assert result.content == [{"type": "text", "text": "inherited"}]
