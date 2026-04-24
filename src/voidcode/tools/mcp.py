from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..mcp import McpToolSafety
from .contracts import ToolCall, ToolDefinition, ToolResult


class McpToolCallLike(Protocol):
    @property
    def content(self) -> list[dict[str, object]]: ...

    @property
    def is_error(self) -> bool: ...


class McpRequester(Protocol):
    def __call__(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, object],
        workspace: Path,
    ) -> McpToolCallLike: ...


class McpTool:
    def __init__(
        self,
        *,
        server_name: str,
        tool_name: str,
        description: str,
        input_schema: dict[str, object],
        safety: McpToolSafety | None = None,
        requester: McpRequester,
    ) -> None:
        self._server_name = server_name
        self._tool_name = tool_name
        self._requester = requester
        self._safety = safety or McpToolSafety()
        self.definition = ToolDefinition(
            name=f"mcp/{server_name}/{tool_name}",
            description=description,
            input_schema=input_schema,
            read_only=self._safety.read_only,
        )

    def invoke(self, call: ToolCall, *, workspace: Path) -> ToolResult:
        result = self._requester(
            server_name=self._server_name,
            tool_name=self._tool_name,
            arguments=dict(call.arguments),
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
