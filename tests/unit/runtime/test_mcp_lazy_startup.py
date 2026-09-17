"""Runtime-owned MCP laziness: the cache makes runs cheap, never the surface.

The rule under test: a run whose configured MCP servers are all covered by a
previous discovery connects to nothing; a cold install (or a reconfigured
server) discovers once, synchronously, at run start, so the first turn still
sees the configured MCP tools; discovery failures stay run-start diagnostics.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from voidcode.graph.contracts import GraphEvent, GraphStep
from voidcode.mcp import McpToolDescriptor
from voidcode.runtime.config import (
    RuntimeAgentConfig,
    RuntimeConfig,
    RuntimeMcpConfig,
    RuntimeMcpServerConfig,
    RuntimeToolsConfig,
)
from voidcode.runtime.contracts import RuntimeRequest
from voidcode.runtime.mcp_tool_cache import McpToolCatalogCache, mcp_server_identity
from voidcode.runtime.paths import mcp_tool_catalog_cache_path
from voidcode.runtime.permission import PermissionPolicy
from voidcode.runtime.service import GraphRunRequest, SessionState, VoidCodeRuntime
from voidcode.tools.contracts import ToolCall, ToolResult

_SERVER_SCRIPT = r"""
from __future__ import annotations

import json
import pathlib
import sys

pathlib.Path(r"{marker}").write_text("started", encoding="utf-8")


def send(message):
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
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "protocolVersion": "2025-11-25",
                    "capabilities": {{"tools": {{}}}},
                    "serverInfo": {{"name": "echo-mcp", "version": "0.1.0"}},
                }},
            }}
        )
    elif method == "notifications/initialized":
        continue
    elif method == "tools/list":
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "tools": [
                        {{
                            "name": "echo",
                            "description": "Echo the text argument.",
                            "annotations": {{"readOnlyHint": True, "destructiveHint": False}},
                            "inputSchema": {{
                                "type": "object",
                                "properties": {{"text": {{"type": "string"}}}},
                                "required": ["text"],
                            }},
                        }}
                    ]
                }},
            }}
        )
    elif method == "tools/call":
        params = message.get("params") or {{}}
        arguments = params.get("arguments") or {{}}
        send(
            {{
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {{
                    "content": [{{"type": "text", "text": "echo:" + str(arguments.get("text"))}}],
                    "isError": False,
                }},
            }}
        )
