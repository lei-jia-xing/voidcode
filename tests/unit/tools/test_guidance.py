from voidcode.tools.guidance import (
    guidance_filename_for_tool,
    guidance_for_tool,
    load_tool_guidance,
)


def test_guidance_loader_returns_complete_builtin_sidecar() -> None:
    filename = guidance_filename_for_tool("read")
    assert filename == "read.txt"
    guidance = guidance_for_tool("read")
    assert guidance
    assert guidance == load_tool_guidance(filename)
    assert "Internal documentation URLs:" in guidance


def test_guidance_loader_maps_dynamic_mcp_tools_to_shared_sidecar() -> None:
    assert guidance_filename_for_tool("mcp/server/tool") == "mcp.txt"
    guidance = guidance_for_tool("mcp/server/tool")
    assert guidance


def test_yield_guidance_describes_terminal_child_handoff() -> None:
    filename = guidance_filename_for_tool("yield")
    assert filename == "yield.txt"
    guidance = guidance_for_tool("yield")
    assert "delegated child session" in guidance
    assert "summary" in guidance
    assert "data" in guidance
    assert "incremental" in guidance
    assert "peer message bus" in guidance


def test_background_task_guidance_describes_operations_and_lifecycle_rules() -> None:
    filename = guidance_filename_for_tool("background_task")
    assert filename == "background_task.txt"
    guidance = guidance_for_tool("background_task")
    for operation in ("output", "cancel", "ps", "steer"):
        assert f'operation="{operation}"' in guidance
    for selector in ("task_id", "task_ids", "parallel_group_id"):
        assert selector in guidance
    assert "block=false" in guidance
    assert "block=true" in guidance
    assert "milliseconds" in guidance
    assert "full_session=true" in guidance


def test_guidance_loader_returns_empty_for_unknown_tool() -> None:
    assert guidance_filename_for_tool("unknown_tool") is None
    assert guidance_for_tool("unknown_tool") == ""
