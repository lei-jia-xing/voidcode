from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, cast

import pytest

from voidcode.mcp import (
    MCP_PROTOCOL_VERSION,
    InMemoryMcpDiagnosticsCollector,
)
from voidcode.mcp import (
    McpManagerState as CanonicalMcpManagerState,
)
from voidcode.runtime.config import RuntimeMcpConfig, RuntimeMcpServerConfig
from voidcode.runtime.mcp import McpConfigState, McpManagerState, McpRuntimeEvent, build_mcp_manager

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
                            "annotations": {
                                "readOnlyHint": True,
                                "destructiveHint": False,
                                "idempotentHint": True,
                                "openWorldHint": False,
                            },
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


def test_runtime_mcp_reexports_canonical_manager_state_type() -> None:
    assert McpManagerState is CanonicalMcpManagerState


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
    assert discovered[0].safety.read_only is True
    assert discovered[0].safety.destructive is False
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


def test_mcp_manager_initializes_with_authoritative_protocol_version(tmp_path: Path) -> None:
    protocol_path = tmp_path / "protocol.txt"
    server_script = tmp_path / "protocol_mcp_server.py"
    server_script.write_text(
        rf"""
from __future__ import annotations

import json
import pathlib
import sys

protocol_path = pathlib.Path(r"{protocol_path}")


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        params = message.get("params", {{}})
        protocol_path.write_text(str(params.get("protocolVersion")), encoding="utf-8")
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "protocolVersion": "2025-11-25",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "protocol-mcp", "version": "0.1.0"}},
                }},
            }}
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send({{"jsonrpc": "2.0", "id": message["id"], "result": {{"tools": []}}}})
        continue
""",
        encoding="utf-8",
    )
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "protocol": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                )
            },
        )
    )

    assert manager.list_tools(workspace=tmp_path) == ()
    assert protocol_path.read_text(encoding="utf-8") == MCP_PROTOCOL_VERSION


def test_mcp_manager_gracefully_closes_server_stdin_on_shutdown(tmp_path: Path) -> None:
    marker_path = tmp_path / "shutdown-marker"
    server_script = tmp_path / "shutdown_mcp_server.py"
    server_script.write_text(
        rf"""
from __future__ import annotations

import json
import pathlib
import sys

marker = pathlib.Path(r"{marker_path}")


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "protocolVersion": "2025-11-25",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "shutdown-mcp", "version": "0.1.0"}},
                }},
            }}
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send({{"jsonrpc": "2.0", "id": message["id"], "result": {{"tools": []}}}})
        continue

marker.write_text("closed", encoding="utf-8")
""",
        encoding="utf-8",
    )
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "shutdown": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                )
            },
        )
    )

    assert manager.list_tools(workspace=tmp_path) == ()

    _ = manager.shutdown()

    assert marker_path.read_text(encoding="utf-8") == "closed"


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


def test_mcp_manager_times_out_when_server_never_responds(tmp_path: Path) -> None:
    server_script = tmp_path / "silent_mcp_server.py"
    server_script.write_text(
        r"""
from __future__ import annotations

import time

while True:
    time.sleep(10)
""",
        encoding="utf-8",
    )

    config = RuntimeMcpConfig(
        enabled=True,
        servers={
            "silent": RuntimeMcpServerConfig(
                transport="stdio",
                command=(sys.executable, str(server_script)),
            )
        },
    )

    manager = build_mcp_manager(config)
    start = time.monotonic()
    try:
        manager.list_tools(workspace=tmp_path)
    except ValueError as exc:
        elapsed = time.monotonic() - start
        assert "timed out" in str(exc)
        assert elapsed < 5
    else:
        raise AssertionError("expected MCP manager to time out waiting for a silent server")

    failure_events = manager.drain_events()
    assert [event.event_type for event in failure_events] == [
        "runtime.mcp_server_started",
        "runtime.mcp_server_failed",
        "runtime.mcp_server_stopped",
    ]
    assert failure_events[1].payload["stage"] == "startup"
    assert failure_events[1].payload["method"] == "initialize"