"""


@dataclass(slots=True)
class _StubStep:
    tool_call: ToolCall | None = None
    output: str | None = None
    events: tuple[GraphEvent, ...] = ()
    is_finished: bool = False


class _ToolCallGraph:
    """Deterministic stand-in for a provider turn (optionally calling one tool)."""

    def __init__(self, tool_name: str | None = None, arguments: dict[str, object] | None = None) -> None:
        self._tool_name = tool_name
        self._arguments = dict(arguments or {})
        self.first_turn_tools: tuple[str, ...] = ()

    def step(
        self,
        request: GraphRunRequest,
        tool_results: tuple[ToolResult, ...],
        *,
        session: SessionState,
    ) -> GraphStep:
        _ = session
        if not tool_results:
            self.first_turn_tools = tuple(definition.name for definition in request.available_tools)
            if self._tool_name is None:
                return _StubStep(output="done", is_finished=True)
            return _StubStep(tool_call=ToolCall(tool_name=self._tool_name, arguments=dict(self._arguments)))
        return _StubStep(output=tool_results[-1].content or "done", is_finished=True)


@dataclass(frozen=True, slots=True)
class _McpWorkspace:
    root: Path
    marker: Path
    server_config: RuntimeMcpServerConfig


def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, command: tuple[str, ...] | None = None) -> _McpWorkspace:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    marker = tmp_path / "server-started.marker"
    script = tmp_path / "echo_mcp_server.py"
    script.write_text(_SERVER_SCRIPT.format(marker=marker), encoding="utf-8")
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    return _McpWorkspace(
        root=tmp_path,
        marker=marker,
        server_config=RuntimeMcpServerConfig(
            transport="stdio",
            command=command if command is not None else (sys.executable, str(script)),
        ),
    )


@pytest.fixture
def mcp_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _McpWorkspace:
    """Workspace with a stdio MCP server that records when it is started."""
    return _workspace(tmp_path, monkeypatch)


def _with_command(workspace: _McpWorkspace, command: tuple[str, ...]) -> _McpWorkspace:
    """Same workspace/cache dirs, different configured server command."""
    return _McpWorkspace(
        root=workspace.root,
        marker=workspace.marker,
        server_config=RuntimeMcpServerConfig(transport="stdio", command=command),
    )


def _runtime(
    workspace: _McpWorkspace,
    *,
    enabled: bool = True,
    agent: RuntimeAgentConfig | None = None,
    graph: object | None = None,
) -> VoidCodeRuntime:
    return VoidCodeRuntime(
        workspace=workspace.root,
        config=RuntimeConfig(
            execution_engine="deterministic",
            mcp=RuntimeMcpConfig(enabled=enabled, servers={"echo": workspace.server_config}),
            agent=agent,
        ),
        graph=graph,  # type: ignore[arg-type]
        permission_policy=PermissionPolicy(mode="allow"),
    )


def _seed_catalog(workspace: _McpWorkspace) -> None:
    """Persist a discovered surface exactly as an earlier run would have."""
    McpToolCatalogCache(path=mcp_tool_catalog_cache_path()).store(
        server_name="echo",
        identity=mcp_server_identity("echo", workspace.server_config, workspace_root=workspace.root),
        descriptors=(
            McpToolDescriptor(
                server_name="echo",
                tool_name="echo",
                description="Echo the text argument.",
                input_schema={"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            ),
        ),
    )


def test_cold_install_discovers_once_so_the_first_turn_sees_the_surface(mcp_workspace: _McpWorkspace) -> None:
    graph = _ToolCallGraph()  # no tool call at all: the configured server is unused
    runtime = _runtime(mcp_workspace, graph=graph)

    response = runtime.run(RuntimeRequest(prompt="hi", session_id="mcp-cold-unused"))

    assert response.output == "done"
    assert mcp_workspace.marker.exists() is True
    registry = runtime.tool_registry_for_effective_config(runtime.effective_runtime_config())
    assert "mcp/echo/echo" in registry.tools
    assert "mcp/echo/echo" in graph.first_turn_tools


def test_warm_catalog_makes_run_start_connect_free(mcp_workspace: _McpWorkspace) -> None:
    _seed_catalog(mcp_workspace)

    graph = _ToolCallGraph()
    runtime = _runtime(mcp_workspace, graph=graph)

    response = runtime.run(RuntimeRequest(prompt="hi", session_id="mcp-warm-unused"))

    assert response.output == "done"
    assert mcp_workspace.marker.exists() is False
    assert "mcp/echo/echo" in graph.first_turn_tools


def test_reconfigured_server_discovers_once_again(mcp_workspace: _McpWorkspace) -> None:
    _seed_catalog(mcp_workspace)
    other_script = mcp_workspace.root / "other_server.py"
    other_script.write_text(_SERVER_SCRIPT.format(marker=mcp_workspace.marker), encoding="utf-8")
    changed = _with_command(mcp_workspace, (sys.executable, str(other_script)))

    runtime = _runtime(changed, graph=_ToolCallGraph())
    _ = runtime.run(RuntimeRequest(prompt="hi", session_id="mcp-reconfigured"))

    assert changed.marker.exists() is True


def test_mcp_tool_call_on_a_cold_install_succeeds(mcp_workspace: _McpWorkspace) -> None:
    graph = _ToolCallGraph("mcp/echo/echo", {"text": "hi"})
    runtime = _runtime(mcp_workspace, graph=graph)

    response = runtime.run(RuntimeRequest(prompt="hi", session_id="mcp-cold-used"))

    completed = [event for event in response.events if event.event_type == "runtime.tool_completed"]
    assert completed
    assert completed[0].payload["content"] == "echo:hi"
    assert completed[0].payload["status"] == "ok"
    assert "mcp/echo/echo" in graph.first_turn_tools
    assert mcp_workspace.marker.exists() is True


def test_cached_mcp_catalog_covers_a_server_that_exposes_no_tools(mcp_workspace: _McpWorkspace) -> None:
    McpToolCatalogCache(path=mcp_tool_catalog_cache_path()).store(
        server_name="echo",
        identity=mcp_server_identity("echo", mcp_workspace.server_config, workspace_root=mcp_workspace.root),
        descriptors=(),
    )

    runtime = _runtime(mcp_workspace, graph=_ToolCallGraph())
    _ = runtime.run(RuntimeRequest(prompt="hi", session_id="mcp-warm-empty"))

    # A known-empty surface is still a covered server: no reconnect.
    assert mcp_workspace.marker.exists() is False


def test_cold_install_mcp_discovery_failure_is_a_run_start_diagnostic(mcp_workspace: _McpWorkspace) -> None:
    broken = _with_command(mcp_workspace, ("voidcode-missing-mcp-binary",))
    runtime = _runtime(broken, graph=_ToolCallGraph())

    response = runtime.run(RuntimeRequest(prompt="hi", session_id="mcp-cold-failed"))

    assert response.output == "done"
    failure_events = [event for event in response.events if event.event_type == "runtime.mcp_server_failed"]
    assert failure_events
    assert failure_events[0].payload["state"] == "failed"
    assert "voidcode-missing-mcp-binary" in str(failure_events[0].payload.get("error"))


def test_disabled_mcp_never_starts_the_server(mcp_workspace: _McpWorkspace) -> None:
    runtime = _runtime(mcp_workspace, enabled=False, graph=_ToolCallGraph("mcp/echo/echo", {"text": "hi"}))

    with pytest.raises(ValueError, match="unknown tool"):
        _ = runtime.run(RuntimeRequest(prompt="hi", session_id="mcp-lazy-disabled"))

    assert mcp_workspace.marker.exists() is False


def test_discovered_surface_cannot_widen_the_agent_tool_scope(mcp_workspace: _McpWorkspace) -> None:
    restricted_agent = RuntimeAgentConfig(preset="leader", tools=RuntimeToolsConfig(allowlist=("read",)))
    graph = _ToolCallGraph("mcp/echo/echo", {"text": "hi"})
    runtime = _runtime(mcp_workspace, agent=restricted_agent, graph=graph)

    response_events: list[str] = []
    with pytest.raises(ValueError, match="unknown tool"):
        for chunk in runtime.run_stream(RuntimeRequest(prompt="hi", session_id="mcp-lazy-scoped")):
            if chunk.event is not None:
                response_events.append(chunk.event.event_type)

    assert "runtime.tool_completed" not in response_events
    assert "mcp/echo/echo" not in graph.first_turn_tools
