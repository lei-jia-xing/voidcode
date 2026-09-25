"""`runtime.mcp_server_idle_cleaned` through a real runtime session lifecycle.

`cleanup_idle_session_servers` had no caller, so this event was unreachable: the
sweep only existed as manager behaviour plus a documented contract. These tests
drive a real `VoidCodeRuntime` run and pin the two things the wiring must do —
surface the reaped server in the run's stream/session events, and stay
housekeeping when the manager fails.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests.fake_mcp_stdio_server import write_fake_mcp_stdio_server
from voidcode.mcp.types import (
    McpConfigState,
    McpManagerState,
    McpRuntimeEvent,
    McpToolDescriptor,
)
from voidcode.runtime.config import RuntimeConfig, RuntimeMcpConfig, RuntimeMcpServerConfig
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.mcp import ManagedMcpManager
from voidcode.runtime.service import VoidCodeRuntime
from voidcode.runtime.storage import SqliteSessionStore

_SERVER_NAME = "idle"
_ABANDONED_SESSION_ID = "abandoned-session"
_RUN_SESSION_ID = "run-session"


@dataclass(frozen=True, slots=True)
class _FinishStep:
    tool_call: None = None
    output: str = "completed"
    events: tuple[object, ...] = ()
    is_finished: bool = True
    reasoning: str | None = None
    provider_usage: object | None = None


class _FinishingGraph:
    """One terminal step, so a run reaches the session-end write path."""

    def step(self, request: Any, tool_results: tuple[Any, ...], *, session: Any) -> _FinishStep:
        _ = request, tool_results, session
        return _FinishStep()


class _FailingCleanupMcpManager:
    """A manager whose idle sweep raises, to prove the runtime contains it."""

    def current_state(self) -> McpManagerState:
        return McpManagerState(mode="managed", configuration=McpConfigState(configured_enabled=True))

    def list_tools(self, **_: object) -> tuple[McpToolDescriptor, ...]:
        return ()

    def call_tool(self, **_: object) -> object:
        raise AssertionError("MCP tool calls are not used by this test")

    def drain_events(self) -> tuple[McpRuntimeEvent, ...]:
        return ()

    def release_session(self, *, session_id: str) -> tuple[McpRuntimeEvent, ...]:
        _ = session_id
        return ()

    def cleanup_idle_session_servers(
        self,
        *,
        max_idle_seconds: float,
        active_session_ids: set[str] | None = None,
    ) -> tuple[McpRuntimeEvent, ...]:
        _ = max_idle_seconds, active_session_ids
        raise RuntimeError("idle sweep exploded")

    def shutdown(self) -> tuple[McpRuntimeEvent, ...]:
        return ()


def _session_scoped_config(server_command: tuple[str, ...]) -> RuntimeConfig:
    return RuntimeConfig(
        mcp=RuntimeMcpConfig(
            enabled=True,
            servers={
                _SERVER_NAME: RuntimeMcpServerConfig(
                    transport="stdio",
                    command=server_command,
                    scope="session",
                )
            },
        ),
        execution_engine="deterministic",
    )


def _event_types(chunks: list[Any]) -> list[str]:
    return [chunk.event.event_type for chunk in chunks if chunk.event is not None]


def test_idle_cleanup_is_reachable_from_a_real_run(tmp_path: Path) -> None:
    server = write_fake_mcp_stdio_server(tmp_path)
    config = _session_scoped_config(server.command)
    manager = ManagedMcpManager(config.mcp)
    store = SqliteSessionStore()

    # A session-scoped server left behind by a session that is no longer running:
    # same shape a waiting session that was never resumed leaves in the manager.
    leftover_tools = manager.list_tools(workspace=tmp_path, owner_session_id=_ABANDONED_SESSION_ID)
    assert [tool.tool_name for tool in leftover_tools] == ["echo"]
    _ = manager.drain_events()

    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=store,
        config=config,
        graph=_FinishingGraph(),
        mcp_manager=manager,
    )

    chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=_RUN_SESSION_ID)))
    event_types = _event_types(chunks)

    assert "runtime.mcp_server_idle_cleaned" in event_types
    assert "runtime.failed" not in event_types

    reaped = [chunk.event for chunk in chunks if chunk.event is not None and chunk.event.event_type == "runtime.mcp_server_idle_cleaned"]
    assert len(reaped) == 1
    payload = reaped[0].payload
    assert payload["server"] == _SERVER_NAME
    assert payload["scope"] == "session"
    assert payload["owner_session_id"] == _ABANDONED_SESSION_ID
    assert payload["reason"] == "abandoned"
    assert payload["cleaned_count"] == 1

    # Durable, not just streamed: the reaped server is in the stored session events.
    stored_events = store.load_session(workspace=tmp_path, session_id=_RUN_SESSION_ID).events
    assert any(event.event_type == "runtime.mcp_server_idle_cleaned" for event in stored_events)
    assert manager.current_state().servers[_SERVER_NAME].status == "stopped"


def test_failing_idle_cleanup_leaves_the_run_healthy(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    config = RuntimeConfig(mcp=RuntimeMcpConfig(enabled=True), execution_engine="deterministic")
    runtime = VoidCodeRuntime(
        workspace=tmp_path,
        session_store=SqliteSessionStore(),
        config=config,
        graph=_FinishingGraph(),
        mcp_manager=_FailingCleanupMcpManager(),
    )

    with caplog.at_level(logging.WARNING, logger="voidcode.runtime.service"):
        chunks = list(runtime.run_stream(RuntimeRequest(prompt="go", session_id=_RUN_SESSION_ID)))

    assert [chunk.output for chunk in chunks if chunk.kind == "output"] == ["completed"]
    assert "runtime.failed" not in _event_types(chunks)
    assert any("idle MCP session cleanup failed" in record.getMessage() for record in caplog.records)