def test_mcp_manager_records_diagnostics_for_runtime_failures(tmp_path: Path) -> None:
    server_script = tmp_path / "silent_mcp_server.py"
    server_script.write_text(
        r"""
from __future__ import annotations

import time

while True:
    time.sleep(10)
""",
        encoding="utf-8",
    )
    collector = InMemoryMcpDiagnosticsCollector()
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            request_timeout_seconds=0.2,
            servers={
                "silent": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                )
            },
        ),
        diagnostics_collector=collector,
    )

    try:
        manager.list_tools(workspace=tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("expected MCP manager to time out waiting for a silent server")

    diagnostics = collector.get_diagnostics()
    assert diagnostics
    assert diagnostics[-1].category == "timeout"
    assert diagnostics[-1].details is not None
    assert diagnostics[-1].details["timeout_seconds"] == 0.2


def test_mcp_manager_restarts_server_after_timeout(tmp_path: Path) -> None:
    server_script = tmp_path / "retryable_mcp_server.py"
    marker_path = tmp_path / "first_run_marker"
    server_script.write_text(
        rf'''
from __future__ import annotations

import json
import pathlib
import sys
import time

marker = pathlib.Path(r"{marker_path}")
first_run = not marker.exists()
if first_run:
    marker.write_text("seen", encoding="utf-8")


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    message = json.loads(line)
    method = message.get("method")
    if first_run:
        time.sleep(10)
        continue
    if method == "initialize":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "protocolVersion": "2025-11-25",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "retry-mcp", "version": "0.1.0"}},
                }},
            }}
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "tools": [
                        {{
                            "name": "echo",
                            "description": "Echo the text argument.",
                            "inputSchema": {{"type": "object"}},
                        }}
                    ]
                }},
            }}
        )
        continue
''',
        encoding="utf-8",
    )

    config = RuntimeMcpConfig(
        enabled=True,
        servers={
            "retry": RuntimeMcpServerConfig(
                transport="stdio",
                command=(sys.executable, str(server_script)),
            )
        },
    )

    manager = build_mcp_manager(config)

    try:
        manager.list_tools(workspace=tmp_path)
    except ValueError as exc:
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected first MCP attempt to time out")

    failure_events = manager.drain_events()
    assert [event.event_type for event in failure_events] == [
        "runtime.mcp_server_started",
        "runtime.mcp_server_failed",
        "runtime.mcp_server_stopped",
    ]

    discovered = manager.list_tools(workspace=tmp_path)

    assert [tool.tool_name for tool in discovered] == ["echo"]


def test_mcp_manager_retry_connections_skips_ownerless_session_scoped_servers(
    tmp_path: Path,
) -> None:
    runtime_started = tmp_path / "runtime-started.txt"
    session_started = tmp_path / "session-started.txt"
    runtime_server = tmp_path / "runtime_retry_mcp_server.py"
    session_server = tmp_path / "session_retry_mcp_server.py"
    server_template = r"""
from __future__ import annotations

import json
import pathlib
import sys

started = pathlib.Path(r"{started_path}")
started.write_text("started\n", encoding="utf-8")


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "protocolVersion": "2025-11-25",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "retry-scope", "version": "0.1.0"}},
                }},
            }}
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send({{"jsonrpc": "2.0", "id": message["id"], "result": {{"tools": []}}}})
        continue
"""
    runtime_server.write_text(
        server_template.format(started_path=runtime_started),
        encoding="utf-8",
    )
    session_server.write_text(
        server_template.format(started_path=session_started),
        encoding="utf-8",
    )
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "runtime": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(runtime_server)),
                ),
                "session": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(session_server)),
                    scope="session",
                ),
            },
        )
    )

    manager.retry_connections(workspace=tmp_path)

    assert runtime_started.read_text(encoding="utf-8") == "started\n"
    assert not session_started.exists()
    state = manager.current_state()
    assert state.servers["runtime"].status == "running"
    assert state.servers["session"].status == "stopped"
    assert [event.event_type for event in manager.drain_events()] == [
        "runtime.mcp_server_started",
        "runtime.mcp_server_acquired",
    ]

    try:
        manager.list_tools(workspace=tmp_path)
    except ValueError as exc:
        assert "session-scoped server requires an owning session id" in str(exc)
    else:
        raise AssertionError("expected ownerless session-scoped discovery to remain strict")

    assert not session_started.exists()


