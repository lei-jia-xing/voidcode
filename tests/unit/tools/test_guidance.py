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


def test_guidance_loader_returns_empty_for_unknown_tool() -> None:
    assert guidance_filename_for_tool("unknown_tool") is None
    assert guidance_for_tool("unknown_tool") == ""
