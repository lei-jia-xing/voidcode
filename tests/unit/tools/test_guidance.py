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


def test_steer_task_guidance_describes_parent_and_lifecycle_rules() -> None:
    filename = guidance_filename_for_tool("steer_task")
    assert filename == "steer_task.txt"
    guidance = guidance_for_tool("steer_task")
    assert "parent session" in guidance
    assert "idle" in guidance
    assert "interrupted" in guidance
    assert "cannot be steered" in guidance


def test_background_output_guidance_describes_three_selectors_and_wait_semantics() -> None:
    guidance = guidance_for_tool("background_output")
    for selector in ("task_id", "task_ids", "parallel_group_id"):
        assert selector in guidance
    assert "block=false" in guidance
    assert "block=true" in guidance
    assert "milliseconds" in guidance
    assert "full_session=true" in guidance


def test_guidance_loader_returns_empty_for_unknown_tool() -> None:
    assert guidance_filename_for_tool("unknown_tool") is None
    assert guidance_for_tool("unknown_tool") == ""