def test_mcp_manager_preserves_session_on_recoverable_call_failures(tmp_path: Path) -> None:
    server_script = tmp_path / "recoverable_call_mcp_server.py"
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
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "recoverable-call-mcp", "version": "0.1.0"},
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
                                "properties": {
                                    "mode": {"type": "string"},
                                    "text": {"type": "string"},
                                },
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
        mode = arguments.get("mode") if isinstance(arguments, dict) else None
        if mode == "tool_not_found":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Tool not found"},
                }
            )
            continue
        if mode == "invalid_params":
            send(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32602, "message": "Invalid tool arguments"},
                }
            )
            continue
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
""",
        encoding="utf-8",
    )

    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "recoverable": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                )
            },
        )
    )

    discovered = manager.list_tools(workspace=tmp_path)

    assert [tool.tool_name for tool in discovered] == ["echo"]
    assert [event.event_type for event in manager.drain_events()] == [
        "runtime.mcp_server_started",
        "runtime.mcp_server_acquired",
    ]

    for mode, expected_error in (
        ("tool_not_found", "Tool not found"),
        ("invalid_params", "Invalid tool arguments"),
    ):
        try:
            manager.call_tool(
                server_name="recoverable",
                tool_name="echo",
                arguments={"mode": mode},
                workspace=tmp_path,
            )
        except ValueError as exc:
            assert expected_error in str(exc)
        else:
            raise AssertionError("expected recoverable MCP call failure")

        retry = manager.call_tool(
            server_name="recoverable",
            tool_name="echo",
            arguments={"text": mode},
            workspace=tmp_path,
        )

        assert retry.content == [{"type": "text", "text": f"echo:{mode}"}]
        failure_events = manager.drain_events()
        failed_event = next(
            event for event in failure_events if event.event_type == "runtime.mcp_server_failed"
        )
        assert failed_event.payload["stage"] == "call"
        assert failed_event.payload["method"] == "tools/call"


def test_mcp_manager_keeps_failure_events_coherent_during_concurrent_drains(
    tmp_path: Path,
) -> None:
    server_script = tmp_path / "concurrent_failure_mcp_server.py"
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
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "concurrent-failure", "version": "0.1.0"},
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
                            "name": "fail",
                            "description": "Always fails recoverably.",
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
                "error": {"code": -32602, "message": "Invalid tool arguments"},
            }
        )
        continue
""",
        encoding="utf-8",
    )
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "concurrent_failure": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                )
            },
        )
    )
    assert [tool.tool_name for tool in manager.list_tools(workspace=tmp_path)] == ["fail"]
    _ = manager.drain_events()

    worker_count = 12
    finished = threading.Event()
    drained_events: list[McpRuntimeEvent] = []
    drained_events_lock = threading.Lock()
    errors: list[BaseException] = []

    def call_failure() -> None:
        try:
            manager.call_tool(
                server_name="concurrent_failure",
                tool_name="fail",
                arguments={},
                workspace=tmp_path,
            )
        except ValueError as exc:
            assert "Invalid tool arguments" in str(exc)
        except BaseException as exc:  # pragma: no cover - re-raised below
            errors.append(exc)
        else:  # pragma: no cover - indicates the fake server stopped failing
            errors.append(AssertionError("expected recoverable MCP call failure"))

    def drain_until_finished() -> None:
        while not finished.is_set():
            drained = manager.drain_events()
            if drained:
                with drained_events_lock:
                    drained_events.extend(drained)
            time.sleep(0.001)
        drained = manager.drain_events()
        if drained:
            with drained_events_lock:
                drained_events.extend(drained)

    drainer = threading.Thread(target=drain_until_finished)
    threads = [threading.Thread(target=call_failure) for _ in range(worker_count)]
    drainer.start()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3.0)
    finished.set()
    drainer.join(timeout=3.0)

    if errors:
        raise errors[0]
    assert all(not thread.is_alive() for thread in threads)
    assert not drainer.is_alive()

    failure_events = [
        event for event in drained_events if event.event_type == "runtime.mcp_server_failed"
    ]
    assert len(failure_events) == worker_count
    assert {event.payload["stage"] for event in failure_events} == {"call"}
    assert {event.payload["method"] for event in failure_events} == {"tools/call"}
    assert manager.current_state().servers["concurrent_failure"].status == "failed"
    assert [tool.tool_name for tool in manager.list_tools(workspace=tmp_path)] == ["fail"]


