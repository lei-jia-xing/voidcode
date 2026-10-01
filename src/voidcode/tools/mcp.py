from __future__ import annotations

from ..core.tool_context import ToolContext
from ..mcp import McpToolSafety
from .contracts import ToolCall, ToolDefinition, ToolEffect, ToolResult


class McpTool:
    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict[str, object],
        safety: McpToolSafety | None = None,
    ) -> None:
        self._server_name = server_name
        self._tool_name = tool_name
        self._safety = safety or McpToolSafety()
        self.definition = ToolDefinition(
            name=f"mcp/{server_name}/{tool_name}",
            description=description,
            input_schema=input_schema,
            effects=frozenset({ToolEffect.READ if self._safety.read_only else ToolEffect.WRITE, ToolEffect.NETWORK}),
        )

    def invoke(self, call: ToolCall, *, context: ToolContext) -> ToolResult:
        workspace = context.require_workspace()
        requester = context.mcp_request
        if requester is None:
            raise RuntimeError("mcp tool requires an explicit runtime-owned requester")
        result = requester(
            server_name=self._server_name,
            tool_name=self._tool_name,
            arguments=call.arguments,
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
            return ToolResult(
                tool_name=self.definition.name,
                status="error",
                error=content or f"MCP tool {self.definition.name} reported an error",
                data=payload,
            )
        return ToolResult(
            tool_name=self.definition.name,
            status="ok",
            content=content,
            data=payload,
        )
