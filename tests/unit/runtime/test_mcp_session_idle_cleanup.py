"""Runtime-owned MCP idle reaping: threshold, ownership, and release evidence.

The manager owns *which* session-scoped servers count as reapable; run start
owns *when* the sweep runs (see `tests/integration/test_mcp_session_idle_lifecycle.py`
for the end-to-end seam). These tests drive the manager directly with an
injected clock, so idle is decided without waiting on wall time.
"""

from __future__ import annotations

from pathlib import Path

from tests.fake_mcp_stdio_server import FakeMcpStdioServer, write_fake_mcp_stdio_server
from voidcode.mcp.types import McpRuntimeEvent
from voidcode.runtime.config import RuntimeMcpConfig, RuntimeMcpServerConfig
from voidcode.runtime.mcp import (
    DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS,
    ManagedMcpManager,
)

_SERVER_NAME = "idle"


class _FakeClock:
    """A monotonic clock the test advances by hand."""

    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _manager(server: FakeMcpStdioServer, clock: _FakeClock) -> ManagedMcpManager:
    return ManagedMcpManager(
        RuntimeMcpConfig(
            enabled=True,
            servers={
                _SERVER_NAME: RuntimeMcpServerConfig(
                    transport="stdio",
                    command=server.command,
                    scope="session",
                )
            },
        ),
        clock=clock,
    )


def _tool_names(manager: ManagedMcpManager, tmp_path: Path, *, owner: str) -> list[str]:
    return [tool.tool_name for tool in manager.list_tools(workspace=tmp_path, owner_session_id=owner)]


def _reaped(events: tuple[McpRuntimeEvent, ...]) -> McpRuntimeEvent:
    idle_events = [event for event in events if event.event_type == "runtime.mcp_server_idle_cleaned"]
    assert len(idle_events) == 1, [event.event_type for event in events]
    return idle_events[0]


def test_session_server_idle_past_ttl_is_released(tmp_path: Path) -> None:
    clock = _FakeClock()
    server = write_fake_mcp_stdio_server(tmp_path)
    manager = _manager(server, clock)

    assert _tool_names(manager, tmp_path, owner="owner") == ["echo"]
    _ = manager.drain_events()

    clock.advance(DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS)

    events = manager.cleanup_idle_session_servers(
        max_idle_seconds=DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS,
        active_session_ids={"owner"},
    )

    assert [event.event_type for event in events] == [
        "runtime.mcp_server_idle_cleaned",
        "runtime.mcp_server_stopped",
    ]
    payload = _reaped(events).payload
    assert payload["server"] == _SERVER_NAME
    assert payload["scope"] == "session"
    assert payload["owner_session_id"] == "owner"
    assert payload["reason"] == "idle_timeout"
    assert payload["cleaned_count"] == 1
    assert manager.current_state().servers[_SERVER_NAME].status == "stopped"

    # The released server is gone, not hidden: the next use starts a new process.
    assert _tool_names(manager, tmp_path, owner="owner") == ["echo"]
    assert server.start_count == 2


def test_session_server_before_ttl_is_kept(tmp_path: Path) -> None:
    clock = _FakeClock()
    server = write_fake_mcp_stdio_server(tmp_path)
    manager = _manager(server, clock)

    assert _tool_names(manager, tmp_path, owner="owner") == ["echo"]
    _ = manager.drain_events()

    clock.advance(DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS - 1.0)

    events = manager.cleanup_idle_session_servers(
        max_idle_seconds=DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS,
        active_session_ids={"owner"},
    )

    assert events == ()
    assert manager.current_state().servers[_SERVER_NAME].status == "running"

    # Same process: the kept server is reused instead of restarted.
    assert _tool_names(manager, tmp_path, owner="owner") == ["echo"]
    assert server.start_count == 1


def test_active_session_server_is_not_reaped_as_abandoned(tmp_path: Path) -> None:
    clock = _FakeClock()
    server = write_fake_mcp_stdio_server(tmp_path)
    manager = _manager(server, clock)

    assert _tool_names(manager, tmp_path, owner="live") == ["echo"]
    assert _tool_names(manager, tmp_path, owner="gone") == ["echo"]
    _ = manager.drain_events()

    events = manager.cleanup_idle_session_servers(
        max_idle_seconds=DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS,
        active_session_ids={"live"},
    )

    assert [event.event_type for event in events] == [
        "runtime.mcp_server_idle_cleaned",
        "runtime.mcp_server_stopped",
    ]
    payload = _reaped(events).payload
    assert payload["owner_session_id"] == "gone"
    assert payload["reason"] == "abandoned"
    assert payload["cleaned_count"] == 1

    # The live owner keeps its process...
    assert _tool_names(manager, tmp_path, owner="live") == ["echo"]
    assert server.start_count == 2

    # ...while the abandoned owner's server is genuinely gone and restarts on demand.
    assert _tool_names(manager, tmp_path, owner="gone") == ["echo"]
    assert server.start_count == 3


def test_cleanup_without_active_session_ids_is_idle_only(tmp_path: Path) -> None:
    clock = _FakeClock()
    server = write_fake_mcp_stdio_server(tmp_path)
    manager = _manager(server, clock)

    assert _tool_names(manager, tmp_path, owner="owner") == ["echo"]
    _ = manager.drain_events()

    # No ownership truth: a recently used server survives even though its owner
    # is not listed anywhere.
    assert (
        manager.cleanup_idle_session_servers(
            max_idle_seconds=DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS,
            active_session_ids=None,
        )
        == ()
    )

    clock.advance(DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS)

    events = manager.cleanup_idle_session_servers(
        max_idle_seconds=DEFAULT_SESSION_MCP_IDLE_TIMEOUT_SECONDS,
        active_session_ids=None,
    )
    assert _reaped(events).payload["reason"] == "idle_timeout"