def test_mcp_manager_emits_failure_event_when_startup_command_is_missing(tmp_path: Path) -> None:
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "missing": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=("voidcode-mcp-missing-binary",),
                )
            },
        )
    )

    try:
        manager.list_tools(workspace=tmp_path)
    except ValueError as exc:
        assert "command not found" in str(exc)
    else:
        raise AssertionError("expected MCP startup to fail for missing command")

    failure_events = manager.drain_events()
    assert [event.event_type for event in failure_events] == ["runtime.mcp_server_failed"]
    assert failure_events[0].payload["server"] == "missing"
    assert failure_events[0].payload["stage"] == "startup"
    assert failure_events[0].payload["command"] == ["voidcode-mcp-missing-binary"]


def test_mcp_manager_redacts_failure_event_command_secrets(tmp_path: Path) -> None:
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "missing": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(
                        "voidcode-mcp-missing-binary",
                        "--api-key",
                        "secret-token",
                        "ACCESS_TOKEN=other-secret",
                    ),
                )
            },
        )
    )

    with pytest.raises(ValueError, match="command not found"):
        manager.list_tools(workspace=tmp_path)

    failure_event = manager.drain_events()[0]
    assert failure_event.payload["command"] == [
        "voidcode-mcp-missing-binary",
        "--api-key",
        "<redacted>",
        "ACCESS_TOKEN=<redacted>",
    ]
    assert failure_event.payload["cmd"] == failure_event.payload["command"]
    assert "secret-token" not in str(failure_event.payload)
    assert "other-secret" not in str(failure_event.payload)


def test_mcp_manager_preserves_failed_state_after_startup_failure_stop_event(
    tmp_path: Path,
) -> None:
    server_script = tmp_path / "silent_mcp_server.py"
    server_script.write_text(
        r"""
from __future__ import annotations

import time

while True:
    time.sleep(10)
""",
        encoding="utf-8",
    )

    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            request_timeout_seconds=0.2,
            servers={
                "silent": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                )
            },
        )
    )

    with pytest.raises(ValueError, match="timed out"):
        manager.list_tools(workspace=tmp_path)

    state = manager.current_state()
    server_state = state.servers["silent"]

    assert server_state.status == "failed"
    assert server_state.stage == "startup"
    assert server_state.retry_available is True
    assert server_state.workspace_root == str(tmp_path)


