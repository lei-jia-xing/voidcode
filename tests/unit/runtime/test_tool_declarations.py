from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from voidcode.core.tool_context import ToolContext
from voidcode.runtime.config import RuntimeMcpConfig, RuntimeMcpServerConfig
from voidcode.runtime.mcp import ManagedMcpManager
from voidcode.runtime.permission import PermissionPolicy, resolve_permission
from voidcode.runtime.permission_context import operation_class_for_tool
from voidcode.runtime.tool_materializer import RuntimeToolMaterializer
from voidcode.runtime.tool_provider import builtin_tool_definitions
from voidcode.runtime.tool_registry import ToolRegistry
from voidcode.security.json_values import json_wire_object
from voidcode.tools.contracts import TextOutput, ToolCall, ToolSuccess
from voidcode.tools.local_custom import LocalCustomTool, discover_local_custom_tool_manifests
from voidcode.tools.mcp import McpTool


def _manifest(workspace: Path, *, name: str, command: list[str]) -> Path:
    root = workspace / ".voidcode" / "tools"
    root.mkdir(parents=True, exist_ok=True)
    path = root / "command.json"
    path.write_text(
        json.dumps(
            {
                "name": name,
                "description": "Execute a configured local command.",
                "input_schema": {"type": "object", "properties": {}},
                "command": command,
                "read_only": False,
            }
        )
    )
    return path


def test_binding_rejects_same_name_with_different_real_capability(tmp_path: Path) -> None:
    _manifest(tmp_path, name="read", command=[sys.executable, "-c", "print(7 * 7)"])
    (manifest,) = discover_local_custom_tool_manifests(tmp_path, enabled=True)
    read = next(definition for definition in builtin_tool_definitions() if definition.name == "read")
    declared = ToolRegistry.from_definitions((read,))

    with pytest.raises(ValueError, match="materialized tool does not match declaration"):
        declared.bind(lambda _definition: LocalCustomTool(manifest))
    with pytest.raises(ValueError, match="tool is not bound"):
        declared.resolve("read")


def test_local_command_change_invalidates_materialization_generation(tmp_path: Path) -> None:
    path = _manifest(tmp_path, name="local/calculate", command=[sys.executable, "-c", "print(7 * 7)"])
    materializer = RuntimeToolMaterializer(ToolRegistry.from_definitions(()), ())
    first = materializer.materialize_local_manifests(materializer.base(), discover_local_custom_tool_manifests(tmp_path, enabled=True))
    payload = json.loads(path.read_text())
    payload["command"] = [sys.executable, "-c", "print(8 * 8)"]
    path.write_text(json.dumps(payload))
    second = materializer.materialize_local_manifests(materializer.base(), discover_local_custom_tool_manifests(tmp_path, enabled=True))

    assert first.registry.definitions() == second.registry.definitions()
    assert first.generation != second.generation


def test_observed_mcp_safety_drives_real_admission_and_sdk_call(tmp_path: Path) -> None:
    server = tmp_path / "server.py"
    server.write_text(
        "from mcp.server.mcpserver import MCPServer\n"
        "from mcp.types import ToolAnnotations\n"
        "server = MCPServer('declaration-test')\n"
        "@server.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False))\n"
        "def inspect_square(value: int) -> str:\n"
        "    return str(value * value)\n"
        "@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True))\n"
        "def persist_square(value: int) -> str:\n"
        "    from pathlib import Path\n"
        "    Path('square.txt').write_text(str(value * value))\n"
        "    return str(value * value)\n"
        "server.run(transport='stdio')\n"
    )
    manager = ManagedMcpManager(
        RuntimeMcpConfig(
            enabled=True,
            servers={"observed": RuntimeMcpServerConfig(command=(sys.executable, str(server)))},
            request_timeout_seconds=10,
        )
    )
    try:
        descriptors = manager.list_tools(workspace=tmp_path, server_name="observed")
        materializer = RuntimeToolMaterializer(ToolRegistry.from_definitions(()), ())
        observed = materializer.materialize_mcp_descriptors(descriptors)
        captured = {f"mcp/{descriptor.server_name}/{descriptor.tool_name}": descriptor for descriptor in descriptors}
        bound = observed.registry.bind(lambda definition: McpTool(captured[definition.name]))
        context = ToolContext(workspace=tmp_path, session_id="observed", mcp_request=manager.call_tool)
        for descriptor in descriptors:
            name = f"mcp/{descriptor.server_name}/{descriptor.tool_name}"
            tool = bound.resolve(name)
            call = ToolCall(tool_name=name, arguments={"value": 7})
            operation = operation_class_for_tool(name, tool.definition.effects, tool_instance=tool, arguments=json_wire_object(call.arguments))
            for mode in ("ask", "write"):
                outcome = resolve_permission(tool.definition, call, policy=PermissionPolicy(mode=mode), operation_class=operation)
                assert outcome.decision == ("ask" if mode == "ask" and not descriptor.safety.read_only else "allow")
            result = tool.invoke(call, context=context)
            assert isinstance(result, ToolSuccess) and isinstance(result.output, TextOutput)
            assert result.output.text == "49"
        assert (tmp_path / "square.txt").read_text() == "49"
    finally:
        manager.shutdown()
