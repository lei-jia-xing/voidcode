"""Passive inventories retain unknown discovery and connection diagnostics."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from voidcode.plugins.mcp.inventory import McpInventoryCommand, context_contribution
from voidcode.plugins.mcp.types import McpCachedToolSurface, McpConfigState, McpManagerState, McpServerConfig, McpServerRuntimeState
from voidcode.sdk.commands import CommandInvocation
from voidcode.sdk.invocation import ToolContext


@pytest.mark.parametrize("discovered", [False, True])
def test_inventory_distinguishes_unknown_from_known_empty_without_connecting(tmp_path: Path, discovered: bool):
    state = McpManagerState(
        configuration=McpConfigState(configured_enabled=True, servers={"demo": McpServerConfig(command=["unused"])}),
        servers={"demo": McpServerRuntimeState(status="failed", error="startup failed")},
    )
    manager = SimpleNamespace(current_state=lambda: state, cached_surface=lambda **kwargs: McpCachedToolSurface({"demo": ()} if discovered else {}))
    context = ToolContext(workspace=tmp_path, session_id="session", run_id="run", invocation_id="inv", revision_id="rev", actor="client")
    result = McpInventoryCommand(manager).invoke(CommandInvocation(context=context, name="mcp"), None)
    assert result.status == "ok"
    assert result.data["servers"][0]["tools_known"] == (0 if discovered else None)
    assert result.data["servers"][0]["error"] == "startup failed"
    assert "startup failed" in result.output
    expected = "no tools" if discovered else "not discovered"
    assert expected in result.output
    assert expected in context_contribution(manager, context).content