def test_mcp_manager_starts_runtime_scoped_server_once_under_concurrent_discovery(
    tmp_path: Path,
) -> None:
    starts_path = tmp_path / "starts.txt"
    server_script = tmp_path / "concurrent_mcp_server.py"
    server_script.write_text(
        rf'''
from __future__ import annotations

import json
import pathlib
import sys

starts = pathlib.Path(r"{starts_path}")
starts.write_text(
    starts.read_text(encoding="utf-8") + "start\n" if starts.exists() else "start\n",
    encoding="utf-8",
)


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "protocolVersion": "2025-11-25",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "concurrent", "version": "0.1.0"}},
                }},
            }}
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "tools": [
                        {{
                            "name": "echo",
                            "description": "Echo.",
                            "inputSchema": {{"type": "object"}},
                        }}
                    ]
                }},
            }}
        )
        continue
''',
        encoding="utf-8",
    )
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "concurrent": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                )
            },
        )
    )
    errors: list[BaseException] = []

    def discover() -> None:
        try:
            assert [tool.tool_name for tool in manager.list_tools(workspace=tmp_path)] == ["echo"]
        except BaseException as exc:  # pragma: no cover - re-raised below
            errors.append(exc)

    threads = [threading.Thread(target=discover) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    if errors:
        raise errors[0]
    assert starts_path.read_text(encoding="utf-8").splitlines() == ["start"]
    event_types = [event.event_type for event in manager.drain_events()]
    assert event_types.count("runtime.mcp_server_started") == 1
    assert "runtime.mcp_server_reused" in event_types


def test_mcp_manager_keys_session_scoped_servers_by_owner_and_releases_one_owner(
    tmp_path: Path,
) -> None:
    starts_path = tmp_path / "session-starts.txt"
    stops_path = tmp_path / "session-stops.txt"
    server_script = tmp_path / "session_scoped_mcp_server.py"
    server_script.write_text(
        rf'''
from __future__ import annotations

import json
import pathlib
import sys

starts = pathlib.Path(r"{starts_path}")
stops = pathlib.Path(r"{stops_path}")
starts.write_text(
    starts.read_text(encoding="utf-8") + "start\n" if starts.exists() else "start\n",
    encoding="utf-8",
)


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "protocolVersion": "2025-11-25",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "session", "version": "0.1.0"}},
                }},
            }}
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "tools": [
                        {{
                            "name": "echo",
                            "description": "Echo.",
                            "inputSchema": {{"type": "object"}},
                        }}
                    ]
                }},
            }}
        )
        continue

stops.write_text(
    stops.read_text(encoding="utf-8") + "stop\n" if stops.exists() else "stop\n",
    encoding="utf-8",
)
''',
        encoding="utf-8",
    )
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "session": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                    scope="session",
                )
            },
        )
    )

    owner_a_tools = manager.list_tools(workspace=tmp_path, owner_session_id="a")
    owner_b_tools = manager.list_tools(workspace=tmp_path, owner_session_id="b")

    assert [tool.tool_name for tool in owner_a_tools] == ["echo"]
    assert [tool.tool_name for tool in owner_b_tools] == ["echo"]
    assert starts_path.read_text(encoding="utf-8").splitlines() == ["start", "start"]

    release_session = cast(Any, manager).release_session
    released_events: tuple[McpRuntimeEvent, ...] = release_session(session_id="a")

    assert [event.event_type for event in released_events][-2:] == [
        "runtime.mcp_server_released",
        "runtime.mcp_server_stopped",
    ]
    assert manager.current_state().servers["session"].status == "running"
    assert stops_path.read_text(encoding="utf-8").splitlines() == ["stop"]
    assert [
        tool.tool_name for tool in manager.list_tools(workspace=tmp_path, owner_session_id="b")
    ] == ["echo"]

    released_b_events: tuple[McpRuntimeEvent, ...] = release_session(session_id="b")

    assert [event.event_type for event in released_b_events][-2:] == [
        "runtime.mcp_server_released",
        "runtime.mcp_server_stopped",
    ]
    assert manager.current_state().servers["session"].status == "stopped"
    assert stops_path.read_text(encoding="utf-8").splitlines() == ["stop", "stop"]


def test_mcp_manager_idle_cleans_abandoned_session_scoped_servers(tmp_path: Path) -> None:
    server_script = tmp_path / "idle_mcp_server.py"
    server_script.write_text(
        r"""
from __future__ import annotations

import json
import sys


def send(message: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


for raw_line in sys.stdin:
    message = json.loads(raw_line)
    method = message.get("method")
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "idle", "version": "0.1.0"},
                },
            }
        )
        continue
    if method == "notifications/initialized":
        continue
    if method == "tools/list":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"tools": []}})
        continue
""",
        encoding="utf-8",
    )
    manager = build_mcp_manager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                "idle": RuntimeMcpServerConfig(
                    transport="stdio",
                    command=(sys.executable, str(server_script)),
                    scope="session",
                )
            },
        )
    )

    assert manager.list_tools(workspace=tmp_path, owner_session_id="abandoned") == ()
    _ = manager.drain_events()

    cleanup_idle_session_servers = cast(Any, manager).cleanup_idle_session_servers
    cleanup_events: tuple[McpRuntimeEvent, ...] = cleanup_idle_session_servers(
        max_idle_seconds=0,
        active_session_ids=set(),
    )

    assert [event.event_type for event in cleanup_events] == [
        "runtime.mcp_server_idle_cleaned",
        "runtime.mcp_server_stopped",
    ]
    assert cleanup_events[0].payload["owner_session_id"] == "abandoned"
    assert cleanup_events[0].payload["reason"] == "abandoned"
