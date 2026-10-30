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


@pytest.mark.parametrize("discovered", [False, True])
def test_inventory_selector_opens_passive_server_details(tmp_path: Path, discovered: bool):
    from voidcode.sdk.views import ChoicesViewData, MarkdownViewData, choices_dispatch

    state = McpManagerState(
        configuration=McpConfigState(configured_enabled=True, servers={"demo": McpServerConfig(command=["unused"])}),
        servers={"demo": McpServerRuntimeState(status="failed", error="startup failed")},
    )
    manager = SimpleNamespace(current_state=lambda: state, cached_surface=lambda **kwargs: McpCachedToolSurface({"demo": ()} if discovered else {}))
    context = ToolContext(workspace=tmp_path, session_id="session", run_id="run", invocation_id="inv", revision_id="rev", actor="client")
    command = McpInventoryCommand(manager)
    listed = command.invoke(CommandInvocation(context=context, name="mcp"), None)
    action, arguments = choices_dispatch(ChoicesViewData.from_payload(listed.presentation.data), "demo")
    detail = command.invoke(CommandInvocation(context=context, name=action, arguments=arguments), None)
    view = MarkdownViewData.from_payload(detail.presentation.data)
    assert "startup failed" in view.content
    assert ("Tools: 0" if discovered else "Tools: not discovered") in view.content
    assert view.actions[0].action == "mcp"
    missing = command.invoke(CommandInvocation(context=context, name="mcp", arguments={"server": "missing"}), None)
    assert missing.status == "error" and "Reopen /mcp" in missing.error


def test_inventory_pages_all_cached_tools_without_discovery(tmp_path: Path):
    from voidcode.plugins.mcp.types import McpToolDescriptor
    from voidcode.sdk.views import MarkdownViewData

    tools = tuple(McpToolDescriptor(server_name="demo", tool_name=f"tool-{index}", description="🙂" * 200, input_schema={}) for index in range(61))
    state = McpManagerState(configuration=McpConfigState(configured_enabled=True, servers={"demo": McpServerConfig(command=["unused"])}))
    manager = SimpleNamespace(current_state=lambda: state, cached_surface=lambda **kwargs: McpCachedToolSurface({"demo": tools}))
    context = ToolContext(workspace=tmp_path, session_id="session", run_id="run", invocation_id="inv", revision_id="rev", actor="client")
    command = McpInventoryCommand(manager)
    arguments = {"server": "demo"}
    seen = []
    while True:
        result = command.invoke(CommandInvocation(context=context, name="mcp", arguments=arguments), None)
        view = MarkdownViewData.from_payload(result.presentation.data)
        seen.extend(line.split(" [")[0][2:] for line in view.content.splitlines() if line.startswith("- tool-"))
        next_page = next((action for action in view.actions if action.label == "Next tools"), None)
        if next_page is None:
            break
        arguments = dict(next_page.arguments)
    assert seen == [f"tool-{index}" for index in range(61)]
