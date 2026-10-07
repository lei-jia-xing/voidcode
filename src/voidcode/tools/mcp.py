from __future__ import annotations

from ..core.tool_context import ToolContext
from ..mcp import McpToolDescriptor
from ..security.json_values import json_wire_object
from .contracts import EmptyOutput, OpaqueToolBody, TextOutput, ToolCall, ToolDefinition, ToolEffect, ToolFailure, ToolResult, ToolSuccess


def mcp_tool_definition(descriptor: McpToolDescriptor) -> ToolDefinition:
    """Project an observed remote descriptor without constructing its adapter."""
    return ToolDefinition(
        name=f"mcp/{descriptor.server_name}/{descriptor.tool_name}",
        description=descriptor.description,
        input_schema=descriptor.input_schema,
        effects=frozenset({ToolEffect.READ if descriptor.safety.read_only else ToolEffect.WRITE, ToolEffect.NETWORK}),
    )


class McpTool:
    def __init__(self, descriptor: McpToolDescriptor) -> None:
        self._server_name = descriptor.server_name
        self._tool_name = descriptor.tool_name
        self._safety = descriptor.safety
        self.definition = mcp_tool_definition(descriptor)

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        requester = context.mcp_request
        if requester is None:
            raise RuntimeError("mcp tool requires an explicit runtime-owned requester")
        result = requester(
            server_name=self._server_name,
            tool_name=self._tool_name,
            arguments=json_wire_object(call.arguments),
            workspace=workspace,
        )
        content_parts: list[str] = []
        for item in result.content:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                content_parts.append(text)
        content = "\n\n".join(content_parts) if content_parts else None
        payload: dict[str, object] = {
            "server": self._server_name,
            "tool": self._tool_name,
            "content": result.content,
            "safety": {
                "read_only": self._safety.read_only,
                "destructive": self._safety.destructive,
                "idempotent": self._safety.idempotent,
                "open_world": self._safety.open_world,
                "source": self._safety.source,
            },
        }
        if result.is_error:
            return ToolFailure(
                tool_name=self.definition.name,
                error=content or f"MCP tool {self.definition.name} reported an error",
                output=TextOutput(content) if content is not None else EmptyOutput(),
                body=OpaqueToolBody(payload),
            )
        return ToolSuccess(
            tool_name=self.definition.name,
            output=TextOutput(content) if content is not None else EmptyOutput(),
            body=OpaqueToolBody(payload),
        )
