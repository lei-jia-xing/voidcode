from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from voidcode.runtime.tool_materializer import RuntimeToolMaterializer
from voidcode.runtime.tool_registry import ToolRegistry
from voidcode.tools.contracts import ToolCall, ToolDefinition, ToolResult
from voidcode.tools.local_custom import LocalCustomTool, LocalCustomToolManifest


@dataclass(frozen=True)
class _Tool:
    name: str
    marker: str

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            description=self.marker,
            input_schema={"type": "object"},
            read_only=True,
        )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        _ = call, workspace
        return ToolResult(tool_name=self.name, status="ok", content=self.marker)


def test_materializer_returns_base_tools_in_a_fresh_runtime_registry() -> None:
    base_tool = _Tool("base", "base")
    base = ToolRegistry.from_tools((base_tool,))

    materialization = RuntimeToolMaterializer(base).materialize_mcp_tools(())

    assert materialization.registry is not base
    assert materialization.registry.resolve("base") is base_tool


def test_materializer_adds_mcp_tools_without_mutating_base_registry() -> None:
    base = ToolRegistry.from_tools((_Tool("base", "base"),))
    mcp_tool = _Tool("mcp/server/tool", "mcp")

    materialization = RuntimeToolMaterializer(base).materialize_mcp_tools((mcp_tool,))

    assert materialization.registry.resolve("mcp/server/tool") is mcp_tool
    assert "mcp/server/tool" not in base.tools


def test_materializer_preserves_mcp_override_semantics() -> None:
    base_tool = _Tool("shared", "base")
    mcp_tool = _Tool("shared", "mcp")
    base = ToolRegistry.from_tools((base_tool,))

    materialization = RuntimeToolMaterializer(base).materialize_mcp_tools((mcp_tool,))

    assert materialization.registry.resolve("shared") is mcp_tool
    assert base.resolve("shared") is base_tool


def test_materializer_records_source_provenance_and_stable_generation() -> None:
    base = ToolRegistry.from_tools((_Tool("base", "base"),))
    materializer = RuntimeToolMaterializer(base)

    first = materializer.materialize_mcp_tools((_Tool("mcp/tool", "mcp"),))
    second = materializer.materialize_mcp_tools((_Tool("mcp/tool", "mcp"),))

    assert first.generation == second.generation
    assert [(item.tool_name, item.source_kind) for item in first.provenance] == [
        ("base", "base"),
        ("mcp/tool", "mcp"),
    ]


def test_materializer_generation_changes_when_capability_definition_changes() -> None:
    base = ToolRegistry.from_tools((_Tool("base", "base"),))
    materializer = RuntimeToolMaterializer(base)

    first = materializer.materialize_mcp_tools((_Tool("mcp/tool", "one"),))
    second = materializer.materialize_mcp_tools((_Tool("mcp/tool", "two"),))

    assert first.generation != second.generation


def test_materializer_generation_includes_local_manifest_command(tmp_path: Path) -> None:
    manifest_path = tmp_path / "echo.json"

    def local_tool(command: tuple[str, ...]) -> LocalCustomTool:
        return LocalCustomTool(
            LocalCustomToolManifest(
                name="local/echo",
                description="echo",
                input_schema={"type": "object"},
                command=command,
                read_only=True,
                manifest_path=manifest_path,
            )
        )

    base = ToolRegistry.from_tools((_Tool("base", "base"),))
    materializer = RuntimeToolMaterializer(base)

    first = materializer.materialize_local_tools(
        materializer.base(),
        (local_tool(("python", "echo.py")),),
    )
    second = materializer.materialize_local_tools(
        materializer.base(),
        (local_tool(("python", "different.py")),),
    )

    assert first.generation != second.generation


def test_materializer_adds_local_tools_without_mutating_runtime_registry() -> None:
    runtime_tool = _Tool("runtime", "runtime")
    local_tool = _Tool("local/tool", "local")
    runtime_registry = ToolRegistry.from_tools((runtime_tool,))

    materialization = RuntimeToolMaterializer.materialize_local_tools(
        RuntimeToolMaterializer(runtime_registry).base(),
        (local_tool,),
    )

    assert materialization.registry.resolve("local/tool") is local_tool
    assert "local/tool" not in runtime_registry.tools


def test_materializer_rejects_local_tool_name_collisions() -> None:
    runtime_registry = ToolRegistry.from_tools((_Tool("shared", "runtime"),))

    with pytest.raises(ValueError, match="duplicate tool definition: shared"):
        RuntimeToolMaterializer.materialize_local_tools(
            RuntimeToolMaterializer(runtime_registry).base(),
            (_Tool("shared", "local"),),
        )
